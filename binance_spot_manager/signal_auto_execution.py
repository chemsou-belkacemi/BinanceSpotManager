"""Prepare fresh Telegram or drop signals, route them, and enqueue only the AUTO ones.

Routage (signal_routing) : un signal part automatiquement vers le worker Demo
seulement s'il n'a AUCUN motif de revue (confiance déclarée, risque modéré,
données mesurables). Sinon il passe « À confirmer » (auto_state REVIEW) avec ses
motifs, et le propriétaire le confirme ou non dans la page Signaux. REJECTED est
réservé aux signaux que personne ne peut exécuter (contrat violé).
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time

from . import signal_routing
from .models import EventType
from .notification_engine import Notification
from .risk_engine import RiskLimits
from .signal_parser import ParsedSignal
from .signal_plan import prepare_signal, signal_sl_after_tp
from .signal_routing import (
    AUTO, CONFIANCE, DONNEES, EARLY_REVIEW_CODES, KIND_CSI, REVIEW, Reason, RouteDecision,
    RoutingDataError, RoutingPolicy,
)
from .signal_sizing import SignalSizingPolicy, suggest_signal_budget_from_account

logger = logging.getLogger("bsm.signal_auto")

#: Libellés des événements et diagnostics selon l'origine du signal.
SOURCE_LABELS = {"telegram": "Signal Telegram", "api": "Signal ML/dépôt"}
MESSAGE_LABELS = {"telegram": "Message Telegram", "api": "Signal ML/dépôt"}


def _bounded_number(values, key, default, minimum, maximum):
    try:
        value = float(values.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_deviation_bps(price: float, entry_price: float) -> float:
    """Écart absolu du prix courant à ENTRY_1, en points de base."""
    return abs(price / entry_price - 1.0) * 10_000.0


class RejectSignal(ValueError):
    """Contrat violé : personne ne peut exécuter ce signal (REJECTED, jamais « À confirmer »)."""


class AutomaticSignalExecutor:
    """Turns explicitly authorized, fresh inbox rows into durable commands, or into reviews.

    Binance writes are never made here. The existing CommandProcessor performs
    the final price, balance, fee and portfolio-risk checks on the worker thread.
    """

    def __init__(self, scope, inbox, commands, client, rules_cache, risk_limits,
                 preferences_loader, events, *, clock=time.time, positions=None,
                 notify=None, run_mode=None):
        self.scope = scope
        self.inbox = inbox
        self.commands = commands
        self.client = client
        self.rules_cache = rules_cache
        self.risk_limits = risk_limits
        self.preferences_loader = preferences_loader
        self.events = events
        self.clock = clock
        #: Positions locales (PositionStore) : sans elles, le risque n'est pas évaluable.
        self.positions = positions
        #: Notification distante des signaux « À confirmer » (NotificationEngine.notify).
        self.notify = notify
        #: Mode d'exécution du worker (DRY_RUN, DEMO_MANUAL, DEMO_AUTO) ; inconnu = manuel.
        self.run_mode = getattr(run_mode, "value", run_mode)
        self._new_enqueue = False
        self._diagnostics = {
            "state": "DISABLED",
            "queued_total": 0,
            "rejected_total": 0,
            "review_total": 0,
            "last_signal_id": "",
            "last_detail": "",
            "last_reasons": [],
            "last_processed_at": None,
        }

    def snapshot(self):
        return dict(self._diagnostics)

    def _update(self, **values):
        self._diagnostics.update(values)

    def _limits(self) -> RiskLimits:
        limits = self.risk_limits()
        return limits if isinstance(limits, RiskLimits) else RiskLimits(
            min_reserve_percent=float(getattr(limits, "min_reserve_percent", 20.0)))

    def process_pending(self, limit=3):
        preferences = self.preferences_loader()
        preferences = preferences if isinstance(preferences, dict) else {}
        if not preferences.get("signal_auto_execute_enabled", False):
            self._update(state="DISABLED", last_detail="")
            return []
        telegram_ready = bool(preferences.get("signal_telegram_enabled", False)
                              and preferences.get("signal_telegram_auto_enabled", False))
        drop_ready = bool(preferences.get("signal_drop_enabled", False)
                          and preferences.get("signal_drop_auto_enabled", False))
        if not (telegram_ready or drop_ready):
            self._update(
                state="MISCONFIGURED",
                last_detail="Activer la relève Telegram automatique ou l'exécution des signaux déposés.",
            )
            return []
        enabled_since = _bounded_number(
            preferences, "signal_auto_execute_enabled_since", 0, 0, self.clock() + 60,
        )
        if enabled_since <= 0:
            self._update(
                state="WAITING_AUTHORIZATION",
                last_detail="Réenregistrer l'autorisation d'exécution automatique.",
            )
            return []
        sources = {}
        if telegram_ready:
            sources["telegram"] = enabled_since
        if drop_ready:
            # Autorisation séparée : seuls les dépôts reçus après elle sont éligibles.
            drop_since = _bounded_number(
                preferences, "signal_drop_auto_enabled_since", 0, 0, self.clock() + 60,
            )
            if drop_since > 0:
                sources["api"] = drop_since
            elif not telegram_ready:
                self._update(
                    state="WAITING_AUTHORIZATION",
                    last_detail="Réenregistrer l'autorisation d'exécution des signaux ML/dépôt.",
                )
                return []
        max_age_minutes = _bounded_number(
            preferences, "signal_auto_max_age_minutes", 5, 1, 60,
        )
        policy = RoutingPolicy.from_mapping(preferences, self._limits())
        now = self.clock()
        oldest = now - max_age_minutes * 60
        # Pré-passe sans appel Binance : un message trop ancien reçu après l'autorisation
        # n'est plus ignoré en silence, il passe « À confirmer » avec son motif.
        for row in self.inbox.stale_rows_for_review(
                self.scope, enabled_since=enabled_since, oldest_source_timestamp=oldest,
                now=now, sources=sources):
            self._review_safely(row, signal_routing.decide(
                signal_routing.confidence_reasons(row, policy, now=now, run_mode=self.run_mode)
                or [Reason("C_STALE", CONFIANCE, "Message trop ancien pour une exécution automatique")]),
                now=now, policy=policy)
        rows = self.inbox.auto_candidates(
            self.scope,
            enabled_since=enabled_since,
            oldest_source_timestamp=oldest,
            now=now,
            limit=limit,
            sources=sources,
        )
        processed = []
        for row in rows:
            self._new_enqueue = False
            processed.append(self._process(row, preferences, now, policy))
            if self._new_enqueue:
                # Une seule nouvelle commande AUTO par cycle : le worker la traite dans
                # ce même cycle, la ligne suivante verra le portefeuille à jour.
                break
        if not rows:
            self._update(state="ARMED", last_detail="")
        return processed

    def _process(self, row, preferences, now, policy):
        signal_id = row["id"]
        source = "api" if row.get("source") == "api" else "telegram"
        label = SOURCE_LABELS[source]
        request_key = f"signal:{signal_id}"
        try:
            existing = self.commands.get_by_request_key(self.scope, request_key)
            if existing:
                self.inbox.set_auto_state(
                    self.scope, signal_id, "QUEUED", f"Commande {existing['id']} : {existing['state']}",
                )
                self._update(state="QUEUED", last_signal_id=signal_id,
                             last_detail=f"Commande {existing['id']}", last_processed_at=now)
                return "QUEUED"
            valid_from = float(row["parsed"].get("valid_from") or 0)
            if (row["parsed"].get("signal_version") == 3 and not row["parsed"].get("errors")
                    and now < valid_from):
                # Pas encore valide : la ligne reste disponible, sans être réclamée.
                self._update(state="ARMED", last_signal_id=signal_id,
                             last_detail=f"Signal CSI en attente de VALID_FROM ({_iso_utc(valid_from)})")
                return "WAITING"
            if not self.inbox.claim_auto(self.scope, signal_id):
                return "SKIPPED"
        except Exception:  # noqa: BLE001 - base indisponible : réessai au cycle suivant
            logger.exception("Routage du signal %s : lecture impossible", signal_id)
            self._update(state="ERROR", last_signal_id=signal_id,
                         last_detail="Boîte des signaux indisponible : nouvel essai au prochain cycle")
            return "ERROR"
        try:
            return self._route(row, preferences, now, policy, source, label, request_key)
        except RejectSignal as exc:
            return self._reject(row, str(exc) or "Signal automatique refusé", now, label)
        except Exception as exc:  # noqa: BLE001 - jamais AUTO sur erreur : revue
            logger.exception("Routage du signal %s interrompu", signal_id)
            decision = RouteDecision(REVIEW, [Reason("D_ROUTER_ERROR", DONNEES, f"Erreur de routage : {exc}")])
            return self._review_safely(row, decision, now=now, policy=policy, label=label)

    def _route(self, row, preferences, now, policy, source, label, request_key):
        signal_id = row["id"]
        if row["parsed"].get("errors"):
            raise RejectSignal("Signal non reconnu ou bloqué par le parseur")
        parsed = ParsedSignal(**row["parsed"])
        if parsed.signal_version not in {1, 3}:
            raise RejectSignal(f"Contrat CSI version {parsed.signal_version} retiré : aucune exécution")
        if parsed.is_csi:
            # Double sécurité : seul le dépôt TXT garantit l'idempotence d'un signal CSI.
            if not str(row.get("external_id") or "").startswith("csi:"):
                raise RejectSignal("Signal CSI hors dépôt TXT (identifiant externe csi: absent) : aucune exécution")
            # Contrat CSI : VALID_FROM <= maintenant < EXPIRES_AT remplace la fenêtre d'âge.
            if now >= parsed.expires_at:
                raise RejectSignal(f"Signal CSI expiré (EXPIRES_AT {_iso_utc(parsed.expires_at)}) : aucune exécution")
        if row.get("payload"):
            # Reprise après arrêt : une préparation déjà gelée n'est jamais recalculée.
            payload = row["payload"]
            if float(payload.get("signal_confirmation_expires_at") or 0) <= now:
                raise RejectSignal("Préparation automatique expirée avant sa mise en file")
            return self._enqueue(row, payload, parsed, now, label, request_key)

        reasons = signal_routing.confidence_reasons(row, policy, now=now, run_mode=self.run_mode)
        if any(reason.code in EARLY_REVIEW_CODES for reason in reasons):
            return self._review(row, signal_routing.decide(reasons), now=now, policy=policy, label=label)
        try:
            rules = self.rules_cache.get(parsed.symbol, refresh=True)
            balances = self.client.get_balances()
            prices = self.client.get_prices()
            current_price = self.client.get_price(parsed.symbol)
        except Exception as exc:  # noqa: BLE001 - erreur transitoire : revue, jamais refus définitif
            reasons.append(Reason("D_UNAVAILABLE", DONNEES, f"Données Binance indisponibles : {exc}"))
            return self._review(row, signal_routing.decide(reasons), now=now, policy=policy, label=label)
        try:
            suggestion, missing = suggest_signal_budget_from_account(
                SignalSizingPolicy.from_mapping(preferences),
                balances=balances, prices=prices, quote_asset=rules.quote_asset,
                reserve_percent=self._limits().min_reserve_percent,
            )
        except ValueError as exc:
            suggestion, missing = None, (str(exc),)
        if missing:
            reasons.append(Reason("D_VALUATION", DONNEES,
                                  "Valorisation du portefeuille incomplète : " + ", ".join(missing)))
            return self._review(row, signal_routing.decide(reasons), now=now, policy=policy, label=label)
        if suggestion.budget <= 0:
            reasons.append(Reason("D_NO_BUDGET", DONNEES, "Budget automatique nul après application de la réserve"))
            return self._review(row, signal_routing.decide(reasons), now=now, policy=policy, label=label)
        if parsed.is_csi:
            deviation = entry_deviation_bps(current_price, parsed.entries[0])
            if deviation > parsed.max_entry_deviation_bps:
                raise RejectSignal(
                    f"Écart de prix {deviation:.1f} bps > MAX_ENTRY_DEVIATION_BPS "
                    f"{parsed.max_entry_deviation_bps:g} (prix {current_price}, ENTRY_1 {parsed.entries[0]})"
                )
        try:
            # CSI : prepare_signal remplace la règle enregistrée par EXIT_POLICY_ID. Un SL sur
            # clôture est interprété au toucher seulement pour chiffrer le risque : le motif
            # C_SL_CANDLE interdit alors tout envoi automatique.
            plan, payload = prepare_signal(
                parsed, rules,
                budget=suggestion.budget,
                available_quote=float(balances.get(rules.quote_asset, {}).get("free") or 0),
                reserve_percent=self._limits().min_reserve_percent,
                current_price=current_price,
                signal_id=signal_id,
                source=source,
                sl_after_tp=signal_sl_after_tp(preferences.get("signal_sl_after_tp")),
                touch_stop=bool(policy.touch_stop or parsed.stop_timeframe),
                validity_confirmed=True,
            )
        except ValueError as exc:
            reasons.append(Reason("D_PLAN", DONNEES, f"Plan refusé à ce budget : {exc}"))
            return self._review(row, signal_routing.decide(reasons), now=now, policy=policy, label=label)
        metrics = {"budget": suggestion.budget, "current_price": current_price}
        try:
            if self.positions is None:
                raise RoutingDataError("positions locales non transmises au routage")
            positions = self.positions.list_all()
            if getattr(self.positions, "read_errors", None):
                raise RoutingDataError("stockage des positions illisible")
            active = self.commands.active(self.scope, "SUBMIT_POSITION")
            risk_reasons, risk_metrics = signal_routing.assess_risk(
                kind=signal_routing.signal_kind(row), payload=payload,
                plan_average_price=plan.estimated_average_price, current_price=current_price,
                balances=balances, prices=prices, positions=positions, active_commands=active,
                limits=self._limits(), policy=policy, raw=row.get("raw") or "",
            )
            breaker, breaker_metrics = signal_routing.breaker_reasons(
                policy=policy, now=now, auto_commands=self.commands.auto_commands(self.scope),
                positions=positions, prices=prices, total_capital_usdt=risk_metrics["total_capital_usdt"],
            )
        except RoutingDataError as exc:
            reasons.append(Reason("D_RISK", DONNEES, f"Risque non évaluable : {exc}"))
            return self._review(row, signal_routing.decide(reasons, metrics), now=now, policy=policy, label=label)
        reasons += risk_reasons + breaker
        metrics |= risk_metrics | breaker_metrics
        decision = signal_routing.decide(reasons, metrics)
        if decision.outcome != AUTO:
            return self._review(row, decision, now=now, policy=policy, label=label)
        payload = dict(payload)
        payload["confirmation_mode"] = "AUTO"
        payload["route"] = {"version": signal_routing.ROUTING_POLICY_VERSION, "decision": AUTO,
                            "metrics": signal_routing.clean_metrics(metrics)}
        if parsed.is_csi:
            payload["signal_validation_status"] = parsed.validation_status
        payload = self.inbox.freeze(self.scope, signal_id, payload)
        return self._enqueue(row, payload, parsed, now, label, request_key)

    def _enqueue(self, row, payload, parsed, now, label, request_key):
        signal_id = row["id"]
        command = self.commands.enqueue(
            self.scope, "SUBMIT_POSITION", payload,
            request_key=request_key, ttl=120,
        )
        self._new_enqueue = True
        position = payload["position"]
        detail = f"Commande {command['id']} mise en file"
        self.inbox.set_auto_state(self.scope, signal_id, "QUEUED", detail)
        self.events.append(
            EventType.SIGNAL_AUTO_QUEUED,
            f"{label} envoyé automatiquement au worker : {parsed.symbol}",
            position_id=position["position_id"], symbol=parsed.symbol,
            signal_id=signal_id, command_id=command["id"],
        )
        self._update(
            state="QUEUED",
            queued_total=self._diagnostics["queued_total"] + 1,
            last_signal_id=signal_id,
            last_detail=detail,
            last_reasons=[],
            last_processed_at=now,
        )
        return "QUEUED"

    def _review(self, row, decision, *, now, policy, label=None):
        """« À confirmer » : motifs enregistrés, payload jamais gelé, alerte et push optionnel."""
        signal_id = row["id"]
        label = label or SOURCE_LABELS["api" if row.get("source") == "api" else "telegram"]
        decision = RouteDecision(REVIEW, decision.reasons, decision.metrics)
        detail = decision.summary_fr()
        self.inbox.set_auto_state(self.scope, signal_id, "REVIEW", detail, route=decision.to_json())
        symbol = row["parsed"].get("symbol", "")
        self.events.append(
            EventType.SIGNAL_REVIEW_REQUIRED,
            f"{label} à confirmer : {symbol or 'non reconnu'} · {detail}",
            symbol=symbol, level="WARNING", signal_id=signal_id, reason_codes=decision.codes,
        )
        if policy.notify_review and self.notify is not None:
            try:
                kind = signal_routing.signal_kind(row)
                act_before = float(row["parsed"].get("expires_at") or 0) if kind == KIND_CSI else None
                title, body = signal_routing.review_notification_text(
                    symbol=symbol, kind=kind, reasons=decision.reasons, act_before=act_before or None)
                self.notify(Notification(event="SIGNAL_REVIEW", title=title, body=body, level="WARNING",
                                         position_id=signal_id, symbol=symbol))
            except Exception:  # noqa: BLE001 - une notification ne change jamais la décision
                logger.exception("Notification « à confirmer » non envoyée pour %s", signal_id)
        self._update(
            state="REVIEW",
            review_total=self._diagnostics["review_total"] + 1,
            last_signal_id=signal_id,
            last_detail=detail,
            last_reasons=decision.codes,
            last_processed_at=now,
        )
        return "REVIEW"

    def _review_safely(self, row, decision, *, now, policy, label=None):
        try:
            return self._review(row, decision, now=now, policy=policy, label=label)
        except Exception:  # noqa: BLE001 - base indisponible : la ligne reste en l'état
            logger.exception("Signal %s : mise en revue impossible", row.get("id"))
            self._update(state="ERROR", last_detail="Mise en revue impossible : nouvel essai au prochain cycle")
            return "ERROR"

    def _reject(self, row, detail, now, label):
        signal_id = row["id"]
        try:
            self.inbox.set_auto_state(self.scope, signal_id, "REJECTED", detail)
        except Exception:  # noqa: BLE001
            logger.exception("Signal %s : refus non enregistré", signal_id)
            return "ERROR"
        symbol = row["parsed"].get("symbol", "")
        self.events.append(
            EventType.SIGNAL_AUTO_REJECTED,
            f"{label} automatique refusé : {symbol or 'non reconnu'} · {detail}",
            symbol=symbol, level="WARNING", signal_id=signal_id,
        )
        self._update(
            state="REJECTED",
            rejected_total=self._diagnostics["rejected_total"] + 1,
            last_signal_id=signal_id,
            last_detail=detail,
            last_reasons=[],
            last_processed_at=now,
        )
        return "REJECTED"
