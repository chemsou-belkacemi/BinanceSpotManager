"""Tests d'automation TP/SL avec un client Binance simule.

Aucun appel reseau : le client est remplace par un double controle, ce qui
permet de verifier la sequence complete (declenchement TP, confirmation du
fill, deplacement du SL, fin de position) de facon deterministe.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from binance_spot_manager.automation_engine import (
    AutomationConfig,
    AutomationEngine,
    CycleResult,
    planned_tp_quantity,
)
from binance_spot_manager.binance_client import BinanceError
from binance_spot_manager.config import RunMode, Settings
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.execution_engine import ExecutionEngine
from binance_spot_manager.models import (
    Commission,
    Entry,
    EntryStatus,
    OrderType,
    Position,
    PositionStatus,
    PriceMode,
    SLMode,
    SLRuleAfterTP,
    SLStatus,
    TPExecutionPolicy,
    TPReference,
    TPStatus,
    TakeProfit,
)
from binance_spot_manager.position_engine import PositionEngine, recompute_position
from binance_spot_manager.reconciliation_engine import ReconciliationEngine
from binance_spot_manager.symbol_rules import SymbolRules, parse_symbol_rules

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status", ["PARTIALLY_FILLED", "CANCELED", "FILLED"])
def test_partial_sl_never_closes_remaining_position_or_sends_tp(engine, events, status):
    execution, fake, rules = engine
    position = make_position(rules)
    fake.orders[SL_CLIENT_ID].update({
        "status": status, "executedQty": "0.002", "cummulativeQuoteQty": "160",
    })
    automation = build_automation(execution, rules, events)
    result = automation.run_cycle(position, 90000)  # prix qui declencherait aussi le TP
    assert not result.position_finished
    assert position.is_open
    assert position.metrics.net_qty == pytest.approx(0.004)
    assert result.exits_blocked
    assert fake.created == []
    assert fake.cancelled == []
    automation.run_cycle(position, 90000)
    assert position.metrics.net_qty == pytest.approx(0.004)


def test_unknown_sl_blocks_local_tp_in_same_cycle(engine, events):
    execution, fake, rules = engine
    position = make_position(rules)
    fake.orders.clear()
    result = build_automation(execution, rules, events).run_cycle(position, 90000)
    assert result.exits_blocked
    assert fake.created == []


def test_partial_sl_then_full_fill_accounts_cumulative_fees_once(engine, events):
    execution, fake, rules = engine
    position = make_position(rules)
    order = fake.orders[SL_CLIENT_ID]
    order.update({
        "status": "PARTIALLY_FILLED", "executedQty": "0.002", "cummulativeQuoteQty": "160",
        "fills": [{"qty": "0.002", "price": "80000", "commission": "0.16", "commissionAsset": "USDT"}],
    })
    automation = build_automation(execution, rules, events)
    automation.run_cycle(position, 80000)
    assert position.metrics.net_qty == pytest.approx(0.004)
    order.update({
        "status": "FILLED", "executedQty": "0.006", "cummulativeQuoteQty": "480",
        "fills": [{"qty": "0.006", "price": "80000", "commission": "0.48", "commissionAsset": "USDT"}],
    })
    result = automation.run_cycle(position, 80000)
    assert result.position_finished
    assert not position.is_open
    assert position.metrics.net_qty == pytest.approx(0)
    assert position.stop_loss.executed_qty == pytest.approx(0.006)
    assert position.stop_loss.commission_total("USDT") == pytest.approx(0.48)
    automation.run_cycle(position, 80000)
    assert position.stop_loss.quote_received == pytest.approx(480)
    assert fake.created == []
    assert fake.cancelled == []


def test_sl_move_does_not_replace_order_if_cancellation_unconfirmed(engine):
    execution, fake, rules = engine
    position = make_position(rules)
    fake.orders.clear()
    result = execution.move_stop_loss(position, new_stop_price=82000, quantity=0.006)
    assert not result.success
    assert fake.created == []

SYMBOL_INFO = {
    "symbol": "BTCUSDT",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "status": "TRADING",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "9000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
        {"filterType": "NOTIONAL", "minNotional": "5"},
    ],
}

#: Le SL de la position de test existe deja cote Binance.
SL_ORDER_ID = 9001
SL_CLIENT_ID = "BSM-D-TEST-SL"


# ==========================================================================
# Double de test
# ==========================================================================


class FakeClient:
    """Client Binance simule.

    La signature de create_order reproduit EXACTEMENT celle du vrai
    BinanceSpotClient : ExecutionEngine l'appelle avec des kwargs explicites
    (client_order_id=..., stop_price=...), pas avec un dict camelCase.

    Seul un ordre MARKET se remplit a l'envoi. Une LIMIT et un
    STOP_LOSS_LIMIT restent NEW jusqu'a ce que le prix atteigne le niveau —
    comme sur Binance. `fail_on_stop_loss` simule un refus de creation de SL.
    """

    def __init__(
        self,
        rules: SymbolRules,
        price: float = 84000.0,
        *,
        fail_on_stop_loss: bool = False,
    ) -> None:
        self.rules = rules
        self.price = price
        self.fail_on_stop_loss = fail_on_stop_loss
        self.orders: dict[str, dict[str, Any]] = {}
        self._next_id = 1000
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[Any] = []
        self.fail_next: Optional[BinanceError] = None

    def seed_stop_loss(self, qty: float = 0.006, stop: float = 80640.0) -> None:
        """Enregistre le SL deja en place, comme s'il venait de Binance."""
        self.orders[SL_CLIENT_ID] = {
            "symbol": "BTCUSDT",
            "orderId": SL_ORDER_ID,
            "clientOrderId": SL_CLIENT_ID,
            "side": "SELL",
            "type": "STOP_LOSS_LIMIT",
            "status": "NEW",
            "price": str(round(stop * 0.997, 2)),
            "stopPrice": str(stop),
            "origQty": str(qty),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "fills": [],
        }

    # -- endpoints utilises par ExecutionEngine -----------------------
    def get_symbol_info(self, symbol: str):
        return SYMBOL_INFO if symbol.upper() == "BTCUSDT" else None

    def get_prices(self, symbols=None):
        return {"BTCUSDT": self.price}

    def get_price(self, symbol: str) -> float:
        return self.price

    def get_free_balance(self, asset: str) -> float:
        return 5000.0

    def get_balances(self):
        return {"USDT": {"free": 5000.0, "locked": 0.0}}

    def get_open_orders(self, symbol: Optional[str] = None):
        return [o for o in self.orders.values() if o["status"] in {"NEW", "PARTIALLY_FILLED"}]

    def find_order(self, symbol, *, order_id=None, client_order_id=None):
        if client_order_id and client_order_id in self.orders:
            return self.orders[client_order_id]
        for order in self.orders.values():
            if order_id and order.get("orderId") == order_id:
                return order
        return None

    def get_order(self, symbol, *, order_id=None, client_order_id=None):
        found = self.find_order(symbol, order_id=order_id, client_order_id=client_order_id)
        if found is None:
            raise BinanceError("Order does not exist.", code=-2013)
        return found

    def cancel_order(self, symbol, *, order_id=None, client_order_id=None):
        found = self.find_order(symbol, order_id=order_id, client_order_id=client_order_id)
        if found is None:
            raise BinanceError("Unknown order sent.", code=-2011)
        found["status"] = "CANCELED"
        self.cancelled.append(found.get("orderId"))
        return found

    def create_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[str] = None,
        price: Optional[str] = None,
        stop_price: Optional[str] = None,
        time_in_force: Optional[str] = None,
        client_order_id: Optional[str] = None,
        quote_order_qty: Optional[str] = None,
        test: bool = False,
    ) -> dict[str, Any]:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error

        kind = (order_type or "MARKET").upper()
        if kind == "STOP_LOSS_LIMIT" and self.fail_on_stop_loss:
            raise BinanceError("Filter failure: MAX_NUM_ALGO_ORDERS", code=-1013)

        cid = client_order_id or f"auto{self._next_id}"
        qty = float(quantity or 0)
        is_market = kind == "MARKET"
        # Seul un ordre MARKET se remplit a l'envoi. Une LIMIT ou un
        # STOP_LOSS_LIMIT reste NEW jusqu'a ce que le prix atteigne le niveau.
        fill = is_market
        fill_price = self.price if is_market else float(price or 0)
        quote = qty * fill_price

        order = {
            "symbol": symbol.upper(),
            "orderId": self._next_id,
            "clientOrderId": cid,
            "side": side.upper(),
            "type": kind,
            "status": "FILLED" if fill else "NEW",
            "price": str(fill_price),
            "stopPrice": str(stop_price or "0"),
            "origQty": str(qty),
            "executedQty": str(qty) if fill else "0",
            "cummulativeQuoteQty": str(quote) if fill else "0",
            "fills": (
                [
                    {
                        "price": str(fill_price),
                        "qty": str(qty),
                        "commission": "0.05",
                        "commissionAsset": "USDT",
                    }
                ]
                if fill
                else []
            ),
        }
        self._next_id += 1
        self.orders[cid] = order
        self.created.append(
            {
                "client_order_id": cid,
                "type": kind,
                "qty": qty,
                "price": fill_price,
                "stop_price": float(stop_price or 0),
            }
        )
        return order


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def settings() -> Settings:
    return Settings(
        run_mode=RunMode.DEMO_AUTO,
        demo_base_url="[testnet.binance.vision](https://testnet.binance.vision)",
        demo_api_key="key",
        demo_api_secret="secret",
        quote_asset="USDT",
    )


