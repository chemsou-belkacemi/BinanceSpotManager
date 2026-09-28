"""Execute les demandes UI dans le worker, avec validation au dernier moment."""

import math

from .command_store import account_scope
from .execution_engine import build_client_order_id
from .investment_plan import InvestmentPreview, investment_risk_context
from .models import EntryStatus, EventType, Position, SLStatus, SyncStatus, TPStatus, CloseReason
from .position_engine import PositionEngine, finish_position, recompute_position
from .risk_engine import RiskEngine


class RejectedCommand(ValueError):
    pass


class UncertainCommand(RuntimeError):
    pass


def positive(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise RejectedCommand(f"{name} invalide")
    return value


class CommandProcessor:
    def __init__(self, store, positions, execution, risk_limits):
        self.store, self.positions, self.execution = store, positions, execution
        self.settings = execution.settings
        self.scope = account_scope(self.settings)
        self.risk_limits = risk_limits

    def run_one(self):
        command = self.store.claim(self.scope)
        if command is None:
            return None
        try:
            self.settings.assert_write_allowed("worker command")
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

    def _position(self, payload):
        position = self.positions.load(payload["position_id"])
        if position is None or not position.is_open or position.environment != "DEMO":
            raise RejectedCommand("Position Demo ouverte introuvable")
        return position

    def _check_connection(self, payload):
        self.execution.client.ping()
        return {"message": "Circuit UI/worker et connexion Demo verifies ; aucun ordre envoye"}

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

    def _submit_position(self, payload):
        proposed = Position.model_validate(payload["position"])
        if proposed.environment != "DEMO" or proposed.quote_asset not in {"USDT", "USDC"}:
            raise RejectedCommand("Position Demo USDT/USDC requise")
        if (proposed.oco_exit is not None or proposed.stop_loss.status not in {SLStatus.PLANNED, SLStatus.NONE}
                or proposed.stop_loss.order_id or proposed.stop_loss.client_order_id or proposed.stop_loss.executed_qty
                or any(t.status is not TPStatus.PENDING or t.order_id or t.client_order_id or t.executed_qty for t in proposed.take_profits)):
            raise RejectedCommand("La demande doit contenir un plan neuf, sans ordres ou executions preexistants")
        requested_ids = set(payload["entry_ids"])
        entries = [entry.model_copy(deep=True) for entry in proposed.entries if entry.entry_id in requested_ids]
        if not entries or len(entries) != len(requested_ids) or any(
            e.status is not EntryStatus.PLANNED or e.executed_qty or e.order_id for e in entries
        ):
            raise RejectedCommand("Seules des entrees planifiees non envoyees sont acceptables")
        existing_id = payload.get("existing_id")
        position = self._position({"position_id": existing_id}) if existing_id else proposed
        if position.symbol != proposed.symbol or position.quote_asset != proposed.quote_asset:
            raise RejectedCommand("La demande ne correspond pas a la position cible")
        if position.oco_exit is not None:
            raise RejectedCommand("Ajout sur OCO interdit sans redimensionnement des protections")
        if existing_id and any(e.entry_id in requested_ids for e in position.entries):
            raise RejectedCommand("Entrees deja presentes : verifier leur execution")
        if not existing_id and self.positions.exists(position.position_id):
            raise RejectedCommand("Position deja creee : verifier sa reprise")
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
        if existing_id:
            PositionEngine(rules).add_entries(position, entries)
        else:
            position.entries = entries
        self.positions.save(position)
        results = []
        for entry in entries:
            entry.client_order_id = build_client_order_id(symbol=position.symbol, position_id=position.position_id,
                                                         suffix=f"E{entry.sequence_number}")
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
        free = self.execution.client.get_free_balance(rules.quote_asset)
        if quantity * price > budget or budget > free * (1 - self.risk_limits().min_reserve_percent / 100):
            raise RejectedCommand("Budget ou reserve depasse : recalculer l'achat")
        errors = rules.validate_order(price, quantity, market=True)
        if not rules.is_trading or errors:
            raise RejectedCommand(" ; ".join(errors) or "Paire non negociable")
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
        if result.success and not result.dry_run:
            position.stop_loss.resolved_price = target
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
