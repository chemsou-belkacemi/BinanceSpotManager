"""Prepare fresh Telegram signals and enqueue them for the guarded worker."""

from __future__ import annotations

from datetime import datetime
import time

from .models import EventType
from .signal_parser import ParsedSignal
from .signal_plan import prepare_signal
from .signal_sizing import SignalSizingPolicy, suggest_signal_budget_from_account


def _bounded_number(values, key, default, minimum, maximum):
    try:
        value = float(values.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


class AutomaticSignalExecutor:
    """Turns explicitly authorized, fresh inbox rows into durable commands.

    Binance writes are never made here. The existing CommandProcessor performs
    the final price, balance, fee and portfolio-risk checks on the worker thread.
    """

    def __init__(self, scope, inbox, commands, client, rules_cache, risk_limits,
                 preferences_loader, events, *, clock=time.time):
        self.scope = scope
        self.inbox = inbox
        self.commands = commands
        self.client = client
        self.rules_cache = rules_cache
        self.risk_limits = risk_limits
        self.preferences_loader = preferences_loader
        self.events = events
        self.clock = clock
        self._diagnostics = {
            "state": "DISABLED",
            "queued_total": 0,
            "rejected_total": 0,
            "last_signal_id": "",
            "last_detail": "",
            "last_processed_at": None,
        }

    def snapshot(self):
        return dict(self._diagnostics)

    def _update(self, **values):
        self._diagnostics.update(values)

    def process_pending(self, limit=3):
        preferences = self.preferences_loader()
        preferences = preferences if isinstance(preferences, dict) else {}
        if not preferences.get("signal_auto_execute_enabled", False):
            self._update(state="DISABLED", last_detail="")
            return []
        if not (preferences.get("signal_telegram_enabled", False)
                and preferences.get("signal_telegram_auto_enabled", False)):
            self._update(
                state="MISCONFIGURED",
                last_detail="La réception Telegram automatique doit être active.",
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
        max_age_minutes = _bounded_number(
            preferences, "signal_auto_max_age_minutes", 5, 1, 60,
        )
        now = self.clock()
        rows = self.inbox.auto_candidates(
            self.scope,
            enabled_since=enabled_since,
            oldest_source_timestamp=now - max_age_minutes * 60,
            now=now,
            limit=limit,
        )
        processed = []
        for row in rows:
            processed.append(self._process(row, preferences, now, max_age_minutes))
        if not rows:
            self._update(state="ARMED", last_detail="")
        return processed

    def _process(self, row, preferences, now, max_age_minutes):
        signal_id = row["id"]
        request_key = f"signal:{signal_id}"
        existing = self.commands.get_by_request_key(self.scope, request_key)
        if existing:
            self.inbox.set_auto_state(
                self.scope, signal_id, "QUEUED", f"Commande {existing['id']} : {existing['state']}",
            )
            self._update(state="QUEUED", last_signal_id=signal_id,
                         last_detail=f"Commande {existing['id']}", last_processed_at=now)
            return "QUEUED"
        if not self.inbox.claim_auto(self.scope, signal_id):
            return "SKIPPED"
        try:
            if row["parsed"].get("errors"):
                raise ValueError("Signal non reconnu ou bloqué par le parseur")
            source_timestamp = float(row.get("source_timestamp") or 0)
            if source_timestamp <= 0 or source_timestamp < now - max_age_minutes * 60:
                raise ValueError("Message Telegram trop ancien pour une exécution automatique")
            parsed = ParsedSignal(**row["parsed"])
            if parsed.published_at:
                published = datetime.fromisoformat(parsed.published_at).timestamp()
                if published < now - max_age_minutes * 60 or published > now + 60:
                    raise ValueError("Date contenue dans le signal hors de la fenêtre autorisée")
            if row.get("payload"):
                payload = row["payload"]
                if float(payload.get("signal_confirmation_expires_at") or 0) <= now:
                    raise ValueError("Préparation automatique expirée avant sa mise en file")
            else:
                rules = self.rules_cache.get(parsed.symbol, refresh=True)
                balances = self.client.get_balances()
                prices = self.client.get_prices()
                suggestion, missing = suggest_signal_budget_from_account(
                    SignalSizingPolicy.from_mapping(preferences),
                    balances=balances,
                    prices=prices,
                    quote_asset=rules.quote_asset,
                    reserve_percent=self.risk_limits().min_reserve_percent,
                )
                if missing:
                    raise ValueError(
                        "Valorisation du portefeuille incomplète : " + ", ".join(missing)
                    )
                if suggestion.budget <= 0:
                    raise ValueError("Budget automatique nul après application de la réserve")
                current_price = self.client.get_price(parsed.symbol)
                _, payload = prepare_signal(
                    parsed,
                    rules,
                    budget=suggestion.budget,
                    available_quote=float(
                        balances.get(rules.quote_asset, {}).get("free") or 0
                    ),
                    reserve_percent=self.risk_limits().min_reserve_percent,
                    current_price=current_price,
                    signal_id=signal_id,
                    source="telegram",
                    touch_stop=bool(preferences.get("signal_auto_touch_stop", False)),
                    validity_confirmed=True,
                )
                payload = self.inbox.freeze(self.scope, signal_id, payload)
            command = self.commands.enqueue(
                self.scope, "SUBMIT_POSITION", payload,
                request_key=request_key, ttl=120,
            )
            position = payload["position"]
            detail = f"Commande {command['id']} mise en file"
            self.inbox.set_auto_state(self.scope, signal_id, "QUEUED", detail)
            self.events.append(
                EventType.SIGNAL_AUTO_QUEUED,
                f"Signal Telegram envoyé automatiquement au worker : {parsed.symbol}",
                position_id=position["position_id"], symbol=parsed.symbol,
                signal_id=signal_id, command_id=command["id"],
            )
            self._update(
                state="QUEUED",
                queued_total=self._diagnostics["queued_total"] + 1,
                last_signal_id=signal_id,
                last_detail=detail,
                last_processed_at=now,
            )
            return "QUEUED"
        except Exception as exc:  # fail closed; manual review stays available
            detail = str(exc) or "Signal automatique refusé"
            self.inbox.set_auto_state(self.scope, signal_id, "REJECTED", detail)
            symbol = row["parsed"].get("symbol", "")
            self.events.append(
                EventType.SIGNAL_AUTO_REJECTED,
                f"Signal Telegram automatique refusé : {symbol or 'non reconnu'} · {detail}",
                symbol=symbol, level="WARNING", signal_id=signal_id,
            )
            self._update(
                state="REJECTED",
                rejected_total=self._diagnostics["rejected_total"] + 1,
                last_signal_id=signal_id,
                last_detail=detail,
                last_processed_at=now,
            )
            return "REJECTED"