@pytest.fixture
def rules() -> SymbolRules:
    return parse_symbol_rules(SYMBOL_INFO)


@pytest.fixture
def events(tmp_path) -> EventStore:
    return EventStore(tmp_path / "events.jsonl")


def build_execution(settings, rules, events, fake):
    cache = type("Cache", (), {"get": lambda self, s, refresh=False: rules})()
    return ExecutionEngine(fake, cache, settings=settings, events=events)


@pytest.fixture
def engine(settings, rules, events):
    """(execution, fake, rules). Le SL de la position existe deja cote Binance."""
    fake = FakeClient(rules)
    fake.seed_stop_loss()
    return build_execution(settings, rules, events, fake), fake, rules


def build_automation(execution, rules, events) -> AutomationEngine:
    cache = type("Cache", (), {"get": lambda self, s, refresh=False: rules})()
    return AutomationEngine(
        execution,
        PositionEngine(rules),
        cache,
        events=events,
        config=AutomationConfig(sl_replace_threshold_percent=0.0),
    )


def make_position(rules: SymbolRules, *, tp_rules=None, entry_status=EntryStatus.FILLED) -> Position:
    """Position 1 Entry remplie + 2 TP qui vendent la totalite, SL deja en place.

    Le SL est ACTIVE et porte un order_id : une position ACTIVE a forcement
    une protection cote Binance. Un SL reste PLANNED ferait creer un ordre au
    premier cycle, ce qui n'est pas ce que ces tests mesurent.

    Les TP vendent 60 % puis 100 % du restant : un pourcentage s'applique
    toujours a la quantite RESTANTE, pas au total initial.
    """
    position = Position(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        environment="DEMO",
        creation_price=84000.0,
    )
    position.entries.append(
        Entry(
            sequence_number=1,
            order_type=OrderType.MARKET,
            status=entry_status,
            executed_qty=0.006,
            net_qty=0.006,
            binance_qty=0.006,
            resolved_price=84000.0,
            average_fill_price=84000.0,
            quote_spent=504.0,
            commissions=[Commission(asset="USDT", amount=0.5)],
        )
    )
    for index, percent in enumerate([3.0, 6.0], start=1):
        position.take_profits.append(
            TakeProfit(
                sequence_number=index,
                price_mode=PriceMode.PERCENT,
                reference_mode=TPReference.AVERAGE_PRICE,
                target_price=84000.0 * (1 + percent / 100),
                target_percent=percent,
                sell_percent=60.0 if index == 1 else 100.0,
                execution_policy=TPExecutionPolicy.MARKET_ON_TRIGGER,
                sl_rule_after_hit=(tp_rules or {}).get(index, SLRuleAfterTP.NO_CHANGE),
            )
        )
    position.stop_loss.mode = SLMode.AVERAGE_PERCENT
    position.stop_loss.value = -4.0
    position.stop_loss.resolved_price = 80640.0
    position.stop_loss.quantity = 0.006
    position.stop_loss.status = SLStatus.ACTIVE
    position.stop_loss.order_id = SL_ORDER_ID
    position.stop_loss.client_order_id = SL_CLIENT_ID
    position.status = PositionStatus.ACTIVE
    recompute_position(position)
    return position


