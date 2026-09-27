"""Tests d'integration Binance Demo — LECTURE SEULE par defaut.

Ces tests ne sont PAS destructifs au sens ou ils ne creent rien sans
`--execute`, et `--execute` passe par /order/test qui n'execute pas d'ordre.

Usage :
    pytest tests/test_demo_integration.py -q                  # lecture seule
    python scripts/demo_tests.py                               # verdict lisible
    python scripts/demo_tests.py --execute                     # + test order (test endpoint)
"""

from __future__ import annotations

import pytest

from binance_spot_manager.binance_client import BinanceError, BinanceSpotClient
from binance_spot_manager.config import ALLOWED_DEMO_BASE_URLS, get_settings
from binance_spot_manager.symbol_rules import SymbolRulesCache, SymbolRulesError

pytestmark = pytest.mark.integration

SYMBOL = "BTCUSDT"


@pytest.fixture(scope="module")
def client() -> BinanceSpotClient:
    settings = get_settings()
    return BinanceSpotClient(settings)


@pytest.fixture(scope="module")
def settings():
    return get_settings()


def test_1_base_url_is_whitelisted(settings):
    assert settings.base_url in ALLOWED_DEMO_BASE_URLS, (
        f"URL hors perimetre Demo : {settings.base_url}"
    )


def test_2_ping(client):
    client.ping()


def test_3_server_time_sync(client):
    offset = client.sync_time()
    assert isinstance(offset, int)


def test_4_symbol_exists(client):
    info = client.get_symbol_info(SYMBOL)
    assert info is not None
    assert info["symbol"] == SYMBOL


def test_5_invalid_symbol_returns_none(client):
    assert client.get_symbol_info("BTCCUSDT") is None


def test_6_current_price(client):
    price = client.get_price(SYMBOL)
    assert price > 0


def test_7_symbol_rules_are_complete(client):
    rules = SymbolRulesCache(client, ttl=0).get(SYMBOL, refresh=True)
    assert rules.tick_size > 0
    assert rules.step_size > 0
    assert rules.min_qty > 0
    assert rules.quote_asset == "USDT"


def test_8_account_requires_credentials(client, settings):
    if not settings.has_credentials:
        pytest.skip("Cles API absentes : renseigner .env")
    account = client.get_account()
    assert "balances" in account


def test_9_balances_readable(client, settings):
    if not settings.has_credentials:
        pytest.skip("Cles API absentes : renseigner .env")
    balances = client.get_balances()
    assert isinstance(balances, dict)


def test_10_open_orders_readable(client, settings):
    if not settings.has_credentials:
        pytest.skip("Cles API absentes : renseigner .env")
    orders = client.get_open_orders(SYMBOL)
    assert isinstance(orders, list)


def test_11_write_refused_outside_demo():
    """Une URL non whitelistee doit refuser toute ecriture."""
    from binance_spot_manager.config import Environment, RunMode, SecurityError, Settings

    forged = Settings(
        environment=Environment.DEMO,
        run_mode=RunMode.DEMO_AUTO,
        demo_base_url="https://api.binance.com",
        demo_api_key="x",
        demo_api_secret="y",
    )
    with pytest.raises(SecurityError):
        forged.assert_write_allowed("create_order")


def test_12_write_refused_in_live_environment():
    from binance_spot_manager.config import Environment, RunMode, SecurityError, Settings

    forged = Settings(
        environment=Environment.LIVE,
        run_mode=RunMode.DEMO_AUTO,
        live_base_url="https://api.binance.com",
        live_api_key="x",
        live_api_secret="y",
    )
    with pytest.raises(SecurityError):
        forged.assert_write_allowed("create_order")


def test_13_run_mode_live_is_downgraded():
    """BSM_RUN_MODE=LIVE ne doit jamais produire un mode Live."""
    from binance_spot_manager.config import RunMode

    assert RunMode.LIVE.value == "LIVE"
    # Le downgrade est applique dans load_settings() : verifie sur le mode courant.
    assert get_settings().run_mode is not RunMode.LIVE


def test_14_test_order_endpoint_optional(client, settings):
    """Test order : valide les filtres sans executer (necessite --execute env)."""
    import os

    if os.getenv("BSM_RUN_TEST_ORDER", "").lower() not in {"1", "true", "yes"}:
        pytest.skip("Definir BSM_RUN_TEST_ORDER=1 pour executer ce test")
    if not settings.has_credentials:
        pytest.skip("Cles API absentes")

    rules = SymbolRulesCache(client, ttl=0).get(SYMBOL, refresh=True)
    price = client.get_price(SYMBOL)
    qty = rules.min_quote_for_order(price) / rules.tick_size  # quantite valide
    qty = rules.round_qty(max(qty, rules.min_qty))

    client.create_order(
        symbol=SYMBOL,
        side="BUY",
        order_type="LIMIT",
        quantity=rules.qty_str(qty),
        price=rules.price_str(price * 0.5),
        time_in_force="GTC",
        client_order_id="BSM-D-TESTORDER",
        test=True,
    )
