"""Prototype OCO : calcul et garde-fous, sans appel réseau ni ordre."""

import pytest

from binance_spot_manager.binance_client import BinanceSpotClient
from binance_spot_manager.config import Environment, RunMode, SecurityError, Settings
from binance_spot_manager.models import (
    Commission,
    Entry,
    EntryStatus,
    Position,
    PositionStatus,
    SLStatus,
    TakeProfit,
)
from binance_spot_manager.oco_preview import preview_oco_sell
from binance_spot_manager.position_engine import recompute_position
from binance_spot_manager.symbol_rules import parse_symbol_rules
from scripts.migrate_oco_demo import _confirmed_order_list

pytestmark = pytest.mark.unit


@pytest.fixture
def rules():
    return parse_symbol_rules(
        {
            "symbol": "BTCUSDT",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "9000"},
                {"filterType": "NOTIONAL", "minNotional": "5"},
            ],
        }
    )


def make_position(*, active_sl=True):
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    position.entries.append(
        Entry(
            status=EntryStatus.FILLED,
            executed_qty=0.00035,
            average_fill_price=84000,
            quote_spent=29.4,
            commissions=[Commission(asset="BTC", amount=0.00000035)],
        )
    )
    position.take_profits.append(TakeProfit(target_price=85000, sell_percent=100))
    position.stop_loss.resolved_price = 81000
    position.stop_loss.status = SLStatus.ACTIVE if active_sl else SLStatus.PLANNED
    recompute_position(position)
    return position


def test_preview_rounds_fees_and_blocks_existing_stop(rules):
    preview = preview_oco_sell(make_position(), rules, 84000)

    assert preview.quantity == "0.00034"
    assert preview.request_params["aboveType"] == "LIMIT_MAKER"
    assert preview.request_params["belowType"] == "STOP_LOSS_LIMIT"
    assert preview.request_params["belowTimeInForce"] == "GTC"
    assert preview.request_params["abovePrice"] == "85000"
    assert preview.request_params["belowStopPrice"] == "81000"
    assert not preview.eligible
    assert any("SL indépendant" in reason for reason in preview.blockers)


def test_preview_rejects_tp_below_market(rules):
    preview = preview_oco_sell(make_position(active_sl=False), rules, 86000)

    assert not preview.eligible
    assert any("Prix OCO invalides" in reason for reason in preview.blockers)


def test_preview_is_coherent_without_legacy_stop(rules):
    preview = preview_oco_sell(make_position(active_sl=False), rules, 84000)

    assert preview.eligible
    assert preview.request_params["quantity"] == "0.00034"
    assert float(preview.stop_limit_price) < float(preview.stop_price) < 84000


def test_preview_rejects_multiple_take_profits(rules):
    position = make_position(active_sl=False)
    position.take_profits.append(TakeProfit(target_price=87000, sell_percent=50))

    preview = preview_oco_sell(position, rules, 84000)

    assert not preview.eligible
    assert any("un seul TP" in reason for reason in preview.blockers)


def test_oco_transport_requires_explicit_confirmation(monkeypatch):
    client = BinanceSpotClient(Settings(run_mode=RunMode.DEMO_AUTO))
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: pytest.fail("ordre envoyé"))

    with pytest.raises(ValueError, match="confirmation explicite"):
        client.create_oco_sell({"side": "SELL"})


def test_oco_transport_uses_signed_demo_endpoint(monkeypatch):
    client = BinanceSpotClient(Settings(run_mode=RunMode.DEMO_AUTO))
    calls = []
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: calls.append((args, kwargs)) or {})

    client.create_oco_sell({"side": "SELL", "symbol": "BTCUSDT"}, experimental_confirmation=True)

    assert calls == [
        (("POST", "/api/v3/orderList/oco"), {"params": {"side": "SELL", "symbol": "BTCUSDT"}, "signed": True})
    ]


def test_oco_transport_refuses_live_before_network(monkeypatch):
    client = BinanceSpotClient(Settings(environment=Environment.LIVE, run_mode=RunMode.LIVE))
    monkeypatch.setattr(
        client._session, "request", lambda *args, **kwargs: pytest.fail("requête envoyée")
    )

    with pytest.raises(SecurityError):
        client.create_oco_sell({"side": "SELL"}, experimental_confirmation=True)


def test_oco_response_identifies_both_branches():
    response = {"orders": [
        {"clientOrderId": "sl-id", "orderId": 22},
        {"clientOrderId": "tp-id", "orderId": 21},
    ]}

    above, below = _confirmed_order_list(response, "tp-id", "sl-id")

    assert above["orderId"] == 21
    assert below["orderId"] == 22
