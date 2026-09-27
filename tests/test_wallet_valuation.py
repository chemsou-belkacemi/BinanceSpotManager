"""Le portefeuille inclut les soldes bloqués et ne masque pas les taux absents."""

import pytest

from binance_spot_manager.wallet_valuation import conversion_rate, value_wallet

pytestmark = pytest.mark.unit


def test_direct_inverse_and_bridge_conversions():
    prices = {"BTCUSDT": 85000, "EURUSDT": 1.14, "ETHBTC": 0.03}
    assert conversion_rate("BTC", "USDT", prices) == 85000
    assert conversion_rate("USDT", "EUR", prices) == pytest.approx(1 / 1.14)
    assert conversion_rate("ETH", "USDT", prices) == pytest.approx(2550)


def test_wallet_counts_free_and_locked_without_double_counting():
    valuation = value_wallet(
        {
            "BTC": {"free": 0.001, "locked": 0.002},
            "USDC": {"free": 10, "locked": 0},
            "XYZ": {"free": 3, "locked": 0},
        },
        {"BTCUSDT": 85000, "USDCUSDT": 1, "EURUSDT": 1.25},
    )
    btc = next(row for row in valuation.assets if row.asset == "BTC")
    assert btc.total == pytest.approx(0.003)
    assert btc.value_usdt == pytest.approx(255)
    assert valuation.total_usdt == pytest.approx(265)
    assert valuation.total_eur == pytest.approx(212)
    assert valuation.unpriced_usdt == ("XYZ",)
    assert not valuation.complete
