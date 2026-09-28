"""Offline liquidation tests. No credentials, network or real orders."""
from types import SimpleNamespace

import pytest

from binance_spot_manager.config import RunMode, Settings
from binance_spot_manager.execution_engine import OrderResult
from binance_spot_manager.market_close import close_market, poll_market_close
from binance_spot_manager.models import (Commission, Entry, EntryStatus, OcoExit,
    Position, PositionStatus, SLStatus, TakeProfit, TPStatus)
from binance_spot_manager.position_engine import recompute_position
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.pnl_display import pnl_css, position_return
from binance_spot_manager.symbol_rules import parse_symbol_rules


@pytest.fixture
def setup(tmp_path):
    store = PositionStore(tmp_path)
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", status=PositionStatus.ACTIVE,
        entries=[Entry(status=EntryStatus.FILLED, order_id=1, executed_qty=.001,
                       quote_spent=80, average_fill_price=80000,
                       commissions=[Commission(asset="BTC", amount=.000001)])],
        take_profits=[TakeProfit(status=TPStatus.SUBMITTED, order_id=2)],
    )
    position.stop_loss.status, position.stop_loss.order_id = SLStatus.ACTIVE, 3
    position.metrics.current_price = 84000
    recompute_position(position)
    store.save(position)
    sibling = position.model_copy(deep=True)
    sibling.position_id = "pos_sibling"
    sibling.entries[0].order_id = 11
    sibling.take_profits[0].order_id = 12
    sibling.stop_loss.order_id = 13
    sibling.revision = 0
    store.save(sibling)
    rules = parse_symbol_rules({"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
                    {"filterType": "NOTIONAL", "minNotional": "5"}]})
    orders = {1: OrderResult(success=True, order_id=1, status="FILLED", executed_qty=.001,
        average_price=80000, cummulative_quote_qty=80, commissions=position.entries[0].commissions),
        2: OrderResult(success=True, order_id=2, status="NEW"),
        3: OrderResult(success=True, order_id=3, status="NEW")}
    calls = []

    def cancel(symbol, order_id=None, **kwargs):
        calls.append(("cancel", order_id))
        orders[order_id].status = "CANCELED"
        if position.oco_exit:
            orders[2].status = orders[3].status = "CANCELED"
        return orders[order_id]

    def sell(**kwargs):
        # Intent is durable before POST, and every protective order is terminal.
        saved = store.load(position.position_id)
        assert saved.manual_exits[-1].client_order_id == kwargs["client_order_id"]
        assert orders[2].status == orders[3].status == "CANCELED"
        calls.append(("sell", kwargs["quantity"]))
        qty = kwargs["quantity"]
        result = OrderResult(success=True, order_id=4, status="FILLED", executed_qty=qty,
            average_price=84000, cummulative_quote_qty=qty*84000,
            commissions=[Commission(asset="USDT", amount=qty*84000*.001)])
        orders[4] = result
        return result

    execution = SimpleNamespace(settings=Settings(run_mode=RunMode.DEMO_MANUAL,
        demo_api_key="test", demo_api_secret="test"), _dry_run=lambda: False,
        client=SimpleNamespace(get_price=lambda _: 84000), rules=lambda *a, **kw: rules,
        get_free_balance=lambda _: 1,
        fetch_order_status=lambda symbol, order_id=None, client_order_id=None: orders.get(order_id or 4),
        cancel_order=cancel, _create_order_safe=sell)
    return position, sibling, store, execution, orders, calls


def test_cancel_before_sale_net_fees_and_sibling_untouched(setup):
    position, sibling, store, execution, orders, calls = setup
    before = store.load(sibling.position_id).model_dump()
    result = close_market(position, execution, store)
    assert calls == [("cancel", 2), ("cancel", 3), ("sell", .00099)]
    assert result["quantity"] == .00099
    assert position.status == PositionStatus.CLOSED
    assert position.metrics.net_qty == pytest.approx(.000009)
    assert position.pnl.realized == pytest.approx(83.16 - .08316 - 80/.000999*.00099)
    assert store.load(sibling.position_id).model_dump() == before
    assert position.manual_exits[0].status == "FILLED"
    old = position.pnl.model_dump(exclude={"updated_at"})
    recompute_position(position)
    assert position.pnl.model_dump(exclude={"updated_at"}) == old


def test_oco_cancel_confirms_both_branches(setup):
    position, _, store, execution, _, calls = setup
    position.oco_exit = OcoExit(order_list_id=8, list_client_order_id="oco", tp_order_id=2, sl_order_id=3, quantity=.000999)
    close_market(position, execution, store)
    assert calls[:1] == [("cancel", 2)]
    assert ("cancel", 3) not in calls
    assert position.oco_exit.status == "CANCELED"


