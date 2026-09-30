"""Prepare fresh Telegram or drop (ML) signals and enqueue them for the guarded worker."""

from __future__ import annotations

from datetime import datetime, timezone
import time

from .models import EventType
from .signal_parser import ParsedSignal
from .signal_plan import prepare_signal, signal_sl_after_tp
from .signal_sizing import SignalSizingPolicy, suggest_signal_budget_from_account


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
        telegram_ready = bool(preferences.get("signal_telegram_enabled", False)
                              and preferences.get("signal_telegram_auto_enabled", False))
        drop_ready = bool(preferences.get("signal_drop_enabled", False)
                          and preferences.get("signal_drop_auto_enabled", False))
        if not (telegram_ready or drop_ready):
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
        now = self.clock()
        rows = self.inbox.auto_candidates(
            self.scope,
            enabled_since=enabled_since,
            oldest_source_timestamp=now - max_age_minutes * 60,
            now=now,
            limit=limit,
            sources=sources,
        )
        processed = []
        for row in rows:
            processed.append(self._process(row, preferences, now, max_age_minutes))
        if not rows:
            self._update(state="ARMED", last_detail="")
        return processed

    def _process(self, row, preferences, now, max_age_minutes):
        signal_id = row["id"]
        source = "api" if row.get("source") == "api" else "telegram"
        label = SOURCE_LABELS[source]
        request_key = f"signal:{signal_id}"
        existing = self.commands.get_by_request_key(self.scope, request_key)
        if existing:
            self.inbox.set_auto_state(
                self.scope, signal_id, "QUEUED", f"Commande {existing['id']} : {existing['state']}",
            )
            self._update(state="QUEUED", last_signal_id=signal_id,
                         last_detail=f"Commande {existing['id']}", last_processed_at=now)
            return "QUEUED"
        valid_from = float(row["parsed"].get("valid_from") or 0)
        if (row["parsed"].get("signal_version") == 2 and not row["parsed"].get("errors")
                and now < valid_from):
            # Pas encore valide : la ligne reste disponible, sans être réclamée.
            self._update(state="ARMED", last_signal_id=signal_id,
                         last_detail=f"Signal V2 en attente de VALID_FROM ({_iso_utc(valid_from)})")
            return "WAITING"
        if not self.inbox.claim_auto(self.scope, signal_id):
            return "SKIPPED"
        try:
            if row["parsed"].get("errors"):
                raise ValueError("Signal non reconnu ou bloqué par le parseur")
            parsed = ParsedSignal(**row["parsed"])
            if parsed.is_v2:
                # Contrat V2 : VALID_FROM <= maintenant < EXPIRES_AT remplace la fenêtre d'âge.
                if now >= parsed.expires_at:
                    raise ValueError(
                        f"Signal V2 expiré (EXPIRES_AT {_iso_utc(parsed.expires_at)}) : aucune exécution"
                    )
            else:
                source_timestamp = float(row.get("source_timestamp") or 0)
                if source_timestamp <= 0 or source_timestamp < now - max_age_minutes * 60:
                    raise ValueError(f"{MESSAGE_LABELS[source]} trop ancien pour une exécution automatique")
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
                if parsed.is_v2:
                    deviation = entry_deviation_bps(current_price, parsed.entries[0])
                    if deviation > parsed.max_entry_deviation_bps:
                        raise ValueError(
                            f"Écart de prix {deviation:.1f} bps > MAX_ENTRY_DEVIATION_BPS "
                            f"{parsed.max_entry_deviation_bps:g} (prix {current_price}, ENTRY_1 {parsed.entries[0]})"
                        )
                # V2 : prepare_signal remplace la règle enregistrée par EXIT_POLICY_ID.
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
                    source=source,
                    sl_after_tp=signal_sl_after_tp(preferences.get("signal_sl_after_tp")),
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
                f"{label} envoyé automatiquement au worker : {parsed.symbol}",
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
                f"{label} automatique refusé : {symbol or 'non reconnu'} · {detail}",
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
