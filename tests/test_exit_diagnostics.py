"""Le controle des sorties ne modifie jamais les positions ni les ordres."""

from types import SimpleNamespace

import pytest

from binance_spot_manager.config import Environment, RunMode, Settings
from binance_spot_manager.exit_diagnostics import inspect_exits
from binance_spot_manager.models import OcoExit, Position, SLStatus, TakeProfit, TPStatus

pytestmark = pytest.mark.unit


def fake_client(**methods):
    return SimpleNamespace(get_symbol_info=lambda symbol: {
        "symbol": symbol, "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"}],
    }, **methods)


def demo_settings(**kwargs):
    return Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test", **kwargs)


def oco_position():
    position = Position(symbol="BTCUSDT", base_asset="BTC")
    position.oco_exit = OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00035,
    )
    position.take_profits = [TakeProfit(target_price=85000)]
    position.stop_loss.resolved_price = 80000
    return position


def order(order_id, **changes):
    return {
        "orderId": order_id, "orderListId": 1, "symbol": "BTCUSDT",
        "side": "SELL", "status": "NEW", "origQty": "0.00035",
        "executedQty": "0", "price": "79760" if order_id == 11 else "85000",
        "stopPrice": "80000" if order_id == 11 else "0",
        "type": "STOP_LOSS_LIMIT" if order_id == 11 else "LIMIT_MAKER", **changes,
    }


def test_oco_report_reads_both_branches_without_mutation():
    position = oco_position()
    before = position.model_dump_json()
    calls = []

    def get_order(symbol, *, order_id=None, client_order_id=None):
        calls.append((symbol, order_id, client_order_id))
        return order(order_id)

    report = inspect_exits(demo_settings(), fake_client(get_order=get_order), [position])
    assert calls == [("BTCUSDT", 10, None), ("BTCUSDT", 11, None)]
    assert [row["Resultat"] for row in report["rows"]] == ["OK", "OK"]
    assert position.model_dump_json() == before


@pytest.mark.parametrize("changes", [
    {"status": "FILLED", "executedQty": "0.00035"},
    {"status": "CANCELED"}, {"status": "PARTIALLY_FILLED"},
    {"origQty": "0.00036"}, {"orderListId": 2}, {"side": "BUY"},
    {"symbol": "ETHUSDT"}, {"orderId": 999},
    {"type": "MARKET"}, {"price": "1"},
])
def test_oco_mismatch_is_not_reported_as_ok(changes):
    position = oco_position()
    before = position.model_dump_json()
    client = fake_client(get_order=lambda symbol, **kw: order(kw["order_id"], **changes))
    report = inspect_exits(demo_settings(), client, [position])
    assert all(row["Resultat"] == "ECART" for row in report["rows"])
    assert position.model_dump_json() == before


def test_unreadable_order_does_not_prevent_next_check():
    def get_order(symbol, *, order_id=None, client_order_id=None):
        if order_id == 10:
            raise TimeoutError("timeout")
        return order(order_id)

    report = inspect_exits(demo_settings(), fake_client(get_order=get_order), [oco_position()])
    assert [row["Resultat"] for row in report["rows"]] == ["INVERIFIABLE", "OK"]


@pytest.mark.parametrize("settings", [
    Settings(), demo_settings(environment=Environment.LIVE),
    demo_settings(demo_base_url="https://api.binance.com"),
    Settings(run_mode=RunMode.DEMO_MANUAL),
])
def test_guards_block_all_requests(settings):
    with pytest.raises(ValueError):
        inspect_exits(settings, SimpleNamespace(), [oco_position()])


def test_local_tp_is_not_mistaken_for_binance_protection():
    position = Position(symbol="BTCUSDT")
    position.take_profits = [TakeProfit(target_price=85000)]
    position.stop_loss.status = SLStatus.ACTIVE
    report = inspect_exits(demo_settings(), SimpleNamespace(), [position])
    assert [row["Resultat"] for row in report["rows"]] == ["INFO", "ECART"]


def test_legacy_tp_lookup_by_client_id_is_read_only():
    position = Position(symbol="BTCUSDT")
    position.take_profits = [TakeProfit(
        status=TPStatus.SUBMITTED, client_order_id="tp-test", estimated_qty=0.00035, target_price=85000,
    )]
    before = position.model_dump_json()
    calls = []

    def get_order(symbol, *, order_id=None, client_order_id=None):
        calls.append((order_id, client_order_id))
        return order(10, clientOrderId="tp-test")

    report = inspect_exits(demo_settings(), SimpleNamespace(get_order=get_order), [position])
    assert calls == [(None, "tp-test")]
    assert report["rows"][0]["Resultat"] == "OK"
    assert position.model_dump_json() == before
