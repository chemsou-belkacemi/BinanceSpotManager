"""Le client respecte Retry-After sans renvoyer de requete pendant la pause."""

import pytest
import requests

from binance_spot_manager.binance_client import BinanceError, BinanceSpotClient
from binance_spot_manager.config import Settings


def test_429_starts_cooldown_without_second_http_request(monkeypatch):
    client = BinanceSpotClient(Settings())
    response = requests.Response()
    response.status_code = 429
    response._content = b'{"code":-1003,"msg":"Too many requests"}'
    response.headers["Retry-After"] = "30"
    attempts = []

    def request(*args, **kwargs):
        attempts.append((args, kwargs))
        return response

    monkeypatch.setattr(client._session, "request", request)

    with pytest.raises(BinanceError) as first:
        client.get_price("BTCUSDT")
    with pytest.raises(BinanceError) as second:
        client.get_price("BTCUSDT")

    assert first.value.status == 429
    assert first.value.retry_after == 30
    assert second.value.retry_after > 0
    assert len(attempts) == 1


@pytest.mark.parametrize("error, ambiguous", [
    (BinanceError("Timeout", code=-1007, status=504), True),
    (BinanceError("Server error", status=500), True),
    (BinanceError("Echec reseau simule"), True),
    (BinanceError("Too many requests", status=429), False),
    (BinanceError("Insufficient balance", code=-2010, status=400), False),
])
def test_write_error_classification(error, ambiguous):
    assert error.is_ambiguous_write is ambiguous


@pytest.mark.parametrize("message, expected", [
    ("Stop price would trigger immediately.", True),
    ("Order would immediately trigger.", True),
    ("Account has insufficient balance for requested action.", False),
])
def test_stop_would_trigger_is_recognised(message, expected):
    from binance_spot_manager.binance_client import BinanceError

    assert BinanceError(message, code=-2010, status=400).is_stop_would_trigger is expected
