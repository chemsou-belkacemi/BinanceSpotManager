"""Worker BinanceSpotManager — process separe de Streamlit (section 9).

Boucle ~5 s :
  1. lire le drapeau d'arret, sortir proprement le cas echeant ;
  2. ecrire heartbeat + PID + etat dans data/bot_runtime.json ;
  3. pour chaque position ouverte : recuperer le prix, lancer un cycle
     d'automation (TP surveilles, SL evolutif) ;
  4. reconcilier avec Binance a intervalles plus espaces ;
  5. sauvegarder les positions modifiees.

Le worker reste vivant meme sans position, et ne s'arrete jamais parce qu'une
position se termine.

Demarrage :
    python scripts/bot_worker.py
Arret :
    creer data/bot_stop.flag, ou utiliser le bouton du Dashboard.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

# Rend le package importable quand le script est lance directement.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.automation_engine import AutomationConfig, AutomationEngine  # noqa: E402
from binance_spot_manager.binance_client import BinanceError, BinanceSpotClient  # noqa: E402
from binance_spot_manager.bot_process_manager import WorkerLock, pid_is_alive  # noqa: E402
from binance_spot_manager.config import (  # noqa: E402
    BOT_STOP_FLAG,
    get_settings,
)
from binance_spot_manager.event_store import EventStore, configure_logging, log_error  # noqa: E402
from binance_spot_manager.execution_engine import ExecutionEngine  # noqa: E402
from binance_spot_manager.models import (  # noqa: E402
    BotRuntime,
    EventType,
    WorkerState,
    utcnow,
)
from binance_spot_manager.notification_engine import NotificationEngine  # noqa: E402
from binance_spot_manager.position_engine import PositionEngine  # noqa: E402
from binance_spot_manager.position_store import PositionStore, RuntimeStore  # noqa: E402
from binance_spot_manager.reconciliation_engine import ReconciliationEngine  # noqa: E402
from binance_spot_manager.symbol_rules import SymbolRulesCache  # noqa: E402

#: Un cycle de reconciliation tous les N passages de boucle.
RECONCILE_EVERY = 12


class Worker:
    """Boucle principale, minimale et resiliente."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.events = EventStore()
        self.positions = PositionStore()
        self.runtime_store = RuntimeStore()
        self.lock = WorkerLock()

        self.client = BinanceSpotClient(self.settings)
        self.rules_cache = SymbolRulesCache(self.client)
        self.execution = ExecutionEngine(
            self.client, self.rules_cache, settings=self.settings, events=self.events
        )
        self.position_engine = PositionEngine()
        self.automation = AutomationEngine(
            self.execution,
            self.position_engine,
            self.rules_cache,
            events=self.events,
            config=AutomationConfig(
                sl_replace_threshold_percent=0.05,
                cancel_entries_on_first_tp=False,
            ),
        )
        self.reconciliation = ReconciliationEngine(self.execution, events=self.events)
        self.notifications = NotificationEngine(self.settings)

        self._running = True
        self._loop = 0
        self._price_cache: dict[str, float] = {}
        self._price_fetched_at = 0.0

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def run(self) -> int:
        if not self.lock.acquire():
            message = (
                f"Un worker est deja actif (PID {self.lock.read_owner()}) — "
                f"demarrage refuse"
            )
            log_error(message, context="worker")
            print(message)
            return 2

        self._install_signal_handlers()
        self._set_state(WorkerState.STARTING, "Worker demarre")
        self.events.append(
            EventType.WORKER_STARTED,
            f"Worker demarre (PID {os.getpid()}) — {self.settings.mode_label}",
        )

        try:
            self._loop_forever()
        except KeyboardInterrupt:
            self._set_state(WorkerState.STOPPED, "Interruption clavier")
        finally:
            self._shutdown()

        return 0

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):  # pragma: no cover - depend du signal
            self.events.append(
                EventType.WORKER_STOPPED, f"Signal {signum} recu — arret en cours"
            )
            self._running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def _shutdown(self) -> None:
        self._set_state(WorkerState.STOPPED, "Worker arrete")
        self.events.append(EventType.WORKER_STOPPED, "Worker arrete")
        self.lock.release()
        BOT_STOP_FLAG.unlink(missing_ok=True)

    def _set_state(self, state: WorkerState, message: str = "", **fields) -> None:
        runtime = self.runtime_store.load()
        runtime.state = state
        runtime.pid = os.getpid()
        runtime.environment = self.settings.environment.value
        runtime.run_mode = self.settings.run_mode.value
        runtime.base_url = self.settings.base_url
        runtime.last_message = message or runtime.last_message
        runtime.heartbeat_at = utcnow()
        if runtime.started_at is None:
            runtime.started_at = utcnow()
        for key, value in fields.items():
            if hasattr(runtime, key):
                setattr(runtime, key, value)
        self.runtime_store.save(runtime)

    # ------------------------------------------------------------------
    # Boucle
    # ------------------------------------------------------------------

    def _loop_forever(self) -> None:
        interval = max(self.settings.worker_interval, 1)
        self._set_state(WorkerState.IDLE, "En attente de positions")

        while self._running:
            started = time.time()
            self._loop += 1

            try:
                if self._stop_requested():
                    self.events.append(
                        EventType.WORKER_STOPPED, "Drapeau d'arret detecte"
                    )
                    break
                positions_monitored = self._tick()
                self._set_state(
                    WorkerState.MONITORING if self._has_open_positions() else WorkerState.IDLE,
                    f"Boucle {self._loop}",
                    loop_count=self._loop,
                    positions_monitored=positions_monitored,
                    last_error="",
                )
            except BinanceError as exc:
                # Erreur Binance : journalisee, le worker continue.
                self._set_state(WorkerState.ERROR, f"Erreur Binance : {exc}", last_error=str(exc))
                self.events.append(
                    EventType.ERROR, f"Erreur Binance : {exc}", level="ERROR"
                )
            except Exception as exc:  # noqa: BLE001 — le worker ne doit jamais mourir
                self._set_state(WorkerState.ERROR, f"Erreur : {exc}", last_error=str(exc))
                log_error(str(exc), context="worker.tick")
                if self.notifications.active_channels:
                    self.notifications.notify(self.notifications.error(str(exc), context="worker"))

            elapsed = time.time() - started
            time.sleep(max(interval - elapsed, 0.2))

    def _stop_requested(self) -> bool:
        return BOT_STOP_FLAG.exists()

    def _has_open_positions(self) -> bool:
        return any(p.is_open for p in self.positions.list_all())

    # ------------------------------------------------------------------
    # Un tour de boucle
    # ------------------------------------------------------------------

    def _tick(self) -> int:
        positions = self.positions.list_open()
        price_provider = self._price_provider([p.symbol for p in positions])

        for position in positions:
            price = price_provider(position.symbol)
            if price is None:
                continue

            outcome = self.automation.run_cycle(position, price)

            if outcome.tp_executed is not None:
                executed_tp = next(
                    (
                        t
                        for t in position.take_profits
                        if t.sequence_number == outcome.tp_executed
                    ),
                    None,
                )
                if executed_tp is not None:
                    self.notifications.notify_position_event(
                        position, self.notifications.tp_executed(position, executed_tp)
                    )

            if outcome.sl_moved_to is not None and position.notifications.on_sl_moved:
                self.notifications.notify_position_event(
                    position,
                    self.notifications.sl_moved(
                        position, position.stop_loss.resolved_price or 0.0, outcome.sl_moved_to
                    ),
                )

            if outcome.position_finished:
                self.notifications.notify_position_event(
                    position, self.notifications.position_finished(position)
                )

            self.positions.save(position)

        # Reconciliation periodique (section 19)
        if self._loop % RECONCILE_EVERY == 0:
            self._reconcile(positions)
            self._sync_quote_balance()

        return len(positions)

    def _reconcile(self, positions) -> None:
        for position in positions:
            previous_sync_status = position.sync_status
            try:
                report = self.reconciliation.reconcile(position)
            except Exception as exc:  # noqa: BLE001
                log_error(str(exc), context=f"reconciliation:{position.symbol}")
                continue

            if report.has_desync:
                self.notifications.notify_position_event(
                    position,
                    self.notifications.desync(
                        position, [f.message for f in report.findings]
                    ),
                )
            if report.has_desync or position.sync_status != previous_sync_status:
                self.positions.save(position)

    def _sync_quote_balance(self) -> None:
        """Memorise le solde quote pour que le Dashboard fonctionne hors ligne."""
        if self.settings.dry_run or not self.settings.has_credentials:
            return
        try:
            free = self.client.get_free_balance(self.settings.quote_asset)
        except BinanceError:
            return
        from binance_spot_manager.position_store import get_settings_store

        get_settings_store().update({"last_known_quote_balance": float(free)})

    # ------------------------------------------------------------------
    # Prix
    # ------------------------------------------------------------------

    def _price_provider(self, symbols: list[str]):
        """Retourne un callable(symbol) -> float|None, avec cache court.

        En DRY_RUN, seuls les endpoints publics sont interroges : aucun ordre
        ne peut partir, et l'API publique ne demande pas de cle.
        """
        unique = sorted({s for s in symbols if s})
        now = time.time()
        if unique and now - self._price_fetched_at > max(self.settings.worker_interval - 1, 1):
            try:
                self._price_cache = self.client.get_prices(unique)
                self._price_fetched_at = now
            except BinanceError as exc:
                logging.getLogger("bsm.worker").warning("Prix indisponibles : %s", exc)

        cache = self._price_cache

        def provider(symbol: str) -> Optional[float]:
            return cache.get(symbol)

        return provider


def main() -> int:
    configure_logging(logging.INFO)
    settings = get_settings()
    logger = logging.getLogger("bsm.worker")
    logger.info(
        "Worker BinanceSpotManager — %s — %s",
        settings.mode_label,
        settings.base_url,
    )

    # Garde-fou : refus explicite si une configuration Live est demandee.
    try:
        settings.assert_write_allowed("worker_startup")
    except Exception as exc:
        logger.error("Demarrage refuse : %s", exc)
        print(f"Demarrage refuse : {exc}")
        return 3

    return Worker().run()


if __name__ == "__main__":
    raise SystemExit(main())
