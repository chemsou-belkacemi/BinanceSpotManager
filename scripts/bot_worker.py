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
from binance_spot_manager.fee_token import FeeTokenMonitor  # noqa: E402
from binance_spot_manager.models import (  # noqa: E402
    BotRuntime,
    CloseReason,
    Commission,
    EntryStatus,
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
from binance_spot_manager.reconciliation_engine import ReconciliationEngine, find_orphan_bot_orders  # noqa: E402
from binance_spot_manager.csi_client import CsiClient  # noqa: E402
from binance_spot_manager.daily_guard import CHECK_EVERY_SECONDS as DAILY_LOSS_CHECK_SECONDS  # noqa: E402
from binance_spot_manager.daily_guard import DailyLossGuard, day_result  # noqa: E402
from binance_spot_manager.daily_report import DailyReport  # noqa: E402
from binance_spot_manager.market_guard import MarketGuard  # noqa: E402
from binance_spot_manager.csi_light import CsiLightGuard  # noqa: E402
from binance_spot_manager.licence import LicenceGate  # noqa: E402
from binance_spot_manager.signal_auto_execution import AutomaticSignalExecutor  # noqa: E402
from binance_spot_manager.signal_drop import SignalDropImporter  # noqa: E402
from binance_spot_manager.signal_feedback import SignalFeedbackWriter  # noqa: E402
from binance_spot_manager.signal_inbox import SignalInbox  # noqa: E402
from binance_spot_manager.symbol_rules import SymbolRulesCache  # noqa: E402
from binance_spot_manager.telegram_commands import ManualPause, TelegramCommands  # noqa: E402
from binance_spot_manager.telegram_signals import TelegramSignalPoller  # noqa: E402
from binance_spot_manager.command_store import CommandStore, account_scope
from binance_spot_manager.command_processor import CommandProcessor
from binance_spot_manager.dashboard_service import DashboardService

#: Un cycle de reconciliation tous les N passages de boucle.
RECONCILE_EVERY = 12

#: Controle des ordres BSM orphelins : au premier tour (et en sortie de veille), puis tous les N
#: tours ; apres un echec, nouvel essai RECONCILE_EVERY tours plus tard. Un seul appel openOrders
#: sans symbole (poids 80) couvre toutes les paires du compte.
ORPHAN_AUDIT_EVERY = 60

#: Achats envoyes dont le remplissage n'est connu que par la reconciliation.
AWAITING_FILL = frozenset({EntryStatus.SUBMITTED, EntryStatus.PARTIALLY_FILLED})

#: Entre deux reconciliations completes, les positions dont un achat attend son remplissage sont
#: relues au plus une fois toutes les N secondes (horloge monotone). Les relire a chaque tour
#: (1 s) approchait la limite de poids Binance (6 000/min) avec 5 positions en attente.
AWAITING_FILL_RECONCILE_SECONDS = 10


def awaiting_fill(position) -> bool:
    """Vrai si un achat est chez Binance sans remplissage complet constate : tant que la quantite
    achetee n'est pas connue, aucun stop ne peut la proteger."""
    return position.oco_exit is None and any(entry.status in AWAITING_FILL for entry in position.entries)


class Worker:
    """Boucle principale, minimale et resiliente."""

    #: Vrai pendant la veille Docker (arret demande, process maintenu).
    _in_standby = False
    #: Reconciliation complete due a la reprise : au demarrage du process et en sortie de veille
    #: Docker (le process ne redemarre pas, _loop ne repasse jamais a 1). Remis a False une fois
    #: la reconciliation faite : un tour en erreur avant elle la reporte au tour suivant.
    _resume_reconcile = False
    #: Horloge monotone (remplacable dans les tests) de la cadence des achats en attente.
    _monotonic = staticmethod(time.monotonic)
    #: Instant monotone de la derniere relecture des achats en attente (None : jamais).
    _awaiting_reconciled_at = None
    #: Tour du prochain controle des ordres orphelins (0 : des le prochain tour).
    _next_orphan_audit = 0
    #: Dernier constat signale (ensemble d'ordres orphelins, "ECHEC" ou None) : un seul evenement
    #: par nouveau constat, pas a chaque controle.
    _orphan_report = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self.events = EventStore()
        self.positions = PositionStore()
        self.runtime_store = RuntimeStore()
        self.lock = WorkerLock(pid_checks=not self.settings.worker_managed_by_docker)

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
        self.notifications = NotificationEngine(self.settings, background=True)
        self.market_prices = DemoMarketPriceStream(self.settings)
        self.commands = CommandStore()
        self.signal_inbox = SignalInbox()
        risk_service = DashboardService(self.settings, position_store=self.positions, client=self.client, events=self.events)
        # Licence de location : ne bloque que les NOUVELLES entrées, jamais le suivi des positions.
        self.licence_gate = LicenceGate()
        # Perte maximale du jour : même effet que la licence (nouvelles entrées refusées jusqu'à 00:00 UTC).
        self.daily_guard = DailyLossGuard(lambda: get_settings_store().load())
        # Pause manuelle (commande Telegram /pause) : nouvelles entrées refusées jusqu'à /reprise.
        self.manual_pause = ManualPause()
        self.command_processor = CommandProcessor(
            self.commands, self.positions, self.execution, risk_service.risk_limits,
            entry_gate=self._entry_refusal,
        )
        self.fee_token_monitor = FeeTokenMonitor(
            self.client, self.events, lambda: get_settings_store().load(), interval_seconds=60,
        )
        self.telegram_poller = TelegramSignalPoller(
            self.settings.telegram_bot_token,
            account_scope(self.settings),
            lambda: get_settings_store().load(),
            inbox=self.signal_inbox,
            pause_requested=self._stop_requested,
            commands=TelegramCommands(lambda: get_settings_store().load(), self.settings.telegram_chat_id,
                                      self.manual_pause, self._status_text),
        )
        # Retour d'execution des signaux V2 (data/signal_drop/outgoing/), hors DRY_RUN :
        # sans ordre reel, aucun evenement ne doit pretendre a une execution.
        self.signal_feedback = SignalFeedbackWriter(
            account_scope(self.settings), self.signal_inbox, self.commands,
            positions=self.positions, client=self.client, enabled=not self.settings.dry_run,
        )
        # Depot direct (generateur ML v1 ou CSI V3) : lecture de data/signal_drop/incoming/.
        self.signal_drop = SignalDropImporter(
            self.signal_inbox, account_scope(self.settings),
            lambda: get_settings_store().load(),
            feedback=self.signal_feedback,
        )
        # Routage : seuls les signaux sans motif de revue partent automatiquement ;
        # les autres passent « À confirmer » (push optionnel, sans ligne de prix).
        self.auto_signal_executor = AutomaticSignalExecutor(
            account_scope(self.settings), self.signal_inbox, self.commands,
            self.client, self.rules_cache, risk_service.risk_limits,
            lambda: get_settings_store().load(), self.events,
            positions=self.positions, notify=self.notifications.notify,
            run_mode=self.settings.run_mode.value,
            csi_client=CsiClient.from_env(),
            entry_gate=self.licence_gate.refusal,
        )
        # Chute de BTC : nouvelles entrées automatiques suspendues (et stops en gain remontés si demandé).
        self.market_guard = MarketGuard(
            self.client.get_klines, self.client.get_price, lambda: get_settings_store().load(),
        )
        # Feu de protection CSI (GET /meteo, désactivé par défaut) : retient ou réduit les NOUVELLES entrées.
        self.csi_light = CsiLightGuard(CsiClient.from_env(), lambda: get_settings_store().load())
        self.daily_report = DailyReport(lambda: get_settings_store().load())

        self._running = True
        self._loop = 0
        self._resume_reconcile = True
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
        interrupted = self.commands.recover_interrupted(account_scope(self.settings))
        if interrupted:
            self.events.append(EventType.ERROR, f"{interrupted} commande(s) interrompue(s) : controle requis", level="CRITICAL")
        self._set_state(WorkerState.STARTING, "Worker demarre")
        self.events.append(
            EventType.WORKER_STARTED,
            f"Worker demarre (PID {os.getpid()}) — {self.settings.mode_label}",
        )
        self.telegram_poller.start()

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
        if hasattr(self, "telegram_poller"):
            self.telegram_poller.stop()
        self._set_state(WorkerState.STOPPED, "Worker arrete")
        self.events.append(EventType.WORKER_STOPPED, "Worker arrete")
        self.lock.release()
        if not self.settings.worker_managed_by_docker:
            # Sous Docker, une veille demandee survit au redemarrage du conteneur.
            BOT_STOP_FLAG.unlink(missing_ok=True)
        self.notifications.close()

    def _set_state(self, state: WorkerState, message: str = "", **fields) -> None:
        runtime = self.runtime_store.load()
        runtime.state = state
        runtime.pid = os.getpid()
        runtime.environment = self.settings.environment.value
        runtime.run_mode = self.settings.run_mode.value
        runtime.base_url = self.settings.base_url
        runtime.command_scope = account_scope(self.settings)
        runtime.command_capabilities = [
            "signal_v1", "independent_positions_v1", "market_close_v1",
            "telegram_getupdates_v1", "telegram_auto_execution_v1", "candle_stop_v1",
            "signal_drop_v1", "signal_drop_csi_v3", "signal_feedback_v2",
            "signal_routing_v1",
        ]
        runtime.last_message = message or runtime.last_message
        runtime.heartbeat_at = utcnow()
        if hasattr(self, "market_prices"):
            runtime.price_diagnostics = self.market_prices.snapshot()
            runtime.price_diagnostics["sources"] = getattr(self, "_price_sources", {})
        if hasattr(self, "telegram_poller"):
            runtime.telegram_diagnostics = self.telegram_poller.snapshot()
            if hasattr(self, "auto_signal_executor"):
                runtime.telegram_diagnostics["auto_execution"] = (
                    self.auto_signal_executor.snapshot()
                )
            if hasattr(self, "signal_drop"):
                runtime.telegram_diagnostics["drop"] = self.signal_drop.snapshot()
            if hasattr(self, "signal_feedback"):
                runtime.telegram_diagnostics["feedback"] = self.signal_feedback.snapshot()
        if runtime.started_at is None or state is WorkerState.STARTING:
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
                    if not self.settings.worker_managed_by_docker:
                        self.events.append(
                            EventType.WORKER_STOPPED, "Drapeau d'arret detecte"
                        )
                        break
                    self._standby()
                    time.sleep(1)
                    continue
                if self._in_standby:
                    self._leave_standby()
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

    def _standby(self) -> None:
        """Sous Docker, un arret propre met en veille : aucun suivi ni ordre.

        Le process reste vivant pour que la politique de redemarrage de Docker
        ne relance pas un worker arrete volontairement. Le heartbeat continue.
        """
        if not self._in_standby:
            self._in_standby = True
            self.events.append(EventType.WORKER_STOPPED, "Worker en veille (arret demande)")
        self._set_state(WorkerState.PAUSED, "En veille : arret demande depuis le Dashboard")

    def _leave_standby(self) -> None:
        """Relance apres une veille : rien n'a ete suivi pendant l'arret (achats remplis, stops
        executes), le premier tour reconcilie donc toutes les positions, comme un redemarrage."""
        self._in_standby = False
        self._resume_reconcile = True
        self._next_orphan_audit = 0
        self.events.append(EventType.WORKER_STARTED, "Worker relance depuis le Dashboard")

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

    def _check_licence(self) -> None:
        """Un evenement a chaque changement d'etat de la licence (pas a chaque tour)."""
        gate = getattr(self, "licence_gate", None)
        if gate is None:
            return
        refusal = gate.refusal()
        if refusal == getattr(self, "_licence_report", ""):
            return
        self._licence_report = refusal
        if refusal:
            self.events.append(EventType.ERROR, refusal, level="CRITICAL")
        else:
            self.events.append(EventType.POSITION_UPDATED, "Licence valide : nouvelles entrées autorisées", level="INFO")

    def _tick(self) -> int:
        self._check_licence()
        if hasattr(self, "fee_token_monitor"):
            self.fee_token_monitor.check()
        if hasattr(self, "signal_drop"):
            self.signal_drop.import_pending()
        if hasattr(self, "market_guard"):
            # Avant le routage : une chute constatée retient les signaux de ce même tour.
            self._check_market_guard()
        if hasattr(self, "csi_light"):
            # Avant le routage aussi : un feu rouge retient les signaux de ce même tour.
            self._check_csi_light()
        if hasattr(self, "daily_guard"):
            self._check_daily_loss()
        if hasattr(self, "manual_pause") and hasattr(self, "auto_signal_executor"):
            try:
                self.auto_signal_executor.manual_pause_reason = self.manual_pause.refusal()
            except Exception:  # noqa: BLE001 - fichier illisible : pause gardee par prudence
                logging.getLogger("bsm.worker").exception("Pause manuelle illisible")
                self.auto_signal_executor.manual_pause_reason = "Pause manuelle illisible : aucune entrée automatique"
        if hasattr(self, "auto_signal_executor"):
            # Toujours AVANT une mise en file automatique : au demarrage, aucun signal ne part
            # tant que les ordres ouverts du compte n'ont pas ete compares aux positions locales.
            if self._loop >= self._next_orphan_audit:
                self._audit_orphan_orders()
            try:
                self.auto_signal_executor.process_pending()
            except Exception as exc:  # noqa: BLE001 - le routage ne bloque jamais les TP/SL
                logging.getLogger("bsm.worker").exception("Routage des signaux interrompu")
                self.events.append(EventType.ERROR, f"Routage des signaux interrompu : {exc}", level="ERROR")
        if hasattr(self, "command_processor"):
            self.command_processor.run_one()
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

        # Reconciliation (section 19) : au premier tour (reprise apres un arret ou une veille : achats
        # remplis, stops executes pendant l'arret), puis tous les RECONCILE_EVERY tours ; entre deux,
        # les positions dont un achat attend son remplissage sont relues au plus une fois toutes les
        # AWAITING_FILL_RECONCILE_SECONDS, sinon le stop n'est pose que jusqu'a RECONCILE_EVERY tours
        # apres l'achat.
        # Ne pas reconcilier un objet potentiellement modifie par un cycle echoue.
        now = self._monotonic()
        if self._resume_reconcile or self._loop % RECONCILE_EVERY == 0:
            self._reconcile(processed)
            self._sync_quote_balance()
            self._resume_reconcile = False
            self._awaiting_reconciled_at = now  # les achats en attente viennent d'etre relus
        else:
            pending = [position for position in processed if awaiting_fill(position)]
            last = self._awaiting_reconciled_at
            if pending and (last is None or now - last >= AWAITING_FILL_RECONCILE_SECONDS):
                self._reconcile(pending)
                self._awaiting_reconciled_at = now

        # Retour d'execution des signaux V2 : d'apres l'etat sauvegarde de ce cycle.
        if hasattr(self, "signal_feedback"):
            self.signal_feedback.sync(processed)
        if hasattr(self, "daily_report"):
            self._send_daily_report()

        if errors:
            raise RuntimeError("Erreur de suivi : " + " ; ".join(errors))
        return len(positions)

    def _process_position(self, position, price: Optional[float]) -> None:
        """Un seul cycle par position ; aucune relance immediate en cas d'erreur."""
        from binance_spot_manager.market_close import poll_market_close
        was_open = position.is_open
        if position.status.value == "CLOSING" and poll_market_close(position, self.execution, price):
            self.positions.save(position)
            if was_open and not position.is_open:
                self._notify_finished(position)
            return
        if position.manual_exits and position.automation.paused:
            return
        if position.oco_exit is not None:
            self._monitor_oco(position, price)
            self.positions.save(position)
            return
        if price is None:
            return

        old_stop = position.stop_loss.resolved_price
        outcome = self.automation.run_cycle(position, price)
        self.positions.save(position)
        # Les evenements de trading partent meme si le cycle signale aussi une erreur.
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
                self.notifications.sl_moved(position, old_stop or 0.0, outcome.sl_moved_to),
            )
        if outcome.position_finished:
            self._notify_finished(position)
        if outcome.candle_stop_hit is not None and position.is_open:
            self._exit_on_candle_close(position, outcome.candle_stop_hit)
        elif outcome.stop_crossed_at is not None and position.is_open:
            self._exit_on_crossed_stop(position, outcome.stop_crossed_at)
        if outcome.errors:
            raise RuntimeError(" ; ".join(outcome.errors))

    def _entry_refusal(self) -> str:
        """Raison de refuser une NOUVELLE entrée (licence, pause, perte maximale du jour, feu CSI rouge réglé sur
        « manuelle ou automatique »), sinon « »."""
        refusal = self.licence_gate.refusal() if hasattr(self, "licence_gate") else ""
        if not refusal and hasattr(self, "manual_pause"):
            refusal = self.manual_pause.refusal()
        if not refusal and hasattr(self, "daily_guard"):
            refusal = self.daily_guard.refusal()
        if not refusal and hasattr(self, "csi_light"):
            refusal = self.csi_light.manual_refusal()        # feu CSI rouge réglé sur « manuelle ou automatique »
        return refusal

    def _status_text(self) -> str:
        """Réponse à /statut : état du worker, positions ouvertes, blocages en cours."""
        positions = self.positions.list_open()
        committed = sum(p.metrics.capital_committed for p in positions)
        latent = sum(p.pnl.unrealized for p in positions)
        blocks = [reason for reason in (
            self.licence_gate.refusal() if hasattr(self, "licence_gate") else "",
            self.manual_pause.refusal() if hasattr(self, "manual_pause") else "",
            self.daily_guard.refusal() if hasattr(self, "daily_guard") else "",
            self.market_guard.active_reason() if hasattr(self, "market_guard") else "",
            self.csi_light.auto_refusal() if hasattr(self, "csi_light") else "",
        ) if reason]
        lines = ["BinanceSpotManager (Binance Demo)",
                 f"Worker : {'en veille' if getattr(self, '_in_standby', False) else 'actif'}",
                 f"Positions ouvertes : {len(positions)} ; capital engagé {committed:.2f} USDT ; "
                 f"latent {latent:+.2f} USDT"]
        lines += [f"Blocage : {reason}" for reason in blocks] or ["Blocages : aucun (nouvelles entrées permises)"]
        if hasattr(self, "csi_light"):
            lines.append(self.csi_light.status_line())
        return "\n".join(lines)

    def _check_daily_loss(self) -> None:
        """Perte maximale du jour (au plus une mesure par minute) ; jamais bloquant pour le suivi."""
        from binance_spot_manager.fee_valuation import fee_rates
        from binance_spot_manager.models import utcnow

        log = logging.getLogger("bsm.worker")
        now = time.time()
        if now < getattr(self, "_next_daily_loss_check", 0.0):
            return
        self._next_daily_loss_check = now + DAILY_LOSS_CHECK_SECONDS
        events = []
        try:
            if not self.settings.dry_run and self.settings.has_credentials:
                balances = self.client.get_balances()
                quote = balances.get(self.settings.quote_asset, {})
                positions = self.positions.list_all()
                # Capital = USDT (libre + bloque) + valeur des cryptos encore detenues (quantite nette au dernier prix
                # connu, sinon au prix moyen) : apres un TP, le produit de la vente n'est plus compte deux fois.
                capital = float(quote.get("free") or 0) + float(quote.get("locked") or 0) + sum(
                    p.metrics.net_qty * (p.metrics.current_price or p.metrics.average_price)
                    for p in positions if p.is_open)
                prices: dict = {}

                def price_of(symbol):
                    if symbol not in prices:
                        try:
                            prices[symbol] = self.client.get_price(symbol)
                        except Exception:  # noqa: BLE001 - frais BNB alors non valorises
                            prices[symbol] = None
                    return prices[symbol]

                result = day_result(positions, utcnow(), lambda p: fee_rates(p, price_of))
                events = self.daily_guard.check(result, capital)
            else:
                events = self.daily_guard.check(0.0, 0.0)          # seulement la fin d'un blocage
        except Exception:  # noqa: BLE001 - mesure impossible : aucun blocage nouveau, aucun leve
            log.exception("Perte maximale du jour : mesure interrompue")
        if hasattr(self, "auto_signal_executor"):
            self.auto_signal_executor.daily_guard_reason = self.daily_guard.refusal()
        for kind, detail in events:
            started = kind == "STARTED"
            self.events.append(EventType.DAILY_LOSS_STARTED if started else EventType.DAILY_LOSS_ENDED, detail,
                               level="CRITICAL" if started else "INFO")
            self.notifications.notify(self.notifications.daily_loss(kind, detail))

    def _check_csi_light(self) -> None:
        """Feu de protection CSI : jamais bloquant pour le suivi des positions (rien n'est vendu ni annulé)."""
        log = logging.getLogger("bsm.worker")
        try:
            changes = self.csi_light.check()
            effect = self.csi_light.effect()
        except Exception:  # noqa: BLE001 - etat illisible : le feu ne bloque rien
            log.exception("Feu CSI : contrôle interrompu")
            return
        if hasattr(self, "auto_signal_executor"):
            self.auto_signal_executor.csi_light_reason = effect.detail if effect.block_auto else ""
            self.auto_signal_executor.csi_light_kept_percent = effect.kept_percent
            self.auto_signal_executor.csi_light_detail = effect.detail
        for kind, detail in changes:
            started = kind == "STARTED"
            self.events.append(
                EventType.CSI_LIGHT_STARTED if started else EventType.CSI_LIGHT_ENDED, detail,
                level="WARNING" if started else "INFO",
            )
            self.notifications.notify(self.notifications.csi_light(kind, detail))

    def _check_market_guard(self) -> None:
        """Protection en cas de chute de BTC : jamais bloquante pour le suivi des positions."""
        log = logging.getLogger("bsm.worker")
        try:
            changes = self.market_guard.check()
            reason = self.market_guard.active_reason()
        except Exception:  # noqa: BLE001 - etat illisible : la protection ne bloque rien
            log.exception("Protection marché : contrôle interrompu")
            return
        if hasattr(self, "auto_signal_executor"):
            self.auto_signal_executor.market_guard_reason = reason
        for kind, detail in changes:
            started = kind == "STARTED"
            self.events.append(
                EventType.MARKET_GUARD_STARTED if started else EventType.MARKET_GUARD_ENDED, detail,
                level="WARNING" if started else "INFO",
            )
            self.notifications.notify(self.notifications.market_guard(kind, detail))
            if started and self.market_guard.state().get("tighten_stops"):
                self._tighten_stops()

    def _tighten_stops(self) -> None:
        """Protection marche : stop des positions en gain remonte a leur seuil de rentabilite (frais
        compris). Jamais a la baisse, jamais une position dont un achat attend encore son execution."""
        log = logging.getLogger("bsm.worker")
        positions = [p for p in self.positions.list_open()
                     if p.metrics.net_qty > 0 and not p.open_entries and p.metrics.break_even_with_fees > 0]
        if not positions:
            return
        try:
            prices = self.client.get_prices([p.symbol for p in positions])
        except Exception as exc:  # noqa: BLE001 - sans prix, aucun stop n'est touche
            log.warning("Protection marché : prix indisponibles, stops inchangés (%s)", exc)
            return
        for position in positions:
            break_even = position.metrics.break_even_with_fees
            price = prices.get(position.symbol)
            old_stop = position.stop_loss.resolved_price or 0.0
            if price is None or price <= break_even or old_stop >= break_even:
                continue
            try:
                outcome = self.automation.raise_stop(position, break_even, why="(protection marché)")
            except Exception:  # noqa: BLE001 - une position en echec n'empeche pas les autres
                log.exception("Protection marché : stop non remonté pour %s", position.symbol)
                continue
            self.positions.save(position)
            if outcome.sl_moved_to is not None and position.notifications.on_sl_moved:
                self.notifications.notify_position_event(
                    position, self.notifications.sl_moved(position, old_stop, outcome.sl_moved_to)
                )
            for error in outcome.errors:
                self.events.append(EventType.ERROR, f"Protection marché, {position.symbol} : {error}",
                                   position_id=position.position_id, symbol=position.symbol, level="ERROR")

    def _send_daily_report(self) -> None:
        """Rapport quotidien (Settings → Notifications) ; un echec est reessaye 10 minutes plus tard."""
        from binance_spot_manager.daily_report import build
        from binance_spot_manager.fee_valuation import fee_rates

        if time.time() < getattr(self, "_daily_report_retry_at", 0.0):
            return
        try:
            moment = self.daily_report.due()
            if moment is None:
                return
            prices: dict[str, Optional[float]] = {}

            def price_of(symbol: str) -> Optional[float]:
                if symbol not in prices:
                    try:
                        prices[symbol] = self.client.get_price(symbol)
                    except Exception:  # noqa: BLE001 - frais BNB alors non valorises
                        prices[symbol] = None
                return prices[symbol]

            title, body = build(self.positions.list_all(), moment, lambda p: fee_rates(p, price_of),
                                channel=self._channel_resolver())
            self.notifications.notify(self.notifications.daily_report(title, body))
            self.events.append(EventType.DAILY_REPORT_SENT, title, level="INFO")
            self.daily_report.mark_sent(moment)
        except Exception:  # noqa: BLE001 - le rapport ne bloque jamais le suivi
            self._daily_report_retry_at = time.time() + 600
            logging.getLogger("bsm.worker").exception("Rapport quotidien non envoyé")

    def _channel_resolver(self):
        """Trader ou canal d'une position ; pour une position ouverte avant le suivi par canal, le nom
        ecrit en tete du texte du signal (boite des signaux)."""
        from binance_spot_manager.performance import channel_of, channel_resolver

        if not hasattr(self, "signal_inbox"):
            return channel_of
        scope = account_scope(self.settings)

        def raw_text(signal_id: str) -> str:
            row = self.signal_inbox.get(scope, signal_id)
            return str(row.get("raw") or "") if row else ""

        return channel_resolver(raw_text)

    def _notify_finished(self, position) -> None:
        """Fin de position (gain ou perte) avec le PnL frais compris de la page History."""
        from binance_spot_manager.fee_valuation import fee_rates

        try:
            rates = fee_rates(position, self.client.get_price)
        except Exception:  # noqa: BLE001 - message envoye quand meme, frais BNB signales non valorises
            rates = {}
        self.notifications.notify_position_event(
            position, self.notifications.position_finished(position, fee_rates=rates)
        )

    def _exit_on_crossed_stop(self, position, stop_price: float) -> None:
        """Binance refuse le SL car le prix a deja franchi le stop : sortir ou mettre en pause.

        Le prix est relu en REST avant toute vente. La vente passe par la cloture
        au marche : intention persistee avant l'envoi, annulations confirmees,
        quantite nette de cette position uniquement, aucun renvoi automatique.
        """
        from binance_spot_manager.market_close import close_market

        saved = get_settings_store().load()
        if isinstance(saved, dict) and not saved.get("exit_on_crossed_stop", True):
            self._pause_on_crossed_stop(
                position, stop_price, "Sortie automatique desactivee dans Settings"
            )
            return
        try:
            price = self.client.get_price(position.symbol)
        except Exception as exc:  # noqa: BLE001 - sans prix frais, aucune vente
            logging.getLogger("bsm.worker").warning(
                "Prix REST indisponible pour %s : sortie reportee (%s)", position.symbol, exc,
            )
            return
        if price > stop_price:
            # Le prix est repasse au-dessus du stop : le SL sera recree au prochain cycle.
            return

        self.events.append(
            EventType.SL_EXECUTED,
            f"Stop franchi ({position.symbol}) : prix {price} <= stop {stop_price}, vente au marche",
            position_id=position.position_id, symbol=position.symbol, level="WARNING",
        )
        try:
            result = close_market(
                position, self.execution, self.positions, reason=CloseReason.STOP_CROSSED,
            )
        except Exception as exc:  # noqa: BLE001 - issue incertaine : pause, jamais de renvoi
            self._pause_on_crossed_stop(position, stop_price, f"Sortie au marche impossible : {exc}")
            return
        self.notifications.notify_position_event(
            position,
            self.notifications.stop_crossed_exit(
                position, stop_price, price, result.get("message", ""),
            ),
        )
        if not position.is_open:
            self._notify_finished(position)

    def _exit_on_candle_close(self, position, breach) -> None:
        """Une bougie a cloture au SL ou dessous : vendre au marche la quantite de cette position.

        La cloture fait foi : le prix n'est pas relu, un rebond apres la cloture ne l'annule pas.
        Meme chemin que la cloture manuelle (intention persistee, aucun renvoi automatique).
        """
        from binance_spot_manager.market_close import close_market

        stop_price = position.stop_loss.resolved_price or 0.0
        interval = position.stop_loss.candle_interval
        saved = get_settings_store().load()
        if isinstance(saved, dict) and not saved.get("exit_on_crossed_stop", True):
            self._pause_on_crossed_stop(
                position, stop_price,
                f"Bougie {interval} cloturee a {breach.close_price} ; sortie automatique desactivee dans Settings",
            )
            return
        self.events.append(
            EventType.SL_EXECUTED,
            f"SL a la cloture ({position.symbol}) : bougie {interval} cloturee a {breach.close_price} "
            f"<= stop {stop_price}, vente au marche",
            position_id=position.position_id, symbol=position.symbol, level="WARNING",
        )
        try:
            result = close_market(
                position, self.execution, self.positions, reason=CloseReason.SL_CANDLE_CLOSE,
            )
        except Exception as exc:  # noqa: BLE001 - issue incertaine : pause, jamais de renvoi
            self._pause_on_crossed_stop(position, stop_price, f"Sortie au marche impossible : {exc}")
            return
        self.notifications.notify_position_event(
            position,
            self.notifications.candle_stop_exit(
                position, stop_price, interval, breach.close_price, result.get("message", ""),
            ),
        )
        if not position.is_open:
            self._notify_finished(position)

    def _pause_on_crossed_stop(self, position, stop_price: float, reason: str) -> None:
        position.automation.paused = True
        self.positions.save(position)
        self.events.append(
            EventType.ERROR,
            f"Stop franchi ({position.symbol}) : {reason}. Position en pause, sans protection",
            position_id=position.position_id, symbol=position.symbol, level="CRITICAL",
        )
        self.notifications.notify_position_event(
            position, self.notifications.stop_crossed_paused(position, stop_price, reason),
        )

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
                self._notify_finished(position)
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
                # Un achat execute, constate a la relecture des ordres, est le chemin NORMAL du worker :
                # annonce « Entry N remplie » (prix, quantite, montant, prochain TP, SL), pas
                # « Desynchronisation ». Les autres constats restent des desynchronisations.
                others = []
                fills_announced = False
                for finding in report.findings:
                    entry = next((e for e in position.entries if e.entry_id == finding.target_id), None)
                    if finding.kind == "FILL_APPLIED" and entry is not None:
                        if not fills_announced:
                            recompute_position(position)
                            fills_announced = True
                        self.notifications.notify_position_event(
                            position, self.notifications.entry_filled(position, entry)
                        )
                    else:
                        others.append(finding.message)
                if others:
                    self.notifications.notify_position_event(
                        position, self.notifications.desync(position, others)
                    )
            if report.has_desync or position.sync_status != previous_sync_status:
                self.positions.save(position)

    def _audit_orphan_orders(self) -> None:
        """Ordres « BSM-… » ouverts chez Binance qu'aucune position locale ne connait (lacune L2).

        Ils viennent d'un second worker sur la meme cle, d'un stockage perdu ou restaure, ou d'un
        arret entre l'envoi et l'enregistrement. Tant qu'il en existe, ou que le controle est
        impossible, l'execution AUTOMATIQUE des signaux est suspendue (echec sur) ; le suivi des
        positions et les commandes confirmees dans l'interface continuent. Rien n'est annule.
        """
        executor = self.auto_signal_executor
        try:
            open_orders = self.execution.get_open_orders()  # toutes les paires du compte
            orphans = find_orphan_bot_orders(open_orders, self.positions.list_all())
        except Exception as exc:  # noqa: BLE001 - echec sur, le worker continue
            self._next_orphan_audit = self._loop + RECONCILE_EVERY
            executor.suspend(f"Contrôle des ordres orphelins impossible : {exc}")
            if self._orphan_report != "ECHEC":
                self._orphan_report = "ECHEC"
                self.events.append(
                    EventType.ERROR,
                    f"Contrôle des ordres orphelins impossible ({exc}) : exécution automatique des "
                    "signaux suspendue jusqu'au prochain contrôle réussi",
                    level="WARNING",
                )
            return
        self._next_orphan_audit = self._loop + ORPHAN_AUDIT_EVERY
        if not orphans:
            if executor.suspended_reason:
                self.events.append(
                    EventType.POSITION_UPDATED,
                    "Aucun ordre BSM orphelin chez Binance : exécution automatique des signaux rétablie",
                    level="INFO",
                )
            executor.resume()
            self._orphan_report = None
            return
        found = frozenset((str(o.get("symbol")), str(o.get("clientOrderId"))) for o in orphans)
        listing = ", ".join(f"{symbol} {client_id}" for symbol, client_id in sorted(found))
        executor.suspend(f"{len(found)} ordre(s) BSM ouvert(s) chez Binance sans position locale : {listing}")
        if self._orphan_report != found:
            self._orphan_report = found
            self.events.append(
                EventType.DESYNC_DETECTED,
                f"Ordre(s) BSM orphelin(s) chez Binance : {listing}. Exécution automatique des "
                "signaux suspendue, suivi des positions maintenu. Vérifier un second worker sur la "
                "même clé ou un stockage restauré ; aucun ordre n'est annulé automatiquement.",
                level="CRITICAL",
                orders=[
                    {"symbol": o.get("symbol"), "order_id": o.get("orderId"),
                     "client_order_id": o.get("clientOrderId"), "side": o.get("side"),
                     "type": o.get("type")}
                    for o in orphans
                ],
            )

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