def test_unconfirmed_cancel_blocks_sale_and_retains_pause(setup):
    position, _, store, execution, _, calls = setup
    execution.cancel_order = lambda *a, **kw: OrderResult(success=False, status="UNKNOWN")
    with pytest.raises(RuntimeError, match="Annulation non confirmee"):
        close_market(position, execution, store)
    assert not calls
    assert store.load(position.position_id).automation.paused


def test_timeout_intent_prevents_second_sell_and_recovers_read_only(setup):
    position, _, store, execution, orders, calls = setup
    post = execution._create_order_safe
    def timeout(**kwargs):
        post(**kwargs)  # exchange executed, response lost
        raise TimeoutError("lost response")
    execution._create_order_safe = timeout
    with pytest.raises(TimeoutError):
        close_market(position, execution, store)
    position = store.load(position.position_id)
    assert position.manual_exits[0].status == "UNKNOWN"
    with pytest.raises(RuntimeError, match="non resolue"):
        close_market(position, execution, store)
    assert poll_market_close(position, execution)
    assert position.status == PositionStatus.CLOSED
    assert len([call for call in calls if call[0] == "sell"]) == 1


def test_partial_buy_fill_during_cancel_is_included(setup):
    position, _, store, execution, orders, calls = setup
    position.entries.append(Entry(status=EntryStatus.SUBMITTED, order_id=5))
    orders[5] = OrderResult(success=True, order_id=5, status="PARTIALLY_FILLED", executed_qty=.0002,
                           cummulative_quote_qty=16, average_price=80000)
    close_market(position, execution, store)
    assert calls[-1] == ("sell", .00119)
    assert position.entries[-1].executed_qty == .0002


def test_dry_run_does_not_cancel_sell_or_mutate(setup):
    position, _, store, execution, _, calls = setup
    execution._dry_run = lambda: True
    before = position.model_dump()
    close_market(position, execution, store)
    assert position.model_dump() == before
    assert not calls


def test_insufficient_free_balance_never_sells_another_strategy(setup):
    position, _, store, execution, _, calls = setup
    execution.get_free_balance = lambda _: .0001
    with pytest.raises(RuntimeError, match="Solde libre insuffisant"):
        close_market(position, execution, store)
    assert not any(call[0] == "sell" for call in calls)


def test_return_uses_real_conversion_and_sign_colors(setup):
    position = setup[0]
    position.quote_asset = "USDC"
    result = position_return(position, .98)
    assert result["total_usdt"] == pytest.approx((.000999*84000-80)*.98)
    assert result["percent"] == pytest.approx((.000999*84000-80)/80*100)
    assert position_return(position, None)["total_usdt"] is None
    assert "16a34a" in pnl_css("+1 250.00 USDT")
    assert "ef4444" in pnl_css("-2.50 %")
    assert pnl_css("—") == ""


def test_market_close_button_queues_selected_id_without_direct_sale(setup, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    import ui_common
    from binance_spot_manager.event_store import EventStore
    position, sibling, store, execution, orders, calls = setup
    queued = []
    service = SimpleNamespace(positions=store, settings=execution.settings,
        events=EventStore(store.directory.parent / "events.jsonl"),
        rules_cache=SimpleNamespace(get=lambda _: execution.rules()), current_price=lambda _: 84000,
        open_orders=lambda _: ([], ""))
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    monkeypatch.setattr(ui_common, "sidebar_status", lambda _: None)
    monkeypatch.setattr(ui_common, "new_command_confirmation", lambda _: None)
    monkeypatch.setattr(ui_common, "submit_to_worker", lambda action, payload, **kwargs: queued.append((action, payload)))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "pages" / "3_Positions.py"), default_timeout=15).run()
    assert not app.exception
    app.selectbox[0].select(position.position_id).run()
    assert not app.exception
    app.checkbox(key=f"confirm_market_{position.position_id}").check().run()
    next(button for button in app.button if button.label == "Annuler les TP/SL et vendre au marché").click().run()
    assert not app.exception
    assert queued == [("CLOSE_MARKET", {"position_id": position.position_id})]
    assert not calls


