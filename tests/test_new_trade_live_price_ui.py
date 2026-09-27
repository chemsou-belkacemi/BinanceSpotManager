"""New Trade affiche le meme prix actualise dans le marche et la simulation."""

from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from binance_spot_manager.risk_engine import PortfolioSnapshot, RiskLimits
from binance_spot_manager.symbol_rules import parse_symbol_rules
import ui_common


NEW_TRADE = Path(__file__).resolve().parents[1] / "pages" / "2_New_Trade.py"


def test_new_trade_market_and_simulation_share_live_price(monkeypatch):
    rules = parse_symbol_rules({
        "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
        "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
        ],
    })
    service = ui_common.get_service()
    monkeypatch.setattr(ui_common, "load_rules", lambda symbol: (rules, ""))
    price = [84545.18]
    monkeypatch.setattr(ui_common, "live_price", lambda symbol: price[0])
    monkeypatch.setattr(
        service, "portfolio",
        lambda: SimpleNamespace(base_balances={}, quote_free=1000.0),
    )
    monkeypatch.setattr(service, "find_by_symbol", lambda symbol: None)
    monkeypatch.setattr(
        service, "risk_snapshot",
        lambda quote_free: PortfolioSnapshot(quote_balance=quote_free),
    )
    monkeypatch.setattr(service, "risk_limits", lambda: RiskLimits())

    app = AppTest.from_file(str(NEW_TRADE), default_timeout=20).run()
    assert not app.exception
    assert any("84 545.18" in info.value for info in app.info)
    assert any("84 545.18" in markdown.value for markdown in app.markdown)

    price[0] = 84600.00
    app.run()
    assert not app.exception
    assert any("84 600.00" in info.value for info in app.info)
    assert any("84 600.00" in markdown.value for markdown in app.markdown)