# ==========================================================================
# Declenchement des TP
# ==========================================================================


def test_tp_not_triggered_below_target(engine):
    """Sous la cible, aucun ordre ne doit partir."""
    execution, fake, rules = engine
    automation = build_automation(execution, rules, execution.events)
    position = make_position(rules)

    outcome = automation.run_cycle(position, 85000.0)  # sous 86520

    assert outcome.tp_executed is None
    assert fake.created == []
    assert not outcome.errors


def test_active_stop_is_adjusted_after_base_asset_buy_fee(engine):
    execution, fake, rules = engine
    position = make_position(rules)
    position.entries[0].commissions = [Commission(asset="BTC", amount=0.00001)]
    recompute_position(position)

    outcome = build_automation(execution, rules, execution.events).run_cycle(
        position, 85000.0
    )

    assert not outcome.errors
    assert fake.cancelled == [SL_ORDER_ID]
    assert position.stop_loss.status is SLStatus.ACTIVE
    assert position.stop_loss.quantity == pytest.approx(0.00599)
    assert not any(order["type"] == "MARKET" for order in fake.created)


def test_tp_triggers_and_sells(engine):
    execution, fake, rules = engine
    automation = build_automation(execution, rules, execution.events)
    position = make_position(rules)

    outcome = automation.run_cycle(position, 86600.0)

    assert outcome.tp_executed == 1
    tp = position.sorted_tps[0]
    assert tp.status is TPStatus.EXECUTED
    assert tp.executed_qty > 0
    assert tp.gain_realized != 0
    assert position.metrics.net_qty < 0.006
    assert any(o["type"] == "MARKET" for o in fake.created)