def test_partial_market_fill_stays_open_until_confirmed(setup):
    position, _, store, execution, orders, calls = setup
    original = execution._create_order_safe
    def partial(**kwargs):
        result = original(**kwargs)
        result.status = "PARTIALLY_FILLED"
        result.executed_qty /= 2
        result.cummulative_quote_qty /= 2
        result.commissions[0].amount /= 2
        return result
    execution._create_order_safe = partial
    with pytest.raises(RuntimeError, match="attente de confirmation"):
        close_market(position, execution, store)
    assert position.status == PositionStatus.CLOSING
    assert position.pnl.realized > 0
    orders[4].status = "FILLED"
    orders[4].executed_qty *= 2
    orders[4].cummulative_quote_qty *= 2
    orders[4].commissions[0].amount *= 2
    assert poll_market_close(position, execution)
    assert position.status == PositionStatus.CLOSED
    assert len([call for call in calls if call[0] == "sell"]) == 1


def test_live_snapshot_refreshes_price_and_accounting_without_saving(setup):
    import ui_common
    position, _, store, execution, _, calls = setup
    price = [84000]
    service = SimpleNamespace(positions=store, current_price=lambda _: price[0])
    before = store.load(position.position_id).model_dump()
    first, _, first_return = ui_common.fresh_position_view(service, position.position_id)
    price[0] = 75000
    second, _, second_return = ui_common.fresh_position_view(service, position.position_id)
    assert first_return["total_usdt"] > 0
    assert second_return["total_usdt"] < 0
    assert first.metrics.current_price == 84000
    assert second.metrics.current_price == 75000
    assert store.load(position.position_id).model_dump() == before
    assert not calls


def test_live_summary_fragment_preserves_editor_and_reads_worker_changes(setup, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from binance_spot_manager.event_store import EventStore
    import ui_common
    position, sibling, store, execution, _, calls = setup
    store.delete(sibling.position_id)
    price = [84000]
    service = SimpleNamespace(positions=store, settings=execution.settings,
        events=EventStore(store.directory.parent / "events.jsonl"),
        rules_cache=SimpleNamespace(get=lambda _: execution.rules()),
        current_price=lambda _: price[0], open_orders=lambda _: ([], ""))
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    monkeypatch.setattr(ui_common, "sidebar_status", lambda _: None)
    monkeypatch.setattr(ui_common, "new_command_confirmation", lambda _: None)
    page = Path(__file__).resolve().parents[1] / "pages" / "3_Positions.py"
    script = ("import streamlit as st\nimport runpy\n"
              "st.session_state['full_runs'] = st.session_state.get('full_runs', 0) + 1\n"
              f"runpy.run_path({str(page)!r})\n"
              "st.button('Tick summary', key='tick_summary', on_click=lambda: st.rerun('position_summary'))\n")
    app = AppTest.from_string(script, default_timeout=15).run()
    assert not app.exception
    app.number_input(key="sl_new_price").set_value(79000).run()
    assert not app.exception
    full_runs = app.session_state["full_runs"]
    price[0] = 75000
    fresh = store.load(position.position_id)
    fresh.stop_loss.resolved_price = 78000
    store.save(fresh)
    app.button(key="tick_summary").click().run()
    assert not app.exception
    assert app.session_state["full_runs"] == full_runs
    # AppTest returns the fragment's tree on a targeted rerun; editor widgets
    # outside it remain in Session State (and untouched in the browser).
    assert app.session_state["sl_new_price"] == 79000
    assert next(m for m in app.metric if m.label == "Prix actuel").value == "75 000.00"
    assert any(":red[" in m.value and "USDT" in m.value for m in app.markdown)
    assert next(m for m in app.metric if m.label == "SL").value.startswith("78 000.00")
    assert not calls


def test_portfolio_revalues_from_stream_and_shares_one_second_rest_reads(setup, monkeypatch):
    from binance_spot_manager.dashboard_service import DashboardService
    position, _, store, execution, _, _ = setup
    balance_reads, price_reads = [], []
    client = SimpleNamespace(
        get_balances=lambda: (balance_reads.append(True) or {"USDT": {"free": 1000, "locked": 0}}),
        get_prices=lambda *args: (price_reads.append(True) or {"BTCUSDT": 75000, "EURUSDT": 1.2}))
    service = DashboardService(settings=execution.settings, position_store=store, client=client)
    service.market_prices = SimpleNamespace(prices=lambda symbols: {symbol: 75000 for symbol in symbols})
    now = [0.0]
    monkeypatch.setattr("binance_spot_manager.dashboard_service.time.monotonic", lambda: now[0])
    view = service.portfolio()
    assert view.unrealized_pnl < 0
    assert service.position_rows()[0].current_price == 75000
    assert not price_reads  # ticker data came from WebSocket, not REST
    service.wallet_valuation()
    assert len(balance_reads) == 1
    now[0] = 1.01
    service.portfolio()
    assert len(balance_reads) == 2
