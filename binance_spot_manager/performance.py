"""Statistiques de positions terminees : par canal d'origine, achats tardifs, resume d'une periode.

Fonctions pures sur des positions deja recalculees (PnL frais compris si les frais BNB ont ete
valorises par l'appelant). Utilisees par History, le rapport quotidien et le routage des signaux.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional

from .models import Position, SignalSource

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


def channel_of(position: Position) -> str:
    """Canal d'origine d'une position (nom du canal Telegram si connu)."""
    if not position.source_groups:
        return "manuel"
    group = position.source_groups[0]
    label = (group.label or "").strip()
    source = group.source
    if source == SignalSource.MANUAL:
        return label if label and label.lower() not in {"manual", "manuel"} else "manuel"
    generic = not label or label.lower() in {source.value.lower(), "telegram", "api"} or label.startswith("Signal ")
    if source == SignalSource.TELEGRAM:
        return UNKNOWN_TELEGRAM if generic else label
    return label if not generic else source.value


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
    """Statistiques par groupe (canal par defaut), triees du meilleur resultat net au pire ; les
    positions sans achat execute ne comptent pas."""
    groups: dict[str, GroupStats] = {}
    for position in positions:
        if not bought(position):
            continue
        name = key(position)
        groups.setdefault(name, GroupStats(name)).add(position.pnl.realized)
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


def losing_channel(positions: Iterable[Position], channel: str, *, min_trades: int) -> Optional[str]:
    """Motif de mise en revue d'un signal : le canal a au moins `min_trades` positions terminees et un
    resultat net negatif, frais compris. None sinon (pas assez de recul, ou canal gagnant)."""
    if not channel or channel in {UNKNOWN_TELEGRAM, "manuel"}:
        return None
    stats = next((s for s in stats_by([p for p in positions if not p.is_open]) if s.name == channel), None)
    if stats is None or stats.positions < min_trades or stats.net >= 0:
        return None
    return (f"Canal « {channel} » perdant : {stats.net:+.2f} sur {stats.positions} positions terminées "
            f"({stats.wins} gagnantes) — à confirmer")
