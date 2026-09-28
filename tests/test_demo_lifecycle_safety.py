"""Gardes hors ligne du test a ordres reels Demo."""

from types import SimpleNamespace

import pytest

from binance_spot_manager.config import Environment, RunMode, SecurityError, Settings
from binance_spot_manager.models import Position
from scripts import test_oco_lifecycle_demo as lifecycle

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status", ["CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"])
def test_completed_oco_allows_unexecuted_terminal_sibling(status):
    lifecycle.confirm_single_oco_fill(
        {"status": "FILLED", "executedQty": "0.0053"},
        {"status": status, "executedQty": "0"},
    )


@pytest.mark.parametrize("status, qty", [("NEW", "0"), ("FILLED", "0.0053"), ("EXPIRED", "0.001")])
def test_completed_oco_rejects_open_or_executed_sibling(status, qty):
    with pytest.raises(RuntimeError):
        lifecycle.confirm_single_oco_fill(
            {"status": "FILLED", "executedQty": "0.0053"},
            {"status": status, "executedQty": qty},
        )


@pytest.mark.parametrize("settings", [
    Settings(),
    Settings(environment=Environment.LIVE, run_mode=RunMode.DEMO_MANUAL),
    Settings(run_mode=RunMode.DEMO_MANUAL, demo_base_url="https://api.binance.com"),
])
def test_lifecycle_forbidden_environment_never_constructs_client(monkeypatch, settings):
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle, "BinanceSpotClient", lambda *a: pytest.fail("Forbidden API access"))
    with pytest.raises(SecurityError):
        lifecycle.run()


def test_lifecycle_does_not_buy_on_an_existing_position(monkeypatch):
    settings = Settings(run_mode=RunMode.DEMO_MANUAL, demo_api_key="test", demo_api_secret="test")
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle, "BotProcessManager", lambda *a: SimpleNamespace(is_running=lambda: True))
    monkeypatch.setattr(lifecycle, "BinanceSpotClient", lambda *a: SimpleNamespace())
    monkeypatch.setattr(lifecycle, "PositionStore", lambda: SimpleNamespace(
        list_open=lambda: [Position(symbol="ETHUSDT")],
    ))
    with pytest.raises(RuntimeError, match="deja utilise"):
        lifecycle.run()
