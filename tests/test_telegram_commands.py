"""Commandes Telegram du propriétaire : /pause, /reprise, /statut, seulement en conversation privée et depuis son
compte ; une commande n'est jamais enregistrée comme signal ; la pause bloque les nouvelles entrées, jamais le
suivi."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.telegram_commands import HELP, ManualPause, TelegramCommands, owner_id
from binance_spot_manager.telegram_signals import TelegramSignalPoller, import_telegram
from test_signal_auto_execution import SIMPLE, enabled_preferences, executor, telegram_id

OWNER = 123456789


def private(text, sender=OWNER, chat=OWNER, kind="private", message_id=1):
    return {"message_id": message_id, "date": 1000, "text": text, "chat": {"id": chat, "type": kind},
            "from": {"id": sender}}


def commands(tmp_path, preferences=None, default_chat=OWNER):
    pause = ManualPause(tmp_path / "pause.json")
    return TelegramCommands(lambda: preferences or {}, default_chat, pause, lambda: "statut ok"), pause


def test_only_the_owner_in_private_can_command(tmp_path):
    handler, pause = commands(tmp_path)
    assert handler.handle(private("/pause", sender=111)) is None                     # autre compte
    assert handler.handle(private("/pause", chat=-100123, kind="supergroup")) is None  # groupe
    assert handler.handle(private("/pause"), edited=True) is None                    # message édité
    assert handler.handle(private("pause")) is None and pause.refusal() == ""        # pas une commande
    assert "Pause activée" in handler.handle(private("/pause"))
    assert "aucune nouvelle entrée" in pause.refusal()
    assert handler.handle(private("/statut@MonBot")) == "statut ok"
    assert "Pause levée" in handler.handle(private("/reprise"))
    assert pause.refusal() == ""
    assert handler.handle(private("/inconnue")) == HELP


def test_the_owner_comes_from_settings_or_the_private_notification_chat():
    assert owner_id({}, OWNER) == OWNER
    assert owner_id({}, -100123) is None                                             # groupe : pas un compte
    assert owner_id({"telegram_owner_id": "42"}, OWNER) == 42
    assert owner_id({"telegram_commands_enabled": False}, OWNER) is None
    assert owner_id({"telegram_owner_id": "abc"}, "") is None


def test_the_pause_survives_a_restart(tmp_path):
    ManualPause(tmp_path / "pause.json").set(True, by="Telegram /pause")
    assert ManualPause(tmp_path / "pause.json").refusal()


class Session:
    def __init__(self, updates):
        self.updates, self.posted = updates, []

    def get(self, url, params=None, timeout=None):
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": self.updates})

    def post(self, url, data=None, timeout=None):
        self.posted.append(data)


def test_a_command_is_answered_and_never_stored_as_a_signal(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    handler, pause = commands(tmp_path)
    session = Session([
        {"update_id": 10, "message": private("/pause")},
        {"update_id": 11, "message": {"message_id": 5, "date": 1000, "text": SIMPLE,
                                      "chat": {"id": -100123, "type": "supergroup", "title": "Relais"}}},
    ])
    items = import_telegram("token", {-100123}, inbox, "demo", session=session, commands=handler)
    assert len(items) == 1 and items[0]["raw"] == SIMPLE                            # seul le signal est gardé
    assert session.posted and session.posted[0]["chat_id"] == OWNER and "Pause activée" in session.posted[0]["text"]
    assert pause.refusal() and all(row["raw"] != "/pause" for row in inbox.recent("demo"))
    assert inbox.offset(__import__("hashlib").sha256(b"token").hexdigest()) == 12


def test_the_reader_runs_for_commands_even_when_signal_reception_is_off(tmp_path):
    handler, pause = commands(tmp_path)
    session = Session([{"update_id": 1, "message": private("/pause")}])
    poller = TelegramSignalPoller("token", "demo", lambda: {}, inbox=SignalInbox(tmp_path / "s.db"),
                                  session=session, commands=handler)
    assert poller.poll_once() == [] and pause.refusal()
    (tmp_path / "off").mkdir()
    off, _ = commands(tmp_path / "off", {"telegram_commands_enabled": False})
    quiet = TelegramSignalPoller("token", "demo", lambda: {}, inbox=SignalInbox(tmp_path / "s2.db"),
                                 session=Session([]), commands=off)
    assert quiet.poll_once() == [] and quiet.snapshot()["state"] == "DISABLED"


def test_the_executor_and_the_worker_respect_the_pause(tmp_path, monkeypatch):
    from scripts import bot_worker

    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    worker, commands_store = executor(tmp_path, inbox, enabled_preferences())
    worker.manual_pause_reason = "Pause manuelle"
    assert worker.process_pending() == [] and worker.snapshot()["state"] == "PAUSE_MANUELLE"
    worker.manual_pause_reason = ""
    assert worker.process_pending() == ["QUEUED"]

    bot = bot_worker.Worker.__new__(bot_worker.Worker)
    bot.manual_pause = ManualPause(tmp_path / "pause.json")
    bot.licence_gate = SimpleNamespace(refusal=lambda: "")
    bot.daily_guard = SimpleNamespace(refusal=lambda: "")
    assert bot._entry_refusal() == ""
    bot.manual_pause.set(True, by="Telegram /pause")
    assert "Pause manuelle" in bot._entry_refusal()
    bot.positions = SimpleNamespace(list_open=lambda: [])
    bot.market_guard = SimpleNamespace(active_reason=lambda: "")
    text = bot._status_text()
    assert "Positions ouvertes : 0" in text and "Blocage : Pause manuelle" in text


def test_settings_save_the_telegram_commands(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    from binance_spot_manager.position_store import JsonFileStore

    store = JsonFileStore(tmp_path / "settings.json")
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price", lambda self, symbol: 500)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    app.text_input(key="telegram_owner_id_input").set_value("-100123")
    next(b for b in app.button if b.label == "Enregistrer les commandes").click().run()
    assert "telegram_owner_id" not in store.load()                                   # groupe refusé
    app.text_input(key="telegram_owner_id_input").set_value("123456789")
    next(b for b in app.button if b.label == "Enregistrer les commandes").click().run()
    assert not app.exception and store.load()["telegram_owner_id"] == "123456789"


def test_manual_reception_keeps_the_signals_the_worker_reads(tmp_path):
    """Relecture du 2026-10-05 : relève « manuelle » + commandes actives → le worker lit le bot ; il doit garder les
    signaux des conversations autorisées (sinon ils seraient consommés sans être enregistrés)."""
    handler, _ = commands(tmp_path)
    session = Session([{"update_id": 7, "message": {"message_id": 3, "date": 1000, "text": SIMPLE,
                                                    "chat": {"id": -100123, "type": "supergroup"}}}])
    preferences = {"signal_telegram_enabled": True, "signal_telegram_auto_enabled": False,
                   "signal_telegram_chats": "-100123"}
    inbox = SignalInbox(tmp_path / "s.db")
    poller = TelegramSignalPoller("token", "demo", lambda: preferences, inbox=inbox, session=session,
                                  commands=handler)
    items = poller.poll_once()
    assert len(items) == 1 and inbox.recent("demo")[0]["raw"] == SIMPLE


def test_a_failing_command_never_blocks_the_next_messages(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    broken = SimpleNamespace(handle=lambda message, edited=False: (_ for _ in ()).throw(OSError("disque plein")))
    session = Session([
        {"update_id": 20, "message": private("/pause")},
        {"update_id": 21, "message": {"message_id": 6, "date": 1000, "text": SIMPLE,
                                      "chat": {"id": -100123, "type": "supergroup"}}},
    ])
    items = import_telegram("token", {-100123}, inbox, "demo", session=session, commands=broken)
    assert len(items) == 1 and "non traitée" in session.posted[0]["text"]
    assert inbox.offset(__import__("hashlib").sha256(b"token").hexdigest()) == 22
