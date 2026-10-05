"""Surveillance du worker par un service séparé (« bot muet »).

Le worker écrit son heartbeat dans `data/bot_runtime.json`. Ce service tourne à part (Docker Compose : service
`watchdog`, données montées en lecture seule, aucun port) et prévient sur Telegram :

- si le heartbeat n'avance plus depuis STALE_SECONDS, ou si le worker est arrêté : worker planté, bloqué ou
  conteneur arrêté. Le message donne le nombre de positions ouvertes dont le stop n'est pas posé chez Binance
  (stop « clôture de bougie », surveillé par le worker seul ; stop absent ou en échec) : rien ne les protège tant
  que le worker ne tourne pas. Rappel toutes les REPEAT_SECONDS tant que le silence dure ;
- quand le worker redonne signe de vie ;
- si le worker est mis en veille alors que des positions sont ouvertes (rappel toutes les STANDBY_REMIND_SECONDS).

Option : `BSM_HEALTHCHECK_URL` (service externe gratuit, par exemple healthchecks.io) reçoit une requête vide
toutes les PING_SECONDS tant que le worker vit ; si le VPS entier tombe, plus de requête, et ce service externe
prévient. Aucune donnée n'est envoyée.

Rien n'est écrit ni passé chez Binance : ce service lit et prévient, c'est tout.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .models import BotRuntime, Position, SLStatus, WorkerState

logger = logging.getLogger("bsm.watchdog")

STALE_SECONDS = 120
REPEAT_SECONDS = 1800
STANDBY_REMIND_SECONDS = 6 * 3600
PING_SECONDS = 300
CHECK_SECONDS = 30


@dataclass
class Exposure:
    """Positions ouvertes (au moins un achat exécuté) et celles dont le stop n'est pas chez Binance."""

    open_positions: int = 0
    unprotected: list[str] = field(default_factory=list)


def stop_on_binance(position: Position) -> bool:
    """Ordre stop actif chez Binance (SL au toucher, ou stop de secours d'un SL à la clôture) : il protège même si
    le worker est arrêté."""
    sl = position.stop_loss
    return sl.status is SLStatus.ACTIVE and bool(sl.order_id)


def exposure(positions: Iterable[Position]) -> Exposure:
    held = [p for p in positions if p.is_open and p.metrics.net_qty > 0]
    return Exposure(open_positions=len(held), unprotected=[p.symbol for p in held if not stop_on_binance(p)])


class Watchdog:
    """Un contrôle par appel de `check()` ; l'état des alertes vit en mémoire (un redémarrage du service peut
    renvoyer une alerte, jamais en perdre une)."""

    def __init__(self, runtime: Callable[[], BotRuntime], positions: Callable[[], list[Position]],
                 notify: Callable[..., object], messages, *, standby_flag: Path,
                 clock: Callable[[], float] = time.time, ping: Optional[Callable[[str], None]] = None,
                 ping_url: str = "", stale: "float | Callable[[], float]" = STALE_SECONDS,
                 repeat: float = REPEAT_SECONDS, remind: float = STANDBY_REMIND_SECONDS,
                 ping_every: float = PING_SECONDS) -> None:
        self.runtime, self.positions, self.notify, self.messages = runtime, positions, notify, messages
        self.standby_flag, self.clock, self.ping, self.ping_url = Path(standby_flag), clock, ping, ping_url
        self.stale, self.repeat, self.remind, self.ping_every = stale, repeat, remind, ping_every
        self._started = clock()
        self._silent_checks = 0
        self._silent_since: Optional[float] = None
        self._last_alert = 0.0
        self._standby_alert = 0.0
        self._last_ping = 0.0

    def _exposure(self) -> Exposure:
        try:
            return exposure(self.positions())
        except Exception:  # noqa: BLE001 - positions illisibles : l'alerte part quand même
            logger.exception("Positions illisibles")
            return Exposure()

    def limit(self) -> float:
        """Âge maximal d'un heartbeat vivant : au moins STALE_SECONDS, et au moins la limite du Dashboard
        (heartbeat_stale_after, 3 × la cadence réglée + 5 s) quand `stale` est une fonction des réglages."""
        try:
            value = float(self.stale() if callable(self.stale) else self.stale)
        except Exception:  # noqa: BLE001 - réglages illisibles : limite par défaut
            value = STALE_SECONDS
        return value if value == value and value > 0 else STALE_SECONDS

    def check(self) -> list[str]:
        """Événements envoyés : "SILENT", "BACK", "STANDBY". Un worker en ERREUR qui écrit encore son heartbeat
        est vivant (il continue de suivre les positions) ; seuls un arrêt (STOPPED) ou un heartbeat trop vieux
        comptent, et seulement après deux contrôles silencieux d'affilée (pas d'alerte sur un tour lent)."""
        now = self.clock()
        runtime = self.runtime()
        age = runtime.heartbeat_age()
        limit = self.limit()
        stopped = runtime.state is WorkerState.STOPPED
        alive = age is not None and age <= limit and not stopped
        sent: list[str] = []
        if not alive:
            self._silent_checks += 1
            if now - self._started < limit or self._silent_checks < 2:
                return sent                       # démarrage, ou un seul contrôle silencieux : on attend le suivant
            if self._silent_since is None:
                self._silent_since = now - (age if age is not None else 0.0)
            if now - self._last_alert >= self.repeat:
                minutes = int((now - self._silent_since) // 60)
                self.notify(self.messages.worker_silent(minutes, stopped=stopped, last_heartbeat=runtime.heartbeat_at,
                                                        exposure=self._exposure()))
                self._last_alert = now
                sent.append("SILENT")
            return sent
        self._silent_checks = 0
        if self._silent_since is not None:
            if self._last_alert:
                minutes = int((now - self._silent_since) // 60)
                self.notify(self.messages.worker_back(minutes))
                sent.append("BACK")
            self._silent_since, self._last_alert = None, 0.0
        if self.standby_flag.exists():
            held = self._exposure()
            if held.open_positions and now - self._standby_alert >= self.remind:
                self.notify(self.messages.worker_standby(held))
                self._standby_alert = now
                sent.append("STANDBY")
        else:
            self._standby_alert = 0.0
        if self.ping and self.ping_url and now - self._last_ping >= self.ping_every:
            try:
                self.ping(self.ping_url)
                self._last_ping = now
            except Exception as exc:  # noqa: BLE001 - le service externe préviendra s'il ne reçoit plus rien
                logger.warning("Signal de vie externe non envoyé : %s", exc)
        return sent
