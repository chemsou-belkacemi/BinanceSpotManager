"""Lacune L3 (audit du 2026-10-01) : deux workers sur la meme cle Demo, meme signal, un seul achat.

Le position_id d'une position issue d'un signal derive du scope du compte et d'un identifiant stable
du signal : les deux installations calculent les memes clientOrderId et Binance n'accepte qu'un ordre.
Aucun reseau : le compte Binance partage est simule.
"""

import re
import time
from types import SimpleNamespace

import pytest

from binance_spot_manager.binance_client import BinanceError
from binance_spot_manager.command_processor import CommandProcessor
from binance_spot_manager.command_store import CommandStore, account_scope
from binance_spot_manager.config import RunMode, Settings
from binance_spot_manager.event_store import EventStore
from binance_spot_manager.execution_engine import ExecutionEngine
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.risk_engine import RiskLimits
from binance_spot_manager.signal_auto_execution import AutomaticSignalExecutor
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_parser import content_hash, parse_signal
from binance_spot_manager.signal_plan import prepare_signal, signal_identity, signal_position_id
from binance_spot_manager.symbol_rules import SymbolRulesCache, parse_symbol_rules

SIGNAL = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"
SYMBOL_INFO = {
    "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
    "filters": [
        {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        {"filterType": "NOTIONAL", "minNotional": "5"},
    ],
}
PREFERENCES = {
    "signal_telegram_enabled": True,
    "signal_telegram_auto_enabled": True,
    "signal_auto_execute_enabled": True,
    "signal_auto_execute_enabled_since": 1.0,
    "signal_auto_max_age_minutes": 5,
    "signal_sizing_mode": "FIXED",
    "signal_fixed_budget": 90,
    "signal_csi_gate_enabled": False,
    # Routage (feat/signal-routing) : groupe déclaré de confiance et risque sous 0,5 %, seul profil AUTO.
    "signal_telegram_chats": "-100",
    "signal_auto_trusted_chats": [-100],
}


class SharedBinance:
    """Un seul compte Binance Demo vu par deux installations BSM.

    Comme Binance, refuse un clientOrderId deja utilise. `blind` simule une course : chaque
    installation a verifie l'absence de l'ordre avant que l'autre ne l'envoie.
    """

    def __init__(self):
        self.orders = {}
        self.accepted = []
        self.refused = []
        self.blind = False

    def get_symbol_info(self, symbol):
        return SYMBOL_INFO if symbol.upper() == "BTCUSDT" else None

    def get_price(self, symbol):
        return 84500.0

    def get_prices(self, symbols=None):
        return {"BTCUSDT": 84500.0}

    def get_ticker_24h(self, symbol):
        return {"quoteVolume": "1000000000", "bidPrice": "84499", "askPrice": "84500"}

    def get_balances(self):
        return {"USDT": {"free": 10000.0, "locked": 0.0}}

    def get_free_balance(self, asset):
        return 10000.0

    def get_open_orders(self, symbol=None):
        return [] if self.blind else [o for o in self.orders.values() if o["status"] == "NEW"]

    def find_order(self, symbol, *, order_id=None, client_order_id=None):
        return None if self.blind else self.orders.get(client_order_id)

    def get_order(self, symbol, *, order_id=None, client_order_id=None):
        found = self.find_order(symbol, client_order_id=client_order_id)
        if found is None:
            raise BinanceError("Order does not exist.", code=-2013, status=400)
        return found

    def create_order(self, *, symbol, side, order_type, quantity=None, price=None, stop_price=None,
                     time_in_force=None, client_order_id=None, quote_order_qty=None, test=False):
        if client_order_id in self.orders:
            self.refused.append(client_order_id)
            raise BinanceError("Duplicate order sent.", code=-2010, status=400)
        order = {
            "symbol": symbol, "orderId": 1000 + len(self.orders), "clientOrderId": client_order_id,
            "side": side, "type": order_type, "status": "NEW", "price": price, "origQty": quantity,
            "executedQty": "0", "cummulativeQuoteQty": "0", "fills": [],
        }
        self.orders[client_order_id] = order
        self.accepted.append(client_order_id)
        return order


def bsm_installation(root, binance, settings):
    """Une installation BSM complete (stockages separes) branchee sur le compte partage."""
    events = EventStore(root / "events.jsonl")
    inbox, commands = SignalInbox(root / "signals.db"), CommandStore(root / "commands.db")
    positions = PositionStore(root / "positions")
    rules = SymbolRulesCache(binance)
    execution = ExecutionEngine(binance, rules, settings=settings, events=events)
    scope = account_scope(settings)
    executor = AutomaticSignalExecutor(
        scope, inbox, commands, binance, rules, RiskLimits, lambda: PREFERENCES, events,
        run_mode="DEMO_AUTO", positions=positions,
    )
    processor = CommandProcessor(
        commands, positions, execution, RiskLimits,
        settings_supplier=lambda: {"bnb_fee_monitor_enabled": False},
    )
    return SimpleNamespace(scope=scope, inbox=inbox, executor=executor, processor=processor,
                           positions=positions)


@pytest.mark.parametrize("race", [False, True], ids=["l-autre-ordre-visible", "course"])
def test_same_signal_on_two_installations_with_the_same_demo_key_buys_once(tmp_path, race):
    settings = Settings(run_mode=RunMode.DEMO_AUTO, demo_api_key="same-demo-key", demo_api_secret="secret")
    binance = SharedBinance()
    binance.blind = race
    sides = [bsm_installation(tmp_path / name, binance, settings) for name in ("pc", "vps")]
    for side in sides:
        side.inbox.receive(side.scope, SIGNAL, source="telegram", external_id=f"{'ab' * 32}:-100:7",
                           source_timestamp=time.time() - 5)
        assert side.executor.process_pending() == ["QUEUED"]
    states = [side.processor.run_one() for side in sides]

    assert len(binance.accepted) == 1  # exactement un create_order accepte
    stored = [side.positions.list_all() for side in sides]
    assert [len(positions) for positions in stored] == [1, 1]
    assert stored[0][0].position_id == stored[1][0].position_id
    assert states[0] == "SUCCEEDED"
    if race:
        assert binance.refused == binance.accepted  # le second envoi est refuse comme doublon
        assert states[1] == "UNCERTAIN"  # jamais rejoue automatiquement : controle requis
    else:
        assert binance.refused == []  # l'ordre deja envoye est retrouve et adopte
        assert states[1] == "SUCCEEDED"
        assert stored[1][0].entries[0].order_id == binance.orders[binance.accepted[0]]["orderId"]


def test_signal_position_id_depends_only_on_account_scope_and_signal():
    scope = account_scope(Settings(run_mode=RunMode.DEMO_AUTO, demo_api_key="k", demo_api_secret="s"))
    other_key = account_scope(Settings(run_mode=RunMode.DEMO_AUTO, demo_api_key="k2", demo_api_secret="s"))
    dry_run = account_scope(Settings(run_mode=RunMode.DRY_RUN, demo_api_key="k", demo_api_secret="s"))
    position_id = signal_position_id(scope, "text:abc")

    assert position_id == signal_position_id(scope, "text:abc")
    assert signal_position_id(scope, "text:abd") != position_id
    assert len({signal_position_id(s, "text:abc") for s in (scope, other_key, dry_run)}) == 3
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,100}", position_id)  # nom de fichier accepte par PositionStore
    with pytest.raises(ValueError):
        signal_position_id("", "text:abc")


