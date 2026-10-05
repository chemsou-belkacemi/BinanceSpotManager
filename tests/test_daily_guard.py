"""Perte maximale du jour : au-delà de −X % du capital (réalisé du jour + latent), plus aucune nouvelle entrée
jusqu'à 00:00 UTC ; les positions restent suivies ; un redémarrage ne lève rien ; désactiver la règle la lève."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from binance_spot_manager.daily_guard import DailyLossGuard, day_result, settings_from
from binance_spot_manager.notification_engine import NotificationEngine
from binance_spot_manager.signal_inbox import SignalInbox
from test_automation import events, make_position, rules, settings  # noqa: F401 - fixtures partagees
from test_journal_telegram_corrections import losing_position
from test_signal_auto_execution import SIMPLE, enabled_preferences, executor, telegram_id
from test_suivi_canaux_protection import never_bought, winning_position

UTC = timezone.utc
NOON = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def guard(tmp_path, preferences=None, clock=None):
    preferences = {} if preferences is None else preferences
    clock = clock or [NOON.timestamp()]
    return DailyLossGuard(lambda: preferences, path=tmp_path / "daily_guard.json", clock=lambda: clock[0]), clock


def test_settings_have_safe_defaults_and_bounds():
    assert settings_from({}) == (True, 3.0)
    assert settings_from({"daily_loss_percent": 0.01}) == (True, 0.5)
    assert settings_from({"daily_loss_percent": "x", "daily_loss_enabled": False}) == (False, 3.0)
    assert settings_from({"daily_loss_percent": float("nan")}) == (True, 3.0)


def test_day_result_counts_today_closed_and_open_latent(rules):  # noqa: F811
    loss, win, old, canceled = (losing_position(rules), winning_position(rules), winning_position(rules),
                                never_bought(rules))
    loss.closed_at = NOON - timedelta(hours=2)
    win.closed_at = NOON - timedelta(hours=1)
    old.closed_at = NOON - timedelta(days=1)                        # hier : ne compte pas
    canceled.closed_at = NOON - timedelta(hours=1)
    still_open = make_position(rules)
    still_open.pnl.unrealized = -12.5
    total = day_result([loss, win, old, canceled, still_open], NOON, lambda p: {})
    assert total == pytest.approx(loss.pnl.realized + win.pnl.realized - 12.5)


def test_the_guard_blocks_new_entries_until_midnight(tmp_path):
    rule, clock = guard(tmp_path)
    assert rule.check(-60.0, 3000.0) == [] and rule.refusal() == ""             # −2 % : sous le seuil de 3 %
    events = rule.check(-95.0, 3000.0)
    assert [kind for kind, _ in events] == ["STARTED"] and "−3 %" in events[0][1]
    assert "aucune nouvelle entrée jusqu'à 00:00 UTC" in rule.refusal()
    assert rule.check(+50.0, 3000.0) == [] and rule.refusal()                    # remonte : le blocage tient
    restarted, _ = guard(tmp_path, clock=clock)
    assert restarted.refusal()                                                   # un redémarrage ne lève rien
    clock[0] = (NOON + timedelta(hours=12, minutes=1)).timestamp()               # 00:01 UTC le lendemain
    assert rule.refusal() == ""
    assert [kind for kind, _ in rule.check(0.0, 3000.0)] == ["ENDED"]
    assert rule.check(0.0, 3000.0) == []


def test_disabling_the_rule_lifts_the_block_at_once(tmp_path):
    preferences = {}
    rule, _ = guard(tmp_path, preferences)
    rule.check(-200.0, 3000.0)
    preferences["daily_loss_enabled"] = False
    assert rule.refusal() == ""
    assert [kind for kind, _ in rule.check(-200.0, 3000.0)] == ["ENDED"]


def test_no_capital_no_block(tmp_path):
    rule, _ = guard(tmp_path)
    assert rule.check(-500.0, 0.0) == [] and rule.refusal() == ""


def test_the_executor_keeps_signals_in_the_inbox_while_blocked(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences())
    worker.daily_guard_reason = "Perte du jour -95.00 USDT"
    assert worker.process_pending() == [] and worker.snapshot()["state"] == "PERTE_DU_JOUR"
    assert inbox.recent("demo")[0]["auto_state"] == "" and commands.list_recent("demo") == []
    worker.daily_guard_reason = ""
    assert worker.process_pending() == ["QUEUED"]


def test_the_worker_measures_blocks_and_announces(tmp_path, rules, events, monkeypatch):  # noqa: F811
    from binance_spot_manager.position_store import PositionStore
    from scripts import bot_worker

    store = PositionStore(tmp_path / "positions")
    position = make_position(rules)
    position.pnl.unrealized = -150.0
    store.save(position)
    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    worker.settings = SimpleNamespace(dry_run=False, has_credentials=True, quote_asset="USDT")
    worker.positions, worker.events = store, events
    worker.client = SimpleNamespace(get_balances=lambda: {"USDT": {"free": 2400.0, "locked": 96.0}},
                                    get_price=lambda symbol: None)
    worker.daily_guard, _ = guard(tmp_path)
    worker.auto_signal_executor = SimpleNamespace(daily_guard_reason="")
    sent = []
    worker.notifications = SimpleNamespace(daily_loss=lambda kind, detail: (kind, detail), notify=sent.append)
    worker.licence_gate = SimpleNamespace(refusal=lambda: "")
    worker._check_daily_loss()                                                   # capital 2400+96+504 = 3000
    assert sent and sent[0][0] == "STARTED" and "−3 %" in sent[0][1]
    assert worker.auto_signal_executor.daily_guard_reason and worker._entry_refusal()
    assert events.tail(limit=1)[0]["event"] == "DAILY_LOSS_STARTED"
    worker._check_daily_loss()                                                   # au plus une mesure par minute
    assert len(sent) == 1
    worker.licence_gate = SimpleNamespace(refusal=lambda: "Licence requise")
    assert worker._entry_refusal() == "Licence requise"                           # la licence passe d'abord


def test_daily_loss_messages(settings):  # noqa: F811
    engine = NotificationEngine(settings)
    started = engine.daily_loss("STARTED", "Perte du jour -95.00 USDT")
    assert started.event == "DAILY_LOSS" and started.level == "CRITICAL" and "bloquees" in started.title
    assert engine.daily_loss("ENDED", "fin").level == "INFO"


def test_settings_save_the_daily_loss_rule(monkeypatch, tmp_path):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from binance_spot_manager.position_store import JsonFileStore

    store = JsonFileStore(tmp_path / "settings.json")
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price", lambda self, symbol: 500)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception
    app.number_input(key="daily_loss_percent_input").set_value(2.5)
    next(b for b in app.button if b.label == "Enregistrer la perte maximale").click().run()
    assert not app.exception
    assert store.load()["daily_loss_percent"] == 2.5 and store.load()["daily_loss_enabled"] is True



def test_the_capital_counts_what_is_still_held_not_the_gross_purchase(tmp_path, rules, events, monkeypatch):  # noqa: F811
    """Relecture du 2026-10-05 : après un TP, le produit de la vente est dans l'USDT libre ; le capital ne compte que
    la crypto encore détenue (sinon le seuil réel serait plus lâche que 3 %)."""
    from binance_spot_manager.models import TPStatus
    from binance_spot_manager.position_engine import recompute_position
    from binance_spot_manager.position_store import PositionStore
    from scripts import bot_worker

    position = make_position(rules)
    tp1 = position.sorted_tps[0]
    tp1.status, tp1.executed_qty, tp1.average_fill_price = TPStatus.EXECUTED, 0.0036, 86520.0
    tp1.quote_received = 0.0036 * 86520.0
    position.metrics.current_price = 85000.0
    recompute_position(position)
    position.pnl.unrealized = -110.0
    store = PositionStore(tmp_path / "positions")
    store.save(position)
    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    worker.settings = SimpleNamespace(dry_run=False, has_credentials=True, quote_asset="USDT")
    worker.positions, worker.events = store, events
    worker.client = SimpleNamespace(get_balances=lambda: {"USDT": {"free": 3300.0, "locked": 0.0}},
                                    get_price=lambda symbol: None)
    worker.daily_guard, _ = guard(tmp_path)
    worker.auto_signal_executor = SimpleNamespace(daily_guard_reason="")
    worker.notifications = SimpleNamespace(daily_loss=lambda kind, detail: (kind, detail), notify=lambda n: None)
    worker._check_daily_loss()
    held = position.metrics.net_qty * 85000.0                                       # ≈ 204 USDT encore détenus
    state = worker.daily_guard.state()
    assert state["capital"] == pytest.approx(3300.0 + held, rel=1e-6)               # pas + 504 de coût d'achat brut
