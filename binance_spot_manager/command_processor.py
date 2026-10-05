"""Execute les demandes UI dans le worker, avec validation au dernier moment."""

import math
import time
from dataclasses import replace

from .candle_stop import INTERVAL_MS
from .command_store import account_scope
from .execution_engine import build_client_order_id
from .fee_token import AccountCommission, FeeTokenPolicy, assess_bnb_fees
from .investment_plan import InvestmentPreview, investment_risk_context
from .models import EntryStatus, EventType, Position, SLStatus, SLTrigger, SyncStatus, TPStatus, CloseReason
from .position_engine import PositionEngine, finish_position, recompute_position
from .risk_engine import RiskEngine
from .position_store import get_settings_store


class RejectedCommand(ValueError):
    pass


class UncertainCommand(RuntimeError):
    pass


def positive(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise RejectedCommand(f"{name} invalide")
    return value


#: Commandes qui ouvrent une NOUVELLE entrée : seules soumises a la licence de location.
#: Les sorties (annulation, deplacement du stop, clotures) ne sont jamais bloquees.
NEW_ENTRY_ACTIONS = frozenset({"SUBMIT_POSITION", "SIMPLE_BUY"})


class CommandProcessor:
    def __init__(self, store, positions, execution, risk_limits, settings_supplier=None, entry_gate=None):
        self.store, self.positions, self.execution = store, positions, execution
        #: Renvoie "" si une nouvelle entree est autorisee, sinon la raison du refus (licence).
        self.entry_gate = entry_gate
        self.settings = execution.settings
        self.scope = account_scope(self.settings)
        self.risk_limits = risk_limits
        self.settings_supplier = settings_supplier or (lambda: get_settings_store().load())

    def _check_bnb_for_buy(self, symbol, quote_asset, quote_notional, balances, prices):
        policy = FeeTokenPolicy.from_mapping(self.settings_supplier())
        if not policy.enabled:
            return assess_bnb_fees(balances, prices, policy)
        try:
            commission = AccountCommission.from_response(
                self.execution.client.get_commission_rates(symbol), symbol,
            )
        except Exception as exc:
            if policy.block_new_buys:
                raise RejectedCommand(f"Commissions Binance non verifiables avant achat : {exc}") from exc
            self.execution.events.append(EventType.ERROR, f"Controle des frais indisponible : {exc}",
                                         level="WARNING", symbol=symbol)
            return None
        if not commission.bnb_enabled:
            # Aucun frais BNB n'est prevu pour cette paire sur ce compte.
            return assess_bnb_fees(balances, prices, replace(policy, enabled=False))
        policy = replace(policy, estimated_fee_percent=commission.conservative_buy_percent())
        assessment = assess_bnb_fees(
            balances, prices, policy,
            quote_asset=quote_asset, quote_notional=quote_notional,
        )
        if not assessment.sufficient and policy.block_new_buys:
            raise RejectedCommand(
                assessment.reason
                + ". Recharger du BNB, reduire le seuil ou desactiver le blocage dans Settings."
            )
        return assessment

    def run_one(self):
        command = self.store.claim(self.scope)
        if command is None:
            return None
        try:
            self.settings.assert_write_allowed("worker command")
            refusal = self.entry_gate() if self.entry_gate and command["action"] in NEW_ENTRY_ACTIONS else ""
            if refusal:
                raise RejectedCommand(refusal)
            result = getattr(self, "_" + command["action"].lower())(command["payload"])
            state = "SUCCEEDED"
        except RejectedCommand as exc:
            state, result = "FAILED", {"message": str(exc)}
        except Exception as exc:
            # Inclut un crash disque apres un POST : ne jamais rejouer.
            state, result = "UNCERTAIN", {"message": str(exc)}
        self.store.finish(self.scope, command["id"], state, result)
        self.execution.events.append(
            EventType.POSITION_UPDATED if state == "SUCCEEDED" else EventType.ERROR,
            f"Commande {command['action']} : {state}",
            level="INFO" if state == "SUCCEEDED" else "ERROR",
            command_id=command["id"], result=result,
        )
        return state

    def _position(self, payload, *, allow_closing=False):
        position = self.positions.load(payload["position_id"])
        if position is None or not position.is_open or position.environment != "DEMO":
            raise RejectedCommand("Position Demo ouverte introuvable")
        if position.status.value == "CLOSING" and not allow_closing:
            raise RejectedCommand("Cloture en cours : aucune autre modification autorisee")
        return position

    def _check_connection(self, payload):
        self.execution.client.ping()
        return {"message": "Circuit UI/worker et connexion Demo verifies ; aucun ordre envoye"}

    def _close_market(self, payload):
        from .market_close import close_market
        return close_market(self._position(payload, allow_closing=True),
                            self.execution, self.positions)

    def _fresh_price(self, symbol, reference):
        reference = positive(reference, "Prix de confirmation")
        price = self.execution.client.get_price(symbol)
        if abs(price / reference - 1) > 0.01:
            raise RejectedCommand("Prix modifie de plus de 1 % : nouvelle confirmation requise")
        return price

    @staticmethod
    def _check_result(result):
        if not result.success:
            if result.status == "UNKNOWN":
                raise UncertainCommand(result.error)
            raise RejectedCommand(result.error or "Operation refusee")

    @staticmethod
    def _check_signal_window(payload, price=None):
        """Contrat CSI gelé dans la commande : VALID_FROM, EXPIRES_AT et écart maximal à ENTRY_1.

        Appelé avant le contrôle de prix (expiration) puis juste avant l'envoi
        de l'entrée (expiration et écart), toujours depuis le payload gelé.
        """
        if "signal_valid_from" in payload:
            valid_from = positive(payload["signal_valid_from"], "Début de validité du signal (VALID_FROM)")
            if time.time() < valid_from:
                raise RejectedCommand("Signal pas encore valide (VALID_FROM non atteint) ; aucun ordre envoyé")
        if "signal_expires_at" in payload:
            expiry = positive(payload["signal_expires_at"], "Expiration du signal (EXPIRES_AT)")
            if time.time() >= expiry:
                raise RejectedCommand("Signal expiré (EXPIRES_AT atteint) ; aucun ordre envoyé")
        if price is not None and "max_entry_deviation_bps" in payload:
            reference = positive(payload.get("signal_entry_price"), "Prix ENTRY_1 du signal")
            max_bps = float(payload["max_entry_deviation_bps"])
            if not math.isfinite(max_bps) or max_bps < 0:
                raise RejectedCommand("MAX_ENTRY_DEVIATION_BPS invalide")
            deviation = abs(price / reference - 1) * 10_000
            if deviation > max_bps:
                raise RejectedCommand(
                    f"Écart de prix {deviation:.1f} bps > {max_bps:g} bps autorisés par le signal ; aucun ordre envoyé"
                )

    def _check_signal_route(self, payload):
        """Refus additionnels pour une commande AUTO (aucun garde-fou existant n'est modifié).

        Une commande sans confirmation_mode (confirmation manuelle ou ancien plan) est
        traitée exactement comme avant ; une commande AUTO doit porter une décision de
        routage AUTO, un statut CSI DEMO_ELIGIBLE, et le mode DEMO_MANUAL l'interdit.
        """
        mode = payload.get("confirmation_mode")
        if mode is None or mode == "MANUAL":
            return
        if mode != "AUTO":
            raise RejectedCommand("Mode de confirmation inconnu ; aucun ordre envoyé")
        route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
        if route.get("decision") != "AUTO":
            raise RejectedCommand("Commande automatique sans décision de routage AUTO ; aucun ordre envoyé")
        csi = any(key in payload for key in ("signal_expires_at", "exit_policy_hash"))
        if csi and payload.get("signal_validation_status") != "DEMO_ELIGIBLE":
            raise RejectedCommand("Signal CSI non DEMO_ELIGIBLE : jamais exécuté automatiquement ; aucun ordre envoyé")
        preferences = self.settings_supplier()
        preferences = preferences if isinstance(preferences, dict) else {}
        honor = preferences.get("signal_route_honor_demo_manual", True)
        if (honor is not False) and self.settings.run_mode.value == "DEMO_MANUAL":
            raise RejectedCommand("Mode DEMO_MANUAL : exécution automatique interdite ; aucun ordre envoyé")

    def _submit_position(self, payload):
        if "signal_confirmation_expires_at" in payload:
            deadline = positive(payload["signal_confirmation_expires_at"], "Expiration du signal")
            if deadline <= time.time() or deadline > time.time() + 125:
                raise RejectedCommand("Confirmation du signal expirée ou invalide ; aucun ordre envoyé")
        self._check_signal_route(payload)
        self._check_signal_window(payload)
        proposed = Position.model_validate(payload["position"])
        if proposed.environment != "DEMO" or proposed.quote_asset not in {"USDT", "USDC"}:
            raise RejectedCommand("Position Demo USDT/USDC requise")
        if (proposed.manual_exits or proposed.oco_exit is not None or proposed.stop_loss.status not in {SLStatus.PLANNED, SLStatus.NONE}
                or proposed.stop_loss.order_id or proposed.stop_loss.client_order_id or proposed.stop_loss.executed_qty
                or any(t.status is not TPStatus.PENDING or t.order_id or t.client_order_id or t.executed_qty for t in proposed.take_profits)):
            raise RejectedCommand("La demande doit contenir un plan neuf, sans ordres ou executions preexistants")
        if proposed.stop_loss.trigger is SLTrigger.CANDLE_CLOSE and (
                proposed.stop_loss.candle_interval not in INTERVAL_MS or proposed.stop_loss.candle_checked_until is not None):
            raise RejectedCommand("SL a la cloture : intervalle de bougie invalide ou plan deja suivi")
        requested_ids = set(payload["entry_ids"])
        entries = [entry.model_copy(deep=True) for entry in proposed.entries if entry.entry_id in requested_ids]
        if not entries or len(entries) != len(requested_ids) or any(
            e.status is not EntryStatus.PLANNED or e.executed_qty or e.order_id for e in entries
        ):
            raise RejectedCommand("Seules des entrees planifiees non envoyees sont acceptables")
        existing_id = payload.get("existing_id")
        if payload.get("independent_position") and existing_id:
            raise RejectedCommand("Une strategie independante ne peut pas etre fusionnee dans une autre position")
        sequences = [e.sequence_number for e in entries]
        if len(set(sequences)) != len(sequences) or any(number <= 0 for number in sequences):
            raise RejectedCommand("Chaque entree doit avoir un numero distinct et positif")
        position = self._position({"position_id": existing_id}) if existing_id else proposed
        if position.symbol != proposed.symbol or position.quote_asset != proposed.quote_asset:
            raise RejectedCommand("La demande ne correspond pas a la position cible")
        if position.oco_exit is not None:
            raise RejectedCommand("Ajout sur OCO interdit sans redimensionnement des protections")
        if existing_id and any(e.entry_id in requested_ids for e in position.entries):
            raise RejectedCommand("Entrees deja presentes : verifier leur execution")
        if not existing_id and self.positions.exists(position.position_id):
            raise RejectedCommand("Position deja creee : verifier sa reprise")
        if not existing_id:
            position.order_identity_version = 2
        rules = self.execution.rules(position.symbol, refresh=True)
        if not rules.is_trading or rules.base_asset != position.base_asset or rules.quote_asset != position.quote_asset:
            raise RejectedCommand("Paire ou regles Binance incompatibles")
        price = self._fresh_price(position.symbol, payload["reference_price"])
        cost, quantity, loss = 0.0, 0.0, 0.0
        for entry in entries:
            qty = positive(entry.binance_qty, "Quantite")
            entry_price = price if entry.order_type.value == "MARKET" else positive(entry.resolved_price, "Prix limite")
            errors = rules.validate_order(entry_price, qty, market=entry.order_type.value == "MARKET")
            if errors:
                raise RejectedCommand(" ; ".join(errors))
            cost += qty * entry_price
            quantity += qty
            loss += qty * (entry_price if position.stop_loss.status is SLStatus.NONE
                           else max(entry_price - positive(position.stop_loss.resolved_price, "SL"), 0))
        if position.stop_loss.status is not SLStatus.NONE and position.stop_loss.resolved_price >= price:
            raise RejectedCommand("SL atteint avant l'achat : plan perime")
        if any(tp.target_price is None or tp.target_price <= price for tp in position.take_profits if tp.is_pending):
            raise RejectedCommand("TP deja atteint : plan perime")
        limits = self.risk_limits()
        balances, prices = self.execution.client.get_balances(), self.execution.client.get_prices()
        positions = self.positions.list_all()
        if self.positions.read_errors:
            raise RejectedCommand("Stockage illisible : risque incomplet")
        preview = InvestmentPreview(position.symbol, position.base_asset, position.quote_asset,
                                    "QUEUED", quantity, cost, position.stop_loss.resolved_price or 0, loss, ())
        plan, snapshot, _ = investment_risk_context(
            preview, capital=cost, reserve_percent=limits.min_reserve_percent,
            balances=balances, prices=prices, positions=positions,
        )
        report = RiskEngine(limits).evaluate(plan, snapshot, symbol=position.symbol)
        if not report.accepted:
            raise RejectedCommand(" ; ".join(report.refusals))
        self._check_bnb_for_buy(position.symbol, position.quote_asset, cost, balances, prices)
        # Dernier contrôle du contrat CSI avant tout envoi : expiration et écart de prix.
        self._check_signal_window(payload, price)
        if existing_id:
            PositionEngine(rules).add_entries(position, entries)
        else:
            position.entries = entries
        self.positions.save(position)
        results = []
        for entry in entries:
            entry.client_order_id = build_client_order_id(symbol=position.symbol, position_id=position.position_id,
                                                         suffix=f"E{entry.sequence_number}",
                                                         identity_version=position.order_identity_version)
            if not self.settings.dry_run:
                entry.status = EntryStatus.SUBMITTED
            self.positions.save(position)
            result = self.execution.place_entry(position, entry, current_price=price)
            if result.success and result.executed_qty > 0 and not result.dry_run:
                PositionEngine(rules).apply_entry_fill(
                    position, entry.entry_id, executed_qty=result.executed_qty,
                    average_price=result.average_price, quote_spent=result.cummulative_quote_qty,
                    commissions=result.commissions, order_id=result.order_id,
                )
            self.positions.save(position)
            self._check_result(result)
            results.append({"entry": entry.sequence_number, "order_id": result.order_id, "status": result.status})
        return {"message": "Entrees traitees ; les executions restent surveillees", "position_id": position.position_id, "orders": results}

    def _simple_buy(self, payload):
        symbol = str(payload["symbol"]).upper()
        rules = self.execution.rules(symbol, refresh=True)
        price = self._fresh_price(symbol, payload["reference_price"])
        quantity = positive(payload["quantity"], "Quantite")
        budget = positive(payload["budget"], "Budget")
        balances = self.execution.client.get_balances()
        prices = self.execution.client.get_prices()
        free = float(balances.get(rules.quote_asset, {}).get("free") or 0)
        if quantity * price > budget or budget > free * (1 - self.risk_limits().min_reserve_percent / 100):
            raise RejectedCommand("Budget ou reserve depasse : recalculer l'achat")
        errors = rules.validate_order(price, quantity, market=True)
        if not rules.is_trading or errors:
            raise RejectedCommand(" ; ".join(errors) or "Paire non negociable")
        self._check_bnb_for_buy(symbol, rules.quote_asset, quantity * price, balances, prices)
        result = self.execution.place_simple_buy(symbol=symbol, quantity=quantity, client_order_id=payload["client_order_id"])
        self._check_result(result)
        return {"message": "Demande d'achat traitee", "order_id": result.order_id, "status": result.status,
                "executed_qty": result.executed_qty, "quote_spent": result.cummulative_quote_qty,
                "commissions": [c.model_dump() for c in result.commissions]}

    def _cancel_order(self, payload):
        symbol, order_id = payload["symbol"], int(payload["order_id"])
        if order_id <= 0:
            raise RejectedCommand("Identifiant d'ordre invalide")
        result = self.execution.cancel_order(symbol, order_id=order_id)
        self._check_result(result)
        if not result.dry_run:
            for position in self.positions.list_open():
                if position.symbol != symbol:
                    continue
                touched = False
                if position.oco_exit and order_id in {position.oco_exit.tp_order_id, position.oco_exit.sl_order_id}:
                    position.oco_exit.status = "CANCELED"
                    position.stop_loss.status = SLStatus.CANCELED
                    for tp in position.take_profits:
                        tp.status = TPStatus.CANCELED
                    touched = True
                for entry in position.entries:
                    if entry.order_id == order_id:
                        entry.status, touched = EntryStatus.CANCELED, True
                if position.stop_loss.order_id == order_id:
                    position.stop_loss.status, touched = SLStatus.CANCELED, True
                for tp in position.take_profits:
                    if tp.order_id == order_id:
                        tp.status, touched = TPStatus.CANCELED, True
                if touched:
                    # Une suppression manuelle ne doit pas etre annulee par l'automation.
                    position.automation.paused = True
                    position.sync_status = SyncStatus.DESYNC_DETECTED
                    recompute_position(position)
                    self.positions.save(position)
        return {"message": "Annulation confirmee" if not result.dry_run else "Simulation sans annulation", "order_id": order_id}

    def _move_sl(self, payload):
        position = self._position(payload)
        if position.stop_loss.order_id != payload.get("expected_order_id"):
            raise RejectedCommand("Le SL a change depuis la confirmation")
        target = positive(payload["target_price"], "Prix SL")
        price = self.execution.client.get_price(position.symbol)
        if target >= price:
            raise RejectedCommand("Le SL doit etre sous le cours actuel")
        result = self.execution.move_stop_loss(position, new_stop_price=target, quantity=position.metrics.net_qty)
        sl = position.stop_loss
        if not result.dry_run and (result.success or sl.status is SLStatus.REPLACING):
            # Deplacement manuel = stop au prix sur Binance (pose, ou incertain chez Binance), plus une surveillance
            # de cloture. Annulation refusee : l'ancien ordre et son mode restent ; creation refusee : l'ancien
            # niveau est protege de nouveau au cycle suivant.
            sl.resolved_price = target
            sl.trigger, sl.candle_interval, sl.candle_checked_until = SLTrigger.TOUCH, "", None
        recompute_position(position)
        self.positions.save(position)
        self._check_result(result)
        return {"message": "Demande de deplacement traitee", "order_id": result.order_id, "status": result.status}

    def _close_local(self, payload):
        position = self._position(payload)
        # Toujours verifier chaque annulation, y compris les entrees.
        targets = {e.order_id for e in position.open_entries if e.order_id}
        if any(e.client_order_id and not e.order_id for e in position.open_entries):
            raise RejectedCommand("Entree incertaine : reconcilier avant fermeture")
        if position.oco_exit and position.oco_exit.status in {"ACTIVE", "PARTIAL"}:
            targets.add(position.oco_exit.tp_order_id)
        else:
            targets.update(t.order_id for t in position.take_profits if t.status is TPStatus.SUBMITTED and t.order_id)
            if position.stop_loss.status is SLStatus.ACTIVE and position.stop_loss.order_id:
                targets.add(position.stop_loss.order_id)
        if position.stop_loss.status is SLStatus.REPLACING:
            raise RejectedCommand("SL incertain : reconcilier avant fermeture")
        if any(t.status is TPStatus.SUBMITTED and not t.order_id for t in position.take_profits):
            raise RejectedCommand("TP incertain : reconcilier avant fermeture")
        if self.settings.dry_run:
            return {"message": "Simulation : aucune annulation ni fermeture reelle"}
        position.automation.paused = True
        self.positions.save(position)
        for order_id in sorted(targets):
            self._cancel_order({"symbol": position.symbol, "order_id": order_id})
        position = self._position(payload)
        finish_position(position, CloseReason.MANUAL_CLOSE)
        self.positions.save(position)
        return {"message": "Position fermee localement ; aucune vente envoyee", "position_id": position.position_id}
