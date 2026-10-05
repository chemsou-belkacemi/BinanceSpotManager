"""Perte maximale du jour sur tout le portefeuille.

Résultat du jour = PnL réalisé des positions terminées depuis 00:00 UTC (frais compris, BNB au cours actuel)
+ PnL latent des positions ouvertes (dernier prix connu du worker). Capital = USDT du compte (libre + bloqué)
+ capital engagé dans les positions ouvertes. Si le résultat du jour descend à −X % du capital ou plus bas, plus
AUCUNE nouvelle entrée, manuelle ou automatique, jusqu'à 00:00 UTC ; les positions ouvertes restent suivies
(stops, objectifs, clôtures). Le blocage tient jusqu'à minuit même si le résultat remonte ; désactiver la règle
dans Settings le lève aussitôt.

Réglages (Settings → Worker & risque) : `daily_loss_enabled` (activé par défaut), `daily_loss_percent` (3 %).
Rien n'est vendu ni annulé : la règle ne fait que refuser de nouvelles entrées.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .config import DATA_DIR
from .models import Position
from .performance import closed_between, valued

PREFIX = "daily_loss_"
DEFAULT_PERCENT = 3.0
BOUNDS = (0.5, 50.0)
GUARD_FILE = DATA_DIR / "daily_guard.json"
CHECK_EVERY_SECONDS = 60


def settings_from(preferences: Any) -> tuple[bool, float]:
    prefs = preferences if isinstance(preferences, dict) else {}
    enabled = bool(prefs.get(PREFIX + "enabled", True))
    try:
        percent = float(prefs.get(PREFIX + "percent", DEFAULT_PERCENT))
    except (TypeError, ValueError):
        percent = DEFAULT_PERCENT
    if percent != percent:                                   # NaN
        percent = DEFAULT_PERCENT
    return enabled, min(max(percent, BOUNDS[0]), BOUNDS[1])


def day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def day_result(positions: Iterable[Position], now: datetime, fee_rates_for: Callable[[Position], dict]) -> float:
    """PnL réalisé des positions terminées aujourd'hui (UTC, frais compris) + latent des positions ouvertes."""
    positions = list(positions)
    start = day_start(now)
    closed_today = valued(closed_between(positions, start, now + timedelta(seconds=1)), fee_rates_for)
    realized = sum(p.pnl.realized for p in closed_today)
    latent = sum(p.pnl.unrealized for p in positions if p.is_open)
    return realized + latent


class DailyLossGuard:
    """État persistant (un redémarrage du worker ne lève pas le blocage du jour)."""

    def __init__(self, preferences: Callable[[], Any], *, path: Path = GUARD_FILE,
                 clock: Callable[[], float] = time.time,
                 read: Optional[Callable] = None, write: Optional[Callable] = None) -> None:
        from .position_store import atomic_write_json, read_json
        self.path = Path(path)
        self.preferences = preferences
        self.clock = clock
        self._read = read or read_json
        self._write = write or atomic_write_json

    def _today(self, now: Optional[float]) -> str:
        moment = datetime.fromtimestamp(self.clock() if now is None else now, tz=timezone.utc)
        return moment.date().isoformat()

    def state(self) -> dict:
        raw = self._read(self.path)
        return raw if isinstance(raw, dict) else {}

    def refusal(self, now: Optional[float] = None) -> str:
        """Raison du blocage des nouvelles entrées aujourd'hui, sinon « »."""
        state = self.state()
        enabled, _ = settings_from(self.preferences())
        if not enabled or not state.get("blocked") or state.get("day") != self._today(now):
            return ""
        return str(state.get("reason") or "Perte maximale du jour atteinte : aucune nouvelle entrée jusqu'à 00:00 UTC")

    def check(self, result: float, capital: float, now: Optional[float] = None) -> list[tuple[str, str]]:
        """Événements à annoncer : ("STARTED", détail), ("ENDED", détail)."""
        today = self._today(now)
        enabled, percent = settings_from(self.preferences())
        state = self.state()
        events: list[tuple[str, str]] = []
        if state.get("blocked") and (state.get("day") != today or not enabled):
            why = "nouvelle journée (00:00 UTC)" if state.get("day") != today else "règle désactivée dans Settings"
            events.append(("ENDED", f"Perte maximale du jour levée ({why}) : nouvelles entrées à nouveau permises."))
            self._write(self.path, {"day": today, "blocked": False})
            state = {}
        if not enabled or capital <= 0 or (state.get("blocked") and state.get("day") == today):
            return events
        share = result / capital * 100.0
        if share > -percent:
            return events
        reason = (f"Perte du jour {result:+.2f} USDT ({share:+.2f} % du capital, seuil −{percent:g} %) : aucune "
                  "nouvelle entrée jusqu'à 00:00 UTC ; les positions ouvertes restent suivies")
        self._write(self.path, {"day": today, "blocked": True, "reason": reason, "result": round(result, 4),
                                "capital": round(capital, 4), "percent": round(share, 4),
                                "at": self.clock() if now is None else now})
        events.append(("STARTED", reason + "."))
        return events
