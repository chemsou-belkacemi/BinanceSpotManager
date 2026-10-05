"""Statistiques de positions terminees : par trader ou canal d'origine, achats tardifs, resume d'une
periode.

Fonctions pures sur des positions deja recalculees (PnL frais compris si les frais BNB ont ete
valorises par l'appelant). Utilisees par History, le rapport quotidien et le routage des signaux. Les
variantes d'ecriture d'un meme nom sont regroupees (trader_name.name_key).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional

from .models import Position, SignalSource
from .trader_name import name_key, trader_of

UNKNOWN_TELEGRAM = "Telegram (canal inconnu)"
LATE_FILL_TAG = "tp1_avant_achat"


def valued(positions: Iterable[Position], fee_rates_for: Callable[[Position], dict]) -> list[Position]:
    """Copies recalculees avec les frais payes en BNB valorises (meme PnL que History) ; les
    positions d'origine ne sont pas modifiees. Un cours indisponible laisse ces frais non deduits."""
    from .position_engine import recompute_position

    out = []
    for position in positions:
        copy = position.model_copy(deep=True)
        try:
            rates = fee_rates_for(position) or {}
        except Exception:  # noqa: BLE001 - frais BNB non valorises plutot qu'aucun chiffre
            rates = {}
        recompute_position(copy, fee_rates=rates)
        out.append(copy)
    return out


def bought(position: Position) -> bool:
    """Au moins un achat execute : une position annulee avant tout achat n'est pas un trade."""
    return position.metrics.total_bought_qty > 0


def signal_row_id(position: Position) -> str:
    """Identifiant de la ligne de la boite des signaux d'une position issue d'un signal ("" sinon)."""
    return position.tags[1] if len(position.tags) >= 2 and position.tags[0] == "signal" else ""


def channel_of(position: Position) -> str:
    """Trader ou canal d'origine d'une position (nom enregistre a sa creation)."""
    if not position.source_groups:
        return "manuel"
    group = position.source_groups[0]
    label = (group.label or "").strip()
    source = group.source
    generic = (not label or label.startswith("Signal ")
               or label.lower() in {source.value.lower(), "telegram", "api", "manual", "manuel"})
    if not generic:
        return label
    return {SignalSource.TELEGRAM: UNKNOWN_TELEGRAM, SignalSource.MANUAL: "manuel"}.get(source, source.value)


def channel_resolver(raw_text: Callable[[str], str]) -> Callable[[Position], str]:
    """channel_of, complete pour une position issue d'un signal sans nom enregistre (ouverte avant le
    suivi par canal) par le nom ecrit en tete du texte du signal : `raw_text(identifiant de ligne)`
    rend ce texte (boite des signaux), "" s'il est introuvable."""
    cache: dict[str, str] = {}

    def resolve(position: Position) -> str:
        name = channel_of(position)
        row_id = signal_row_id(position)
        if name not in {UNKNOWN_TELEGRAM, "manuel", "api"} or not row_id:
            return name
        if row_id not in cache:
            try:
                cache[row_id] = trader_of(raw_text(row_id) or "")
            except Exception:  # noqa: BLE001 - boite illisible : le nom reste inconnu
                cache[row_id] = ""
        return cache[row_id] or name

    return resolve


@dataclass
class GroupStats:
    name: str
    positions: int = 0
    wins: int = 0
    gains: float = 0.0
    losses: float = 0.0          # somme des resultats <= 0 (negative ou nulle)

    @property
    def net(self) -> float:
        return self.gains + self.losses

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.positions if self.positions else None

    @property
    def average_gain(self) -> Optional[float]:
        return self.gains / self.wins if self.wins else None

    @property
    def average_loss(self) -> Optional[float]:
        lost = self.positions - self.wins
        return self.losses / lost if lost else None

    @property
    def profit_factor(self) -> Optional[float]:
        if self.losses < 0:
            return self.gains / abs(self.losses)
        return None

    def add(self, realized: float) -> None:
        self.positions += 1
        if realized > 0:
            self.wins += 1
            self.gains += realized
        else:
            self.losses += realized


def stats_by(positions: Iterable[Position], key=channel_of) -> list[GroupStats]:
    """Statistiques par groupe (trader ou canal par defaut), triees du meilleur resultat net au pire ;
    les variantes d'un meme nom sont regroupees sous l'ecriture la plus frequente ; les positions sans
    achat execute ne comptent pas."""
    groups: dict[str, GroupStats] = {}
    spellings: dict[str, Counter] = {}
    for position in positions:
        if not bought(position):
            continue
        name = key(position)
        group = name_key(name) or name
        spellings.setdefault(group, Counter())[name] += 1
        groups.setdefault(group, GroupStats(name)).add(position.pnl.realized)
    for group, stats in groups.items():
        stats.name = spellings[group].most_common(1)[0][0]
    return sorted(groups.values(), key=lambda g: g.net, reverse=True)


def late_fill_split(positions: Iterable[Position]) -> dict[str, GroupStats]:
    """Positions dont le premier TP a ete atteint AVANT l'achat (achat garde, execute plus tard)
    contre les autres : mesure de l'option « annuler l'achat si le TP1 est touche avant »."""
    groups = stats_by(positions, key=lambda p: "achat apres TP1 deja touche" if LATE_FILL_TAG in p.tags else "autres")
    return {stats.name: stats for stats in groups}


def closed_between(positions: Iterable[Position], start: datetime, end: datetime) -> list[Position]:
    """Positions terminees dans [start, end[ apres au moins un achat execute."""
    return [p for p in positions
            if not p.is_open and bought(p) and start <= (p.closed_at or p.updated_at) < end]


def losing_channel(positions: Iterable[Position], channel: str, *, min_trades: int,
                   key: Callable[[Position], str] = channel_of) -> Optional[str]:
    """Motif de mise en revue d'un signal : son trader ou canal (`key` le donne pour chaque position)
    a au moins `min_trades` positions terminees et un resultat net negatif, frais compris. None sinon
    (nom inconnu, pas assez de recul, ou gagnant)."""
    if not channel or channel in {UNKNOWN_TELEGRAM, "manuel"}:
        return None
    wanted = name_key(channel)
    closed = [p for p in positions if not p.is_open]
    stats = next((s for s in stats_by(closed, key=key) if name_key(s.name) == wanted), None)
    if stats is None or stats.positions < min_trades or stats.net >= 0:
        return None
    return (f"Trader/canal « {channel} » perdant : {stats.net:+.2f} sur {stats.positions} positions "
            f"terminées ({stats.wins} gagnantes) — à confirmer")