def test_tp_sells_net_quantity_after_releasing_locked_stop(settings, rules, events):
    fake = FakeClient(rules, price=85000.0)
    fake.seed_stop_loss(qty=0.00034)
    position = make_position(rules)
    entry = position.entries[0]
    entry.executed_qty = 0.00035
    entry.commissions = [Commission(asset="BTC", amount=0.00000035)]
    position.take_profits = position.take_profits[:1]
    position.take_profits[0].sell_percent = 100.0
    position.take_profits[0].target_price = 85000.0
    position.take_profits[0].status = TPStatus.FAILED
    position.take_profits[0].last_error = "Account has insufficient balance | code=-2010"
    position.stop_loss.quantity = 0.00034
    recompute_position(position)
    assert planned_tp_quantity(position, position.take_profits[0], rules) == 0.00034

    wallet_free = 0.0
    original_cancel = fake.cancel_order
    original_create = fake.create_order

    def cancel_with_balance(*args, **kwargs):
        nonlocal wallet_free
        cancelled = original_cancel(*args, **kwargs)
        wallet_free += float(cancelled["origQty"])
        return cancelled

    def create_with_balance(*args, **kwargs):
        nonlocal wallet_free
        quantity = float(kwargs.get("quantity") or 0)
        if kwargs["side"] == "SELL" and quantity > wallet_free + 1e-12:
            raise BinanceError("Account has insufficient balance", code=-2010)
        if kwargs["side"] == "SELL":
            wallet_free -= quantity
        return original_create(*args, **kwargs)

    fake.cancel_order = cancel_with_balance
    fake.create_order = create_with_balance
    fake.get_free_balance = lambda asset: wallet_free if asset == "BTC" else 5000.0

    execution = build_execution(settings, rules, events, fake)
    automation = build_automation(execution, rules, events)
    before_target = automation.run_cycle(position, 84999.0)
    assert before_target.tp_executed is None
    assert fake.created == []

    outcome = automation.run_cycle(position, 85000.0)

    assert not outcome.errors
    assert outcome.tp_executed == 1
    assert fake.cancelled == [SL_ORDER_ID]
    assert fake.created[0]["type"] == "MARKET"
    assert fake.created[0]["qty"] == 0.00034
    assert position.metrics.net_qty == pytest.approx(0.00000965)
    assert position.status is PositionStatus.CLOSED


def test_reconciliation_accepts_stop_rounded_to_sellable_quantity(engine):
    execution, fake, rules = engine
    position = make_position(rules)
    position.entries[0].executed_qty = 0.00035
    position.entries[0].commissions = [Commission(asset="BTC", amount=0.00000035)]
    recompute_position(position)
    assert position.metrics.net_qty == pytest.approx(0.00034965)

    fake.orders[SL_CLIENT_ID]["origQty"] = "0.00034"
    position.stop_loss.quantity = 0.00034
    report = ReconciliationEngine(execution, events=execution.events).reconcile(position)

    assert not any(f.kind == "SL_QTY_MISMATCH" for f in report.findings)


