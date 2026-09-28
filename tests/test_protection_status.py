"""Les alertes locales ne certifient pas une protection Binance et ne mutent rien."""

import pytest

from binance_spot_manager.models import OcoExit, Position, PositionStatus, SLStatus, SyncStatus
from binance_spot_manager.protection_status import protection_alerts

pytestmark = pytest.mark.unit


def active_position():
    position = Position(symbol="BTCUSDT", status=PositionStatus.ACTIVE)
    position.metrics.net_qty = 0.00035
    return position


@pytest.mark.parametrize("status,missing,severity", [
    ("ACTIVE", False, None), ("ACTIVE", True, "CRITICAL"),
    ("FAILED", False, "CRITICAL"), ("ERROR", False, "CRITICAL"),
    ("PARTIAL", False, "CRITICAL"), ("PARTIAL_TERMINAL", False, "CRITICAL"),
    ("FILLED", False, "WARNING"),
])
def test_oco_warnings_are_read_only(status, missing, severity):
    position = active_position()
    position.oco_exit = OcoExit(
        order_list_id=1, list_client_order_id="oco", tp_order_id=10,
        sl_order_id=11, quantity=0.00035, status=status, missing_branch_alerted=missing,
    )
    before = position.model_dump_json()
    alerts = protection_alerts([position])
    assert position.model_dump_json() == before
    if severity is None:
        assert alerts == []
    else:
        assert len(alerts) == 1
        assert alerts[0]["severity"] == severity


def test_tp_only_warns_without_claiming_an_error():
    position = active_position()
    position.stop_loss.status = SLStatus.NONE
    alert = protection_alerts([position])[0]
    assert alert["severity"] == "WARNING"
    assert "volontaire" in alert["message"]


def test_active_sl_without_identifiers_is_critical():
    position = active_position()
    position.stop_loss.status = SLStatus.ACTIVE
    assert protection_alerts([position])[0]["severity"] == "CRITICAL"


def test_desync_and_pause_are_visible_with_active_sl():
    position = active_position()
    position.stop_loss.status = SLStatus.ACTIVE
    position.stop_loss.order_id = 10
    position.sync_status = SyncStatus.DESYNC_DETECTED
    position.automation.paused = True
    message = protection_alerts([position])[0]["message"]
    assert "Synchronisation" in message
    assert "pause" in message


def test_closed_or_empty_position_has_no_protection_warning():
    closed = active_position()
    closed.status = PositionStatus.CLOSED
    empty = active_position()
    empty.metrics.net_qty = 0
    assert protection_alerts([closed, empty]) == []
