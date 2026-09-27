"""Le flux public Demo n'est utilise qu'avec un prix valide et recent."""

import json
from types import SimpleNamespace
import pytest

from binance_spot_manager.binance_client import BinanceSpotClient
from binance_spot_manager.config import Environment, Settings
from binance_spot_manager.dashboard_service import DashboardService
from binance_spot_manager.market_price_stream import DemoMarketPriceStream
import binance_spot_manager.market_price_stream as market_stream
from binance_spot_manager.models import BotRuntime


def test_stream_accepts_only_fresh_watched_demo_prices(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(market_stream.time, "monotonic", lambda: now[0])
    stream = DemoMarketPriceStream(Settings(), connect=lambda *a, **k: None)
    stream._thread = SimpleNamespace(is_alive=lambda: True)
    assert stream.prices(["BTCUSDT"]) == {}

    stream._accept(json.dumps({"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "85000.50"}))
    stream._accept(json.dumps({"e": "24hrMiniTicker", "s": "ETHUSDT", "c": "2000"}))
    assert stream.prices(["BTCUSDT", "ETHUSDT"]) == {"BTCUSDT": 85000.5}

    now[0] += 6
    assert stream.prices(["BTCUSDT"]) == {}


def test_stream_never_connects_to_live_or_unknown_demo_host():
    assert not DemoMarketPriceStream(
        Settings(environment=Environment.LIVE), connect=lambda *a, **k: None
    ).enabled
    assert not DemoMarketPriceStream(
        Settings(demo_base_url="https://unknown.example"),
        connect=lambda *a, **k: None,
    ).enabled


@pytest.mark.parametrize("rest_url,stream_url", [
    ("https://testnet.binance.vision", "wss://stream.testnet.binance.vision/ws"),
    ("https://demo-api.binance.com/", "wss://demo-stream.binance.com/ws"),
])
def test_stream_connects_to_matching_demo_host(rest_url, stream_url):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send(self, raw):
            assert json.loads(raw)["params"] == ["btcusdt@miniTicker"]

        def recv(self, **kwargs):
            stream._last_access = market_stream.time.monotonic() - 61
            return json.dumps({"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "85000"})

    def connect(url, **kwargs):
        calls.append(url)
        return Connection()

    stream = DemoMarketPriceStream(Settings(demo_base_url=rest_url), connect=connect)
    assert stream.enabled
    stream._watched.add("BTCUSDT")
    stream._run()
    assert calls == [stream_url]
    assert stream._latest["BTCUSDT"][0] == 85000


@pytest.mark.parametrize("settings", [
    Settings(environment=Environment.LIVE),
    Settings(demo_base_url="https://api.binance.com"),
    Settings(demo_base_url="https://demo-api.binance.com.evil.example"),
])
def test_disabled_stream_cannot_start_connection(settings):
    stream = DemoMarketPriceStream(
        settings, connect=lambda *a, **k: pytest.fail("Forbidden WebSocket connection"),
    )
    assert stream.prices(["BTCUSDT"]) == {}
    stream._run()
    assert stream.snapshot()["state"] == "DISABLED"


def test_snapshot_is_read_only_and_reports_price_age(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(market_stream.time, "monotonic", lambda: now[0])
    calls = []
    stream = DemoMarketPriceStream(Settings(), connect=lambda *a, **k: calls.append(a))
    assert stream.snapshot()["state"] == "IDLE"
    assert stream.snapshot()["symbols"] == []
    assert stream._thread is None
    stream._thread = SimpleNamespace(is_alive=lambda: True)
    stream.prices(["BTCUSDT"])
    stream._accept(json.dumps({"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "85000"}))
    last_access = stream._last_access
    now[0] += 6
    snapshot = stream.snapshot()
    assert snapshot["symbols"] == [{"symbol": "BTCUSDT", "age_seconds": 6.0, "fresh": False}]
    assert stream._last_access == last_access
    assert calls == []
    json.dumps(snapshot)


def test_disabled_stream_diagnostic():
    stream = DemoMarketPriceStream(Settings(environment=Environment.LIVE))
    diagnostic = stream.snapshot()
    assert diagnostic["state"] == "DISABLED"
    assert diagnostic["disabled_reason"]
    assert stream._thread is None


def test_old_worker_runtime_accepts_missing_price_diagnostics():
    assert BotRuntime.model_validate({"loop_count": 42}).price_diagnostics == {}


def test_snapshot_reports_reconnection_then_idle_without_network(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("offline")

    stream = DemoMarketPriceStream(Settings(), connect=unavailable)
    observed = []

    def backoff(seconds):
        observed.append(stream.snapshot())
        stream._last_access = market_stream.time.monotonic() - 61

    monkeypatch.setattr(market_stream.time, "sleep", backoff)
    stream._run()
    assert observed[0]["state"] == "RECONNECTING"
    assert observed[0]["reconnects"] == 1
    assert observed[0]["last_error"] == "offline"
    assert stream.snapshot()["state"] == "IDLE"


def test_dashboard_falls_back_to_rest_if_stream_price_missing():
    service = DashboardService.__new__(DashboardService)
    service.settings = Settings()
    service.market_prices = SimpleNamespace(prices=lambda symbols: {})
    calls = []
    service.client = SimpleNamespace(get_price=lambda symbol: calls.append(symbol) or 85001.0)

    assert service.current_price("BTCUSDT") == 85001.0
    assert calls == ["BTCUSDT"]


def test_get_prices_requests_only_the_wanted_symbols(monkeypatch):
    client = BinanceSpotClient(Settings())
    calls = []

    def request(method, endpoint, *, params=None, signed=False):
        calls.append(params)
        if params and "symbol" in params:
            return {"symbol": params["symbol"], "price": "85000"}
        return [{"symbol": symbol, "price": "1"} for symbol in json.loads(params["symbols"])]

    monkeypatch.setattr(client, "_request", request)

    assert client.get_prices(["BTCUSDT"]) == {"BTCUSDT": 85000.0}
    assert client.get_prices(["BTCUSDT", "ETHUSDT"]) == {
        "BTCUSDT": 1.0, "ETHUSDT": 1.0,
    }
    assert calls == [
        {"symbol": "BTCUSDT"},
        {"symbols": '["BTCUSDT","ETHUSDT"]'},
    ]