def test_reconciliation_detects_real_stop_quantity_gap(engine):
    execution, fake, rules = engine
    position = make_position(rules)
    fake.orders[SL_CLIENT_ID]["origQty"] = "0.005"
    position.stop_loss.quantity = 0.005

    report = ReconciliationEngine(execution, events=execution.events).reconcile(position)

    assert any(f.kind == "SL_QTY_MISMATCH" for f in report.findings)


def test_failed_tp_restores_stop_on_net_quantity(settings, rules, events):
    fake = FakeClient(rules)
    fake.seed_stop_loss(qty=0.006)
    position = make_position(rules)
    position.entries[0].commissions = [Commission(asset="BTC", amount=0.00001)]
    recompute_position(position)
    original_create = fake.create_order

    def fail_tp_only(*args, **kwargs):
        if kwargs["order_type"] == "MARKET":
            raise BinanceError("Account has insufficient balance", code=-2010)
        return original_create(*args, **kwargs)

    fake.create_order = fail_tp_only
    execution = build_execution(settings, rules, events, fake)
    outcome = build_automation(execution, rules, events).run_cycle(position, 86600.0)

    assert outcome.tp_executed is None
    assert outcome.errors
    assert position.stop_loss.status is SLStatus.ACTIVE
    assert position.stop_loss.quantity == pytest.approx(0.00599)
    assert any(order["type"] == "STOP_LOSS_LIMIT" for order in fake.created)


def test_wallet_shortfall_does_not_send_partial_tp(settings, rules, events):
    fake = FakeClient(rules)
    fake.seed_stop_loss()
    fake.get_free_balance = lambda asset: 0.001 if asset == "BTC" else 5000.0
    position = make_position(rules)
    execution = build_execution(settings, rules, events, fake)

    outcome = build_automation(execution, rules, events).run_cycle(position, 86600.0)

    assert outcome.tp_executed is None
    assert any("Solde libre BTC insuffisant" in error for error in outcome.errors)
    assert position.stop_loss.status is SLStatus.ACTIVE
    assert not any(order["type"] == "MARKET" for order in fake.created)


def test_limit_tp_expires_and_retries_with_stop_restored(settings, rules, events):
    fake = FakeClient(rules)
    fake.seed_stop_loss()
    position = make_position(rules)
    position.take_profits[0].execution_policy = TPExecutionPolicy.LIMIT_ON_TRIGGER
    original_create = fake.create_order
    time_in_force_values = []

    def expire_limit(*args, **kwargs):
        if kwargs["order_type"] == "LIMIT":
            time_in_force_values.append(kwargs["time_in_force"])
        order = original_create(*args, **kwargs)
        if kwargs["order_type"] == "LIMIT":
            order["status"] = "EXPIRED"
        return order

    fake.create_order = expire_limit
    execution = build_execution(settings, rules, events, fake)
    automation = build_automation(execution, rules, events)

    first = automation.run_cycle(position, 86600.0)
    assert first.tp_executed is None
    assert position.stop_loss.status is SLStatus.ACTIVE
    assert position.take_profits[0].attempt_count == 1

    second = automation.run_cycle(position, 86600.0)
    assert second.tp_executed is None
    assert position.stop_loss.status is SLStatus.ACTIVE
    assert position.take_profits[0].attempt_count == 2
    assert time_in_force_values == ["FOK", "FOK"]
    limit_orders = [order for order in fake.created if order["type"] == "LIMIT"]
    assert limit_orders[0]["client_order_id"] != limit_orders[1]["client_order_id"]


def test_second_tp_only_after_first(engine):
    execution, fake, rules = engine
    automation = build_automation(execution, rules, execution.events)
    position = make_position(rules)

    automation.run_cycle(position, 86600.0)  # TP1 (86520)
    automation.run_cycle(position, 89500.0)  # TP2 (89040)

    assert position.sorted_tps[0].status is TPStatus.EXECUTED
    assert position.sorted_tps[1].status is TPStatus.EXECUTED
    assert position.metrics.net_qty == pytest.approx(0.0, abs=1e-8)
    assert position.status is PositionStatus.CLOSED


def test_full_tp_cycle_closes_position(engine):
    execution, fake, rules = engine
    automation = build_automation(execution, rules, execution.events)
    position = make_position(rules)

    automation.run_cycle(position, 86600.0)
    outcome = automation.run_cycle(position, 89500.0)

    assert outcome.position_finished
    assert position.close_reason is not None