def test_signal_identity_uses_csi_idempotency_key_else_normalized_text_hash(tmp_path):
    first = SignalInbox(tmp_path / "pc.db").receive("demo", SIGNAL)
    second = SignalInbox(tmp_path / "vps.db").receive("demo", "  " + SIGNAL.lower().replace("\n", " \n"))
    assert first["id"] != second["id"]  # identifiants de ligne aleatoires, propres a chaque installation
    assert signal_identity(first) == signal_identity(second) == "text:" + content_hash(SIGNAL)
    csi_v3 = dict(first, parsed=dict(first["parsed"], idempotency_key="CSI-SETUP-42"))
    assert signal_identity(csi_v3) == "csi:CSI-SETUP-42"


def test_preparation_without_signal_key_keeps_a_random_position_id():
    rules = parse_symbol_rules(SYMBOL_INFO)
    kwargs = dict(budget=200, available_quote=1000, reserve_percent=20, current_price=84500,
                  signal_id="s", validity_confirmed=True, touch_stop=True)

    def position_id(**extra):
        return prepare_signal(parse_signal(SIGNAL), rules, **kwargs, **extra)[1]["position"]["position_id"]

    assert position_id() != position_id()
    keyed = {position_id(account_scope="demo", signal_key="text:x") for _ in range(2)}
    assert keyed == {signal_position_id("demo", "text:x")}
