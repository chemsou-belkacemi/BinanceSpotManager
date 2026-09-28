"""Same-pair strategies retain independent identity, diagnostics and UI selection."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import streamlit as st
from streamlit.testing.v1 import AppTest

from binance_spot_manager.execution_engine import build_client_order_id
from binance_spot_manager.models import Entry, OcoExit, Position, PositionStatus, TakeProfit
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.reconciliation_engine import audit_open_orders
import ui_common


def test_same_pair_position_ids_with_same_legacy_prefix_do_not_share_order_ids():
    first, second = "pos_abcdef123aaa", "pos_abcdef123bbb"
    for suffix in ("E1", "E2", "TP1", "TP20R10", "SL", "SL10", "OCOTP1"):
        a = build_client_order_id(symbol="BTCUSDT", position_id=first, suffix=suffix, identity_version=2)
        b = build_client_order_id(symbol="BTCUSDT", position_id=second, suffix=suffix, identity_version=2)
        assert a != b
        assert len(a) <= 36
        assert a == build_client_order_id(symbol="BTCUSDT", position_id=first, suffix=suffix, identity_version=2)
    # Old persisted positions keep their historic recovery IDs after an upgrade.
    legacy = Position(position_id=first, symbol="BTCUSDT")
    assert legacy.order_identity_version == 1
    assert build_client_order_id(symbol="BTCUSDT", position_id=first, suffix="SL", identity_version=1) == "BSM-D-BTCUSDT-abcdef12-SL"


def test_concurrent_same_symbol_creations_are_not_merged(tmp_path):
    positions = [Position(symbol="BTCUSDT", status=PositionStatus.ACTIVE) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda p: PositionStore(tmp_path).save(p), positions))
    stored = PositionStore(tmp_path).list_active_by_symbol("BTCUSDT")
    assert {p.position_id for p in stored} == {p.position_id for p in positions}


def test_open_order_audit_recognizes_all_strategies_and_oco_siblings():
    first = Position(symbol="BTCUSDT")
    first.entries = [Entry(order_id=1, client_order_id="first-entry")]
    second = Position(symbol="BTCUSDT")
    second.take_profits = [TakeProfit(order_id=2, client_order_id="second-tp")]
    second.oco_exit = OcoExit(order_list_id=1, list_client_order_id="oco", tp_order_id=3, sl_order_id=4, quantity=.001)
    foreign = Position(symbol="ETHUSDT", entries=[Entry(order_id=5)])
    orders = [{"symbol": "BTCUSDT", "orderId": n} for n in range(1, 6)]
    findings = audit_open_orders(first, orders, tracked_positions=[first, second, foreign])
    assert [f.order_id for f in findings] == [5]


def test_position_selector_retains_two_same_pair_positions_created_in_same_minute(monkeypatch, tmp_path):
    first, second = Position(symbol="BTCUSDT"), Position(symbol="BTCUSDT")
    second.created_at = first.created_at
    store = PositionStore(tmp_path)
    store.save(first)
    store.save(second)
    service = SimpleNamespace(positions=store, rules_cache=SimpleNamespace(get=lambda symbol: st.stop()))
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)
    page = Path(__file__).resolve().parents[1] / "pages" / "3_Positions.py"
    app = AppTest.from_file(str(page)).run()
    assert not app.exception
    assert len(app.selectbox[0].options) == 2
    assert any(first.position_id in label for label in app.selectbox[0].options)
    assert any(second.position_id in label for label in app.selectbox[0].options)


def test_position_status_filters_and_empty_results(monkeypatch, tmp_path):
    store = PositionStore(tmp_path)
    statuses = [PositionStatus.DRAFT, PositionStatus.PENDING_ENTRIES, PositionStatus.ACTIVE,
                PositionStatus.CLOSING, PositionStatus.CLOSED, PositionStatus.CANCELED, PositionStatus.ERROR]
    positions = [Position(symbol="BTCUSDT", status=status) for status in statuses]
    for position in positions:
        store.save(position)
    service = SimpleNamespace(positions=store, rules_cache=SimpleNamespace(get=lambda symbol: st.stop()))
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    monkeypatch.setattr(ui_common, "sidebar_status", lambda settings: None)
    page = Path(__file__).resolve().parents[1] / "pages" / "3_Positions.py"
    app = AppTest.from_file(str(page)).run()
    assert not app.exception
    assert len(app.selectbox[0].options) == 7
    for label, expected in (("Pending", positions[:2]), ("Active", positions[2:4]), ("Closed", positions[4:6])):
        app.segmented_control(key="position_status_filter").set_value(label).run()
        assert not app.exception
        assert len(app.selectbox[0].options) == 2
        assert all(any(p.position_id in option for option in app.selectbox[0].options) for p in expected)
    # Empty filters remain usable; no Binance calls or position changes.
    empty = PositionStore(tmp_path / "empty_filter")
    empty.save(positions[2].model_copy(update={"revision": 0}))
    service.positions = empty
    app.run()
    assert not app.exception
    assert not app.selectbox
    assert any("Aucune position pour ce statut" in info.value for info in app.info)
    app.segmented_control(key="position_status_filter").set_value("Toutes").run()
    assert not app.exception
    assert len(app.selectbox[0].options) == 1
