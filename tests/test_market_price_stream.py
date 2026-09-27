"""Le flux public Demo n'est utilise qu'avec un prix valide et recent."""

import json
from types import SimpleNamespace

from binance_spot_manager.binance_client import BinanceSpotClient
from binance_spot_manager.config import Environment, Settings
from binance_spot_manager.dashboard_service import DashboardService
from binance_spot_manager.market_price_stream import DemoMarketPriceStream
import binance_spot_manager.market_price_stream as market_stream


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
        Settings(demo_base_url="https://demo-api.binance.com"),
        connect=lambda *a, **k: None,
    ).enabled


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
