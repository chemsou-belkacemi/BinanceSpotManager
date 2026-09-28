"""Worker BinanceSpotManager — process separe de Streamlit (section 9).

Boucle configurable (1 s dans les preferences locales) :
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
    CloseReason,
    Commission,
    EventType,
    SLStatus,
    SyncStatus,
    TPStatus,
    WorkerState,
    utcnow,
)
from binance_spot_manager.notification_engine import NotificationEngine  # noqa: E402
from binance_spot_manager.market_price_stream import DemoMarketPriceStream  # noqa: E402
from binance_spot_manager.position_engine import PositionEngine, finish_position, recompute_position  # noqa: E402
from binance_spot_manager.position_store import PositionStore, RuntimeStore, get_settings_store  # noqa: E402
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
        self.market_prices = DemoMarketPriceStream(self.settings)

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
        if hasattr(self, "market_prices"):
            runtime.price_diagnostics = self.market_prices.snapshot()
            runtime.price_diagnostics["sources"] = getattr(self, "_price_sources", {})
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
            interval = self._worker_interval()
            time.sleep(max(interval - elapsed, 0.2))

    def _worker_interval(self) -> int:
        """Cadence sauvegardee dans Settings, avec repli sur la configuration."""
        saved = get_settings_store().load()
        value = saved.get("worker_interval") if isinstance(saved, dict) else None
        try:
            interval = int(value) if value is not None else self.settings.worker_interval
        except (TypeError, ValueError):
            interval = self.settings.worker_interval
        return max(1, min(interval, 300))

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
        errors = [
            f"Stockage local : {message}"
            for message in getattr(self.positions, "read_errors", [])
        ]
        processed = []
        for position in positions:
            try:
                self._process_position(position, price_provider(position.symbol))
            except Exception as exc:  # noqa: BLE001 - isoler sans masquer l'erreur
                errors.append(f"{position.symbol} ({position.position_id}) : {exc}")
                logging.getLogger("bsm.worker").exception(
                    "Suivi interrompu pour %s (%s)", position.symbol, position.position_id,
                )
            else:
                processed.append(position)

        # Reconciliation periodique (section 19)
        if self._loop % RECONCILE_EVERY == 0:
            # Ne pas reconcilier un objet potentiellement modifie par un cycle echoue.
            self._reconcile(processed)
            self._sync_quote_balance()

        if errors:
            raise RuntimeError("Erreur de suivi : " + " ; ".join(errors))
        return len(positions)

    def _process_position(self, position, price: Optional[float]) -> None:
        """Un seul cycle par position ; aucune relance immediate en cas d'erreur."""
        if position.oco_exit is not None:
            self._monitor_oco(position, price)
            self.positions.save(position)
            return
        if price is None:
            return

        outcome = self.automation.run_cycle(position, price)
        if outcome.tp_executed is not None:
            executed_tp = next(
                (t for t in position.take_profits if t.sequence_number == outcome.tp_executed),
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

    def _monitor_oco(self, position, price: Optional[float]) -> None:
        """Lecture seule des deux branches : jamais de deuxième vente locale."""
        oco = position.oco_exit
        # Les executions Binance restent consultables meme sans prix public frais.
        if price is not None:
            position.metrics.current_price = price
            recompute_position(position)
        if oco.status not in {"ACTIVE", "PARTIAL"}:
            return

        tp_order = self.execution.fetch_order_status(position.symbol, order_id=oco.tp_order_id)
        sl_order = self.execution.fetch_order_status(position.symbol, order_id=oco.sl_order_id)
        if tp_order is None or sl_order is None:
            position.sync_status = SyncStatus.DESYNC_DETECTED
            if not oco.missing_branch_alerted:
                oco.missing_branch_alerted = True
                self.events.append(
                    EventType.ERROR, "Branche OCO introuvable côté Binance",
                    position_id=position.position_id, symbol=position.symbol, level="ERROR",
                )
            return

        if oco.missing_branch_alerted:
            oco.missing_branch_alerted = False
            self.events.append(
                EventType.POSITION_UPDATED,
                "Lectures des deux branches OCO rétablies ; contrôle des états en cours",
                position_id=position.position_id, symbol=position.symbol, level="INFO",
            )

        tp_qty = tp_order.executed_qty
        sl_qty = sl_order.executed_qty
        if tp_qty > 0 and sl_qty > 0:
            position.sync_status = SyncStatus.DESYNC_DETECTED
            oco.status = "ERROR"
            self.events.append(
                EventType.ERROR,
                "OCO incoherent : les deux branches indiquent une execution. "
                "Aucune vente supplementaire ; verification manuelle requise.",
                position_id=position.position_id, symbol=position.symbol,
                level="CRITICAL", tp_order_id=oco.tp_order_id,
                sl_order_id=oco.sl_order_id, tp_qty=tp_qty, sl_qty=sl_qty,
            )
            return

        if tp_qty > 0 or sl_qty > 0:
            filled = tp_order if tp_qty > 0 else sl_order
            if filled.is_open:
                # Une vente partielle ouverte peut laisser du BTC non protégé.
                if oco.status != "PARTIAL":
                    oco.status = "PARTIAL"
                    position.sync_status = SyncStatus.DESYNC_DETECTED
                    self.events.append(
                        EventType.ERROR, "OCO partiellement exécuté : contrôle requis",
                        position_id=position.position_id, symbol=position.symbol,
                        level="CRITICAL",
                    )
                return

            trades = self.execution.fetch_my_trades(
                position.symbol, order_id=filled.order_id
            )
            commissions = [
                Commission(asset=trade["commissionAsset"], amount=float(trade["commission"]))
                for trade in trades
                if trade.get("commissionAsset") and float(trade.get("commission") or 0) > 0
            ]

            if tp_qty > 0:
                tp = position.next_tp
                if tp is not None:
                    tp.estimated_qty = oco.quantity
                    self.position_engine.apply_tp_fill(
                        position, tp.tp_id, executed_qty=filled.executed_qty,
                        average_price=filled.average_price,
                        quote_received=filled.cummulative_quote_qty,
                        commissions=commissions,
                        order_id=filled.order_id,
                    )
                    self.events.append(
                        EventType.TP_EXECUTED,
                        f"TP OCO {tp.sequence_number} exécuté @ {filled.average_price}",
                        position_id=position.position_id, symbol=position.symbol,
                        order_id=filled.order_id, qty=filled.executed_qty,
                    )
                    # L'autre branche de cet OCO a ete annulee par Binance.
                    position.stop_loss.status = SLStatus.CANCELED
                    self.notifications.notify_position_event(
                        position, self.notifications.tp_executed(position, tp)
                    )
                rules = self.rules_cache.get(position.symbol)
                if float(rules.round_qty(position.metrics.net_qty)) <= 0:
                    finish_position(position, CloseReason.ALL_TP_HIT)
            else:
                for tp in position.take_profits:
                    if tp.status is TPStatus.SUBMITTED:
                        tp.status = TPStatus.CANCELED
                if sl_qty >= oco.quantity - 1e-12:
                    self.position_engine.apply_sl_fill(
                        position, executed_qty=filled.executed_qty,
                        average_price=filled.average_price,
                        quote_received=filled.cummulative_quote_qty,
                        commissions=commissions,
                    )
                    self.events.append(
                        EventType.SL_EXECUTED,
                        f"SL OCO exécuté @ {filled.average_price}",
                        position_id=position.position_id, symbol=position.symbol,
                        order_id=filled.order_id, qty=filled.executed_qty,
                    )
                    self.notifications.notify_position_event(
                        position, self.notifications.sl_executed(position)
                    )
                else:
                    position.stop_loss.executed_qty = filled.executed_qty
                    position.stop_loss.average_fill_price = filled.average_price
                    position.stop_loss.quote_received = filled.cummulative_quote_qty
                    position.stop_loss.commissions = commissions
                    recompute_position(position)
            oco.status = "FILLED" if position.status.value == "CLOSED" else "PARTIAL_TERMINAL"
            if oco.status == "FILLED":
                self.events.append(
                    EventType.POSITION_FINISHED,
                    f"Position {position.symbol} terminée par OCO",
                    position_id=position.position_id, symbol=position.symbol,
                )
                self.notifications.notify_position_event(
                    position, self.notifications.position_finished(position)
                )
            if oco.status == "PARTIAL_TERMINAL":
                position.sync_status = SyncStatus.DESYNC_DETECTED
            return

        if not tp_order.is_open and not sl_order.is_open:
            oco.status = "FAILED"
            position.sync_status = SyncStatus.DESYNC_DETECTED
            self.events.append(
                EventType.ERROR, "OCO terminé sans vente : position non protégée",
                position_id=position.position_id, symbol=position.symbol, level="CRITICAL",
            )

    def _reconcile(self, positions) -> None:
        for position in positions:
            if position.oco_exit is not None:
                continue  # La réconciliation legacy ne connaît pas les listes OCO.
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
        streamed = self.market_prices.prices(unique)
        now = time.time()
        missing = [symbol for symbol in unique if symbol not in streamed]
        if missing and now - self._price_fetched_at > max(self._worker_interval() - 1, 1):
            try:
                self._price_cache = self.client.get_prices(missing)
                self._price_fetched_at = now
            except BinanceError as exc:
                logging.getLogger("bsm.worker").warning("Prix indisponibles : %s", exc)

        # Un ancien prix REST ne doit jamais declencher un TP apres une panne.
        rest_prices = self._price_cache if now - self._price_fetched_at <= 5 else {}
        cache = {**rest_prices, **streamed}
        self._price_sources = {
            symbol: "WebSocket" if symbol in streamed else "REST" if symbol in rest_prices else "Indisponible"
            for symbol in unique
        }

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