# ==========================================================================
# SL evolutif
# ==========================================================================


def test_sl_moves_to_break_even_after_tp1(engine):
    execution, fake, rules = engine
    automation = build_automation(execution, rules, execution.events)
    position = make_position(rules, tp_rules={1: SLRuleAfterTP.BREAK_EVEN})

    outcome = automation.run_cycle(position, 86600.0)

    assert outcome.sl_moved_to is not None
    assert outcome.sl_moved_to >= position.metrics.average_price - 1
    assert position.stop_loss.status is SLStatus.ACTIVE
    # Un seul SL a la fois : l'ancien a bien ete annule.
    assert SL_ORDER_ID in fake.cancelled


def test_sl_previous_tp_rule(engine):
    """Apres TP2 partiel, la regle PREVIOUS_TP monte le SL au niveau du TP1.

    Le TP2 ne vend que 40 % : s'il vidait la position, `_apply_sl_rule`
    sortirait aussitot (plus rien a proteger) et le SL resterait inchange —
    ce qui est le comportement correct, mais ne teste pas la regle.
    """
    execution, fake, rules = engine
    automation = build_automation(execution, rules, execution.events)
    position = make_position(
        rules, tp_rules={1: SLRuleAfterTP.NO_CHANGE, 2: SLRuleAfterTP.PREVIOUS_TP}
    )
    position.sorted_tps[1].sell_percent = 40.0
    recompute_position(position)

    automation.run_cycle(position, 86600.0)  # TP1 (86520)
    automation.run_cycle(position, 89500.0)  # TP2 (89040)

    tp1_price = position.sorted_tps[0].target_price
    assert position.metrics.net_qty > 0
    assert position.stop_loss.resolved_price == pytest.approx(
        float(rules.round_price(tp1_price, mode="down")), rel=1e-6
    )
    assert position.stop_loss.status is SLStatus.ACTIVE


def test_sl_not_moved_when_position_fully_sold(engine):
    """Quand le dernier TP solde la position, aucun SL n'est replace.

    C'est le pendant du test precedent : un SL n'a plus d'objet quand il ne
    reste rien a proteger. La position se ferme, la protection est annulee.
    """
    execution, fake, rules = engine
    automation = build_automation(execution, rules, execution.events)
    position = make_position(
        rules, tp_rules={1: SLRuleAfterTP.NO_CHANGE, 2: SLRuleAfterTP.PREVIOUS_TP}
    )

    automation.run_cycle(position, 86600.0)  # TP1
    automation.run_cycle(position, 89500.0)  # TP2 vend la totalite

    assert position.metrics.net_qty == pytest.approx(0.0, abs=1e-8)
    assert position.status is PositionStatus.CLOSED
    assert position.stop_loss.status is SLStatus.CANCELED


def test_sl_replacement_failure_is_logged_not_silent(settings, rules, events):
    """Si la recreation du SL echoue, l'echec est journalise, jamais masque."""
    fake = FakeClient(rules, fail_on_stop_loss=True)
    fake.seed_stop_loss()
    execution = build_execution(settings, rules, events, fake)
    automation = build_automation(execution, rules, events)
    position = make_position(rules, tp_rules={1: SLRuleAfterTP.BREAK_EVEN})

    outcome = automation.run_cycle(position, 86600.0)

    assert outcome.errors, "l'echec du replacement de SL doit remonter"
    logged = [e for e in execution.events.tail(100) if e["level"] in {"ERROR", "CRITICAL"}]
    assert logged, "l'echec doit etre journalise en ERROR/CRITICAL"


def test_sl_not_replaced_under_min_qty(engine):
    """Sous minQty, aucun SL fantome : la position est signalee non protegee.

    Le test cible directement `_apply_sl_rule` : passer par un cycle complet
    ferait vendre un TP et modifierait la quantite avant d'atteindre la branche
    que l'on veut verifier.
    """
    execution, fake, rules = engine
    automation = build_automation(execution, rules, execution.events)
    position = make_position(rules)

    # Quantite restante sous minQty (0.00001) mais au-dessus de QTY_EPSILON :
    # le moteur doit refuser de recreer un SL et le signaler.
    position.entries[0].executed_qty = 0.000005
    recompute_position(position)
    assert position.metrics.net_qty < float(rules.min_qty)

    tp = position.sorted_tps[0]
    tp.sl_rule_after_hit = SLRuleAfterTP.BREAK_EVEN
    result = CycleResult(position_id=position.position_id, symbol=position.symbol)

    automation._apply_sl_rule(position, tp, result)

    assert position.stop_loss.status is SLStatus.NONE
    assert any("non replace" in a or "non protegee" in a for a in result.actions)
    logged = [
        e
        for e in execution.events.tail(100)
        if e["level"] in {"ERROR", "CRITICAL"} and "minQty" in e.get("message", "")
    ]
    assert logged, "le cas doit etre journalise en ERROR/CRITICAL"


