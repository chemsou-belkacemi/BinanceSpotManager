"""Generic label reader: unknown layouts are read, anything ambiguous stays refused."""
import pytest

from binance_spot_manager.signal_parser import parse_signal

LEGEND = """LEGEND TRADING INDICATOR
───────────────────
#QTUM/USDT
───────────────────
📍 Entry1: 1.01
📍 Entry2: 0.97
───────────────────
🎯 TARGETS
───────────────────
🎯 TP1: 1.05 (3.96%)
🎯 TP2: 1.082 (7.13%)
🎯 TP3: 1.13 (11.88%)
───────────────────
🛑 Stop: 0.93 (4h) (-6.06%)
───────────────────
☪️ الحكم الشرعي: مباح ✅
───────────────────
👤 IndicatorCEO: Abo yaseein
───────────────────
📅 Date: Wednesday - 2026-09-30"""


@pytest.mark.parametrize("text,symbol,entries,targets,stop,timeframe", [
    (LEGEND, "QTUMUSDT", [1.01, .97], [1.05, 1.082, 1.13], .93, "4h"),
    ("💎 OP/USDT\n📥 Entry:\n1) 1.20\n2) 1.15\n🎯 Targets:\n1) 1.30\n2) 1.40\n3) 1.55\n⛔ Stop Loss: 1.05",
     "OPUSDT", [1.2, 1.15], [1.3, 1.4, 1.55], 1.05, ""),
    ("Pair: INJUSDT | Entry: 20.1 | TP: 21, 22, 23 | SL: 19", "INJUSDT", [20.1], [21, 22, 23], 19, ""),
    ("BTC/USDT Buy: 60000 TP: 62000 SL: 58000", "BTCUSDT", [60000], [62000], 58000, ""),
    ("🚀 NEW SIGNAL 🚀\nBTC/USDT\nBuy zone: 60000 - 61000\nTargets: 62000 - 63000 - 65000\nStop loss: 58000",
     "BTCUSDT", [60000, 61000], [62000, 63000, 65000], 58000, ""),
    ("Coin: $ARB/USDT\nBuy: 0.5\nTake Profit 1: 0.55\nTake Profit 2: 0.6\nStop: 0.45",
     "ARBUSDT", [.5], [.55, .6], .45, ""),
    ("#SOLUSDT LONG\nEntry: 140 - 142\nTP1: 145\nTP2: 150\nSL: 135 (4H close)",
     "SOLUSDT", [140, 142], [145, 150], 135, "4h"),
    ("ETH/USDT spot\nEntries:\n- 3000\n- 2950\nTargets:\n- 3100\n- 3200\nStop: 2850 daily close",
     "ETHUSDT", [3000, 2950], [3100, 3200], 2850, "1d"),
    ("⚡️ AVAX/USDT ⚡️\nEntry ➡️ 25.5\nTP1 → 26.2 [+2.7%]\nTP2 → 27.0 [+5.9%]\nSL → 24.8 (-2.7%)\nExchange: Binance",
     "AVAXUSDT", [25.5], [26.2, 27], 24.8, ""),
])
def test_unknown_layouts_are_read_through_their_labels(text, symbol, entries, targets, stop, timeframe):
    result = parse_signal(text)
    assert result.errors == []
    assert result.direction == "BUY"
    assert (result.symbol, result.entries, result.targets) == (symbol, entries, targets)
    assert (result.stop, result.stop_timeframe) == (stop, timeframe)


@pytest.mark.parametrize("text,reason", [
    ("BTC/USDT\nEntry: 60000 or market\nTP: 62000\nSL: 58000", "condition ou alternative"),
    ("SOL/USDT\nBuy above 140\nTP1: 150\nSL: 130", "condition ou alternative"),
    ("SOL/USDT\nEntry: 140\nTP1: 150K\nSL: 130", "suffixe"),
    ("SOL/USDT\nEntry: 140,5\nTP1: 150\nSL: 130", "virgule"),
    ("SOL/USDT\nEntry: 140\nTargets:\n1) 150\n2) 160+\nSL: 130", "Objectif ambigu"),
    ("SOL/USDT\nEntry: 140\nTP1: 150 - 155\nSL: 130", "plusieurs prix pour un seul niveau"),
    ("SOL/USDT\nEntry: 140\nTP1: 150\nSL: 130\nStop: 128", "Un seul stop loss"),
    ("BTC/USDT and ETH/USDT\nEntry: 3000\nTP1: 3100\nSL: 2900", "Une seule paire"),
    ("PAIR: SOL\nEntry: 140\nTP1: 150\nSL: 130", "USDT/USDC"),
    ("SOL/USDT futures\nEntry: 140\nTP1: 150\nSL: 130", "Short"),
    ("SOL/USDT\nEntry: 140\nTP1: 130\nTP2: 120\nSL: 150", "Achat incohérent"),
    ("SOL/USDT\nEntry: 140\nTP1: 150\nSL: 130\nExchange: Bybit", "autre que Binance"),
    (LEGEND.replace("TP1: 1.05", "TP1: 1.2"), "strictement croissants"),
])
def test_ambiguous_values_are_refused_never_guessed(text, reason):
    assert any(reason in error for error in parse_signal(text).errors)


@pytest.mark.parametrize("mention", [
    "(au marché)", "(au marche)", "(AU MARCHÉ)", "(marché)", "au prix du marché", "(market)", "MARKET",
])
def test_entry_at_market_is_refused_never_read_as_a_limit(mention):
    """Audit du 2026-10-01 : « ENTRY: 2500 (au marché) » devenait une limite à 2500, mention ignorée."""
    result = parse_signal(f"PAIR: ETH/USDT\nENTRY: 2500 {mention}\nT1: 2700\nSL: 2400")
    assert result.entries == []
    assert any("condition ou alternative" in error for error in result.errors)


@pytest.mark.parametrize("line", ["ENTRY: au marché", "Entrée : AU MARCHE", "ENTRY: market"])
def test_market_entry_without_price_is_refused(line):
    result = parse_signal(f"PAIR: ETH/USDT\n{line}\nT1: 2700\nSL: 2400")
    assert any("non prise en charge" in error for error in result.errors)


@pytest.mark.parametrize("text", [
    "PAIR: ETH/USDT\nENTRY: 2500\nT1: 2700 (vente au marché)\nSL: 2400",
    "PAIR: ETH/USDT\nENTRY: 2500\nT1: 2700\nSL: 2400 (au marché)",
])
def test_market_mention_on_a_target_or_stop_is_refused_too(text):
    assert any("condition ou alternative" in error for error in parse_signal(text).errors)


def test_plain_limit_entry_still_reads_after_the_market_rule():
    result = parse_signal("PAIR: ETH/USDT\nENTRY: 2500\nT1: 2700\nSL: 2400\nPLATFORM: Binance (marché spot)")
    assert result.errors == []
    assert (result.entries, result.targets, result.stop) == ([2500], [2700], 2400)
