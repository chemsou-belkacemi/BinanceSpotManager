"""Lecture des paires par le parseur de signaux (binance_spot_manager/signal_parser.py)."""
from binance_spot_manager.signal_parser import parse_signal


def test_one_letter_coins_are_read_and_a_lone_digit_is_not():
    """2026-10-06 : WHALE HUNTING publie G/USDT (Gravity) ; Binance a aussi T, W, S… Avant, « aucune paire »."""
    for text, symbol in (("PAIR: G/USDT\nENTRY 1: 0.00456\nTP 1: 0.00463\nSL: 0.00438 (15m)", "GUSDT"),
                         ("#T/USDT\nEntry: 0.02\nTP1: 0.022\nSL: 0.018", "TUSDT"),
                         ("WUSDT\nENTRY 1: 0.1\nTP 1: 0.12\nSL: 0.09", "WUSDT")):
        result = parse_signal(text)
        assert result.symbol == symbol and not result.errors, text
    assert parse_signal("PAIR: 1/USDT\nENTRY 1: 2\nTP 1: 3\nSL: 1").errors


def test_with_or_a_dash_never_makes_a_one_letter_coin():
    """« w/ USDT » (with) ou « W-USDT » ne sont jamais une crypto à une lettre (relecture du 2026-10-06)."""
    assert parse_signal("Buy XRP w/ USDT\nENTRY 1: 0.5\nTP 1: 0.55\nSL: 0.45").errors
    assert parse_signal("#W-USDT\nENTRY 1: 0.1\nTP 1: 0.12\nSL: 0.09").errors
    sol = parse_signal("#SOL/USDT\nENTRY 1: 150\nTP 1: 160\nSL: 140\ncorrelation w/ BTC")
    assert sol.symbol == "SOLUSDT" and not sol.errors