# ==========================================================================
# Idempotence
# ==========================================================================


def test_no_duplicate_order_on_repeat_call(engine):
    """Rejouer le meme envoi ne doit pas creer un second ordre."""
    execution, fake, rules = engine
    position = make_position(rules, entry_status=EntryStatus.PLANNED)
    entry = position.sorted_entries[0]
    entry.client_order_id = "BSM-D-BTC-rep-E1"
    entry.order_type = OrderType.LIMIT
    entry.resolved_price = 80000.0

    first = execution.place_entry(position, entry, current_price=84000.0)
    after_first = len(fake.created)
    second = execution.place_entry(position, entry, current_price=84000.0)

    assert first.success
    assert second.client_order_id == first.client_order_id
    assert len(fake.created) == after_first


def test_duplicate_client_order_id_is_adopted(engine):
    execution, fake, rules = engine
    position = make_position(rules, entry_status=EntryStatus.PLANNED)
    entry = position.sorted_entries[0]
    entry.order_type = OrderType.LIMIT
    entry.resolved_price = 80000.0
    entry.binance_qty = 0.001
    entry.client_order_id = "BSM-D-BTC-dup-E1"

    fake.orders["BSM-D-BTC-dup-E1"] = {
        "symbol": "BTCUSDT",
        "orderId": 4242,
        "clientOrderId": "BSM-D-BTC-dup-E1",
        "side": "BUY",
        "type": "LIMIT",
        "status": "NEW",
        "price": "80000",
        "stopPrice": "0",
        "origQty": "0.001",
        "executedQty": "0",
        "cummulativeQuoteQty": "0",
        "fills": [],
    }

    result = execution.place_entry(position, entry, current_price=84000.0)

    assert result.order_id == 4242
    assert len(fake.created) == 0  # aucune creation


def test_filter_failure_is_not_retried(engine):
    execution, fake, rules = engine
    fake.fail_next = BinanceError("Filter failure: MIN_NOTIONAL", code=-1013)
    position = make_position(rules, entry_status=EntryStatus.PLANNED)
    entry = position.sorted_entries[0]
    entry.client_order_id = "BSM-D-BTC-filt-E1"
    entry.order_type = OrderType.LIMIT
    entry.resolved_price = 80000.0

    result = execution.place_entry(position, entry, current_price=84000.0)

    assert not result.success
    assert len(fake.created) == 0


@pytest.mark.parametrize("error", [
    BinanceError("Account has insufficient balance", code=-2010, status=400),
    BinanceError("Too many requests", code=-1003, status=429, retry_after=30),
])
def test_business_error_or_rate_limit_never_retries_or_looks_up(engine, error):
    execution, fake, _ = engine
    attempts = []

    def refuse(**kwargs):
        attempts.append(kwargs)
        raise error

    fake.create_order = refuse
    fake.find_order = lambda *args, **kwargs: pytest.fail("unexpected lookup")
    result = execution._create_order_safe(
        symbol="BTCUSDT", side="BUY", order_type="MARKET",
        quantity=0.001, price=None, client_order_id="BSM-D-BTC-REFUSE",
        market=True,
    )

    assert not result.success
    assert result.status != "UNKNOWN"
    assert len(attempts) == 1


def test_ambiguous_order_without_confirmation_is_not_resent(engine):
    execution, fake, rules = engine
    position = make_position(rules, entry_status=EntryStatus.PLANNED)
    entry = position.sorted_entries[0]
    entry.order_type = OrderType.MARKET
    entry.client_order_id = "BSM-D-BTC-UNKNOWN-E1"
    attempts = []

    def timeout(**kwargs):
        attempts.append(kwargs)
        raise BinanceError("Echec reseau simule")

    fake.create_order = timeout
    result = execution.place_entry(position, entry, current_price=84000)

    assert result.status == "UNKNOWN"
    assert not result.success
    assert entry.status is EntryStatus.SUBMITTED
    assert entry.client_order_id == "BSM-D-BTC-UNKNOWN-E1"
    assert len(attempts) == 1


