"""Filtre de liquidité des signaux automatiques : une paire peu liquide (volume 24 h en USDT trop bas, écart
achat/vente trop large) passe « À confirmer » ; des statistiques indisponibles aussi ; rien n'est refusé d'office."""
from __future__ import annotations

from pathlib import Path

from binance_spot_manager.signal_auto_execution import liquidity_reasons
from binance_spot_manager.signal_inbox import SignalInbox
from test_signal_auto_execution import LIQUID, SIMPLE, enabled_preferences, executor, telegram_id


def codes(reasons):
    return [reason.code for reason in reasons]


def test_thin_volume_and_wide_spread_are_review_reasons():
    reasons, metrics = liquidity_reasons({"quoteVolume": "210000", "bidPrice": "0.0610", "askPrice": "0.0615"}, {})
    assert codes(reasons) == ["R_LIQUIDITY", "R_SPREAD"]
    assert "210 000 USDT < 500 000 USDT" in reasons[0].message and metrics["volume_24h_usdt"] == 210000
    assert codes(liquidity_reasons(LIQUID, {})[0]) == []
    assert codes(liquidity_reasons({"quoteVolume": "210000", "bidPrice": "1", "askPrice": "1.001"},
                                   {"signal_min_volume_usdt": 100000})[0]) == []          # seuils réglables
    assert liquidity_reasons({"quoteVolume": "1"}, {"signal_liquidity_enabled": False}) == ([], {})
    assert codes(liquidity_reasons({"quoteVolume": "x"}, {})[0]) == ["D_LIQUIDITY"]
    assert codes(liquidity_reasons({"quoteVolume": "1e9", "bidPrice": "0", "askPrice": "1"}, {})[0]) == ["R_SPREAD"]


def run(tmp_path, ticker, **preferences):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences(**preferences))
    worker.client.get_ticker_24h = ticker
    return worker.process_pending(), inbox.get("demo", row["id"])


def test_an_illiquid_pair_waits_for_confirmation(tmp_path):
    outcome, row = run(tmp_path, lambda symbol: {"quoteVolume": "210000", "bidPrice": "84499", "askPrice": "84500"})
    assert outcome == ["REVIEW"] and '"R_LIQUIDITY"' in row["route"] and row["payload"] is None


def test_unavailable_statistics_wait_for_confirmation_unless_the_filter_is_off(tmp_path):
    def broken(symbol):
        raise RuntimeError("réseau")

    for folder in ("on", "off"):
        (tmp_path / folder).mkdir()
    outcome, row = run(tmp_path / "on", broken)
    assert outcome == ["REVIEW"] and '"D_LIQUIDITY"' in row["route"]
    outcome, row = run(tmp_path / "off", broken, signal_liquidity_enabled=False)
    assert outcome == ["QUEUED"]


def test_settings_save_the_liquidity_filter(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    from binance_spot_manager.position_store import JsonFileStore

    store = JsonFileStore(tmp_path / "settings.json")
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price", lambda self, symbol: 500)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    app.number_input(key="signal_min_volume_input").set_value(1_000_000)
    app.number_input(key="signal_max_spread_input").set_value(0.3)
    next(b for b in app.button if b.label == "Enregistrer le filtre de liquidité").click().run()
    assert not app.exception
    saved = store.load()
    assert saved["signal_min_volume_usdt"] == 1_000_000 and saved["signal_max_spread_percent"] == 0.3
    assert saved["signal_liquidity_enabled"] is True
