"""Les tranches OCO ne peuvent engager que le solde net et vendable."""

from decimal import Decimal

import pytest

from binance_spot_manager.models import (
    Commission, Entry, EntryStatus, Position, PositionStatus, SLStatus, TakeProfit,
)
from binance_spot_manager.multi_oco_preview import preview_multi_oco
from binance_spot_manager.position_engine import recompute_position
from binance_spot_manager.symbol_rules import parse_symbol_rules

pytestmark = pytest.mark.unit


def _rules(*, min_notional="5", algo_limit="5"):
    return parse_symbol_rules({
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
        "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
            {"filterType": "NOTIONAL", "minNotional": min_notional},
            {"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": algo_limit},
        ],
    })


def _position(percentages=(50, 30, 20)):
    position = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    position.status = PositionStatus.ACTIVE
    position.entries.append(Entry(
        status=EntryStatus.FILLED, executed_qty=0.00035,
        average_fill_price=84000, quote_spent=29.4,
        commissions=[Commission(asset="BTC", amount=0.00000035)],
    ))
    position.take_profits = [
        TakeProfit(sequence_number=i, target_price=85000 + i * 1000, sell_percent=p)
        for i, p in enumerate(percentages, 1)
    ]
    position.stop_loss.status = SLStatus.PLANNED
    position.stop_loss.resolved_price = 81000
    recompute_position(position)
    return position


def test_splits_net_quantity_and_last_tranche_absorbs_rounding():
    preview = preview_multi_oco(_position(), _rules(), 84000)
    assert preview.eligible
    assert [part.quantity for part in preview.tranches] == ["0.00017", "0.0001", "0.00007"]
    assert sum(Decimal(part.quantity) for part in preview.tranches) == Decimal("0.00034")
    assert all(part.request_params["belowType"] == "STOP_LOSS_LIMIT" for part in preview.tranches)


def test_rejects_tranche_below_min_notional():
    preview = preview_multi_oco(_position((80, 10, 10)), _rules(), 84000)
    assert not preview.eligible
    assert any("minNotional" in reason for reason in preview.blockers)


def test_rejects_algo_limit_and_existing_active_stop():
    position = _position()
    position.stop_loss.status = SLStatus.ACTIVE
    preview = preview_multi_oco(position, _rules(algo_limit="2"), 84000)
    assert not preview.eligible
    assert any("MAX_NUM_ALGO_ORDERS" in reason for reason in preview.blockers)
    assert any("SL indépendant" in reason for reason in preview.blockers)
