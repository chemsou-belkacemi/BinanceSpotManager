"""Rapport quotidien sur Telegram : resultat net du jour et des 7 derniers jours (frais compris, BNB au
cours actuel), positions ouvertes, risque total si tous les stops sont touches, meilleur et pire canal.

« Aujourd'hui » et « 7 derniers jours » ne comptent que les positions TERMINEES dans la periode ; le
« PnL realise » du Dashboard compte tout depuis le debut, TP deja vendus des positions ouvertes compris.
Le rapport donne aussi ce chiffre (ligne « Depuis le debut ») et le Dashboard ceux du rapport : `results`
sert aux deux, pour que les memes questions aient les memes reponses.

Un seul message par jour, a l'heure choisie (UTC) ; la date du dernier envoi est gardee dans un fichier
pour ne pas renvoyer apres un redemarrage.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import DATA_DIR
from .models import Position
from .performance import channel_of, closed_between, stats_by, valued

PREFIX = "daily_report_"
REPORT_FILE = DATA_DIR / "daily_report.json"


def settings_from(preferences: Any) -> tuple[bool, int]:
    prefs = preferences if isinstance(preferences, dict) else {}
    enabled = bool(prefs.get(PREFIX + "enabled", True))
    try:
        hour = int(prefs.get(PREFIX + "hour_utc", 20))
    except (TypeError, ValueError):
        hour = 20
    return enabled, min(max(hour, 0), 23)


@dataclass(frozen=True)
class Results:
    """Chiffres communs au rapport Telegram et au Dashboard, sur des positions deja valorisees."""

    today: list[Position]          # terminees depuis 00:00 UTC
    week: list[Position]           # terminees sur les 7 derniers jours
    realized_all: float            # depuis le debut : positions terminees + TP vendus des positions ouvertes
    realized_open: float           # dont TP deja vendus sur les positions encore ouvertes
    latent: float                  # positions ouvertes, au dernier prix connu


def results(positions: list[Position], now: datetime,
            rate: Callable[[Position], float] = lambda p: 1.0) -> Results:
    """`rate` : conversion de la devise de cotation de chaque position (le Dashboard passe ses taux)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = closed_between(positions, now - timedelta(days=7), now + timedelta(seconds=1))
    open_positions = [p for p in positions if p.is_open]
    return Results(
        today=[p for p in week if (p.closed_at or p.updated_at) >= midnight],
        week=week,
        realized_all=sum(p.pnl.realized * rate(p) for p in positions),
        realized_open=sum(p.pnl.realized * rate(p) for p in open_positions),
        latent=sum(p.pnl.unrealized * rate(p) for p in open_positions),
    )


def _line(name: str, positions: list[Position]) -> str:
    if not positions:
        return f"{name} : aucune position terminée"
    total = sum(p.pnl.realized for p in positions)
    wins = sum(1 for p in positions if p.pnl.realized > 0)
    return f"{name} : {total:+.2f} USDT sur {len(positions)} position(s) terminée(s), {wins} gagnante(s)"


def build(positions: list[Position], now: datetime, fee_rates_for: Callable[[Position], dict],
          channel: Callable[[Position], str] = channel_of) -> tuple[str, str]:
    """(titre, corps) du rapport ; `positions` : toutes les positions (ouvertes et terminees) ;
    `channel` : trader ou canal d'une position (performance.channel_resolver pour les anciennes)."""
    figures = results(valued(positions, fee_rates_for), now)
    week, today = figures.week, figures.today
    open_positions = [p for p in positions if p.is_open]
    risk = sum(abs(p.metrics.max_loss_at_sl) for p in open_positions)
    committed = sum(p.metrics.capital_committed for p in open_positions)
    lines = [_line("Aujourd'hui (depuis 00:00 UTC)", today), _line("7 derniers jours", week),
             f"Depuis le début : réalisé {figures.realized_all:+.2f} USDT, dont {figures.realized_open:+.2f} "
             f"déjà encaissés par des TP sur les positions encore ouvertes ; avec le latent "
             f"{figures.realized_all + figures.latent:+.2f} USDT (« PnL réalisé » et « PnL total » du Dashboard)",
             f"Positions ouvertes : {len(open_positions)} ; capital engagé {committed:.2f} USDT ; "
             f"latent {figures.latent:+.2f} USDT",
             f"Risque si tous les stops sont touchés : −{risk:.2f} USDT"]
    groups = stats_by(week, key=channel)
    if len(groups) >= 2:
        best, worst = groups[0], groups[-1]
        lines.append(f"Meilleur trader/canal (7 j) : {best.name} {best.net:+.2f} USDT ({best.positions} pos.)")
        lines.append(f"Pire trader/canal (7 j) : {worst.name} {worst.net:+.2f} USDT ({worst.positions} pos.)")
    elif groups:
        lines.append(f"Trader/canal (7 j) : {groups[0].name} {groups[0].net:+.2f} USDT "
                     f"({groups[0].positions} pos.)")
    lines.append("Résultats frais compris (BNB au cours actuel) ; Binance Demo.")
    total_today = sum(p.pnl.realized for p in today)
    title = f"Rapport du {now.strftime('%d/%m')} — {total_today:+.2f} USDT aujourd'hui"
    return title, "\n".join(lines)


class DailyReport:
    """Envoi unique par jour, apres l'heure choisie."""

    def __init__(self, preferences: Callable[[], Any], *, path: Path = REPORT_FILE,
                 clock: Callable[[], float] = time.time,
                 read: Optional[Callable] = None, write: Optional[Callable] = None) -> None:
        from .position_store import atomic_write_json, read_json
        self.path = Path(path)
        self.preferences = preferences
        self.clock = clock
        self._read = read or read_json
        self._write = write or atomic_write_json
        self._sent_on = ""                    # date du dernier envoi connue en memoire (appel a chaque tour)

    def due(self, now: Optional[float] = None) -> Optional[datetime]:
        """Instant UTC du rapport s'il est du maintenant (et pas deja envoye aujourd'hui), sinon None."""
        enabled, hour = settings_from(self.preferences())
        if not enabled:
            return None
        moment = datetime.fromtimestamp(self.clock() if now is None else now, tz=timezone.utc)
        today = moment.date().isoformat()
        if moment.hour < hour or self._sent_on == today:
            return None
        state = self._read(self.path)
        if isinstance(state, dict) and state.get("last_sent") == today:
            self._sent_on = today             # deja envoye avant un redemarrage
            return None
        return moment

    def mark_sent(self, moment: datetime) -> None:
        self._write(self.path, {"last_sent": moment.date().isoformat(), "sent_at": moment.isoformat()})
        self._sent_on = moment.date().isoformat()