def test_ambiguous_stop_loss_is_not_duplicated_next_cycle(engine):
    execution, fake, rules = engine
    position = make_position(rules)
    position.stop_loss.status = SLStatus.PLANNED
    position.stop_loss.order_id = None
    position.stop_loss.client_order_id = None
    attempts = []

    def timeout(**kwargs):
        attempts.append(kwargs)
        raise BinanceError("Echec reseau simule")

    fake.create_order = timeout
    result = execution.place_stop_loss(
        position, stop_price=80640.0, quantity=0.006
    )
    assert result.status == "UNKNOWN"
    assert position.stop_loss.status is SLStatus.REPLACING
    assert position.stop_loss.client_order_id

    build_automation(execution, rules, execution.events).run_cycle(position, 84000)

    assert len(attempts) == 1


def test_ambiguous_tp_does_not_restore_stop_or_send_second_sell(engine):
    execution, fake, rules = engine
    fake.seed_stop_loss()
    position = make_position(rules)
    attempts = []

    def timeout(**kwargs):
        attempts.append(kwargs)
        raise BinanceError("Echec reseau simule")

    fake.create_order = timeout
    automation = build_automation(execution, rules, execution.events)
    automation.run_cycle(position, 86600)

    assert position.take_profits[0].status is TPStatus.SUBMITTED
    assert position.stop_loss.status is SLStatus.CANCELED
    assert position.sync_status.value == "DESYNC_DETECTED"
    assert len(attempts) == 1

    automation.run_cycle(position, 86600)
    assert len(attempts) == 1


def test_transport_failure_checks_before_retry(engine):
    """Un echec transport ne doit jamais aboutir a deux ordres.

    L'ordre est accepte cote Binance mais la reponse est perdue : le moteur
    doit interroger Binance avant de retenter, retrouver l'ordre, et l'adopter.
    """
    execution, fake, rules = engine
    position = make_position(rules, entry_status=EntryStatus.PLANNED)
    entry = position.sorted_entries[0]
    entry.order_type = OrderType.MARKET
    entry.client_order_id = "BSM-D-BTC-net-E1"

    def create_then_fail(**kwargs):
        cid = kwargs["client_order_id"]
        fake.orders[cid] = {
            "symbol": "BTCUSDT",
            "orderId": 777,
            "clientOrderId": cid,
            "side": "BUY",
            "type": "MARKET",
            "status": "FILLED",
            "price": "84000",
            "stopPrice": "0",
            "origQty": "0.006",
            "executedQty": "0.006",
            "cummulativeQuoteQty": "504",
            "fills": [],
        }
        raise BinanceError("Echec reseau simule")

    fake.create_order = create_then_fail  # type: ignore[assignment]

    result = execution.place_entry(position, entry, current_price=84000.0)

    assert result.success
    assert result.order_id == 777
    # Le SL seme + l'ordre de l'Entry, jamais un troisieme.
    assert len(fake.orders) == 2


# ==========================================================================
# DRY_RUN
# ==========================================================================


def dry_run_settings() -> Settings:
    return Settings(
        run_mode=RunMode.DRY_RUN,
        demo_base_url="[testnet.binance.vision](https://testnet.binance.vision)",
        demo_api_key="key",
        demo_api_secret="secret",
    )


def test_dry_run_sends_nothing(rules, events):
    fake = FakeClient(rules)
    fake.seed_stop_loss()
    execution = build_execution(dry_run_settings(), rules, events, fake)
    position = make_position(rules, entry_status=EntryStatus.PLANNED)
    entry = position.sorted_entries[0]
    entry.client_order_id = None

    result = execution.place_entry(position, entry, current_price=84000.0)

    assert result.dry_run
    assert fake.created == []
    skips = [e for e in events.tail(50) if e["event"] == "DRY_RUN_SKIP"]
    assert skips


def test_dry_run_automation_does_not_sell(rules, events):
    fake = FakeClient(rules)
    fake.seed_stop_loss()
    execution = build_execution(dry_run_settings(), rules, events, fake)
    automation = build_automation(execution, rules, events)
    position = make_position(rules)

    outcome = automation.run_cycle(position, 87000.0)

    assert fake.created == []
    assert position.sorted_tps[0].executed_qty == 0
    assert outcome.tp_executed is None
