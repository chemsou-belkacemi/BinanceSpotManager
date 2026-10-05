"""Nom du trader écrit en tête d'un signal (messages recopiés par un relais, sans transfert).

Les en-têtes viennent des exports Telegram du propriétaire (LEGEND TRADING, AL-MAHWASHI CRYPTO,
IN CRYPTO, fichiers de son robot), raccourcis : une ligne d'événement ou une formule avant le nom, des
préfixes et suffixes à retirer, et des messages sans aucun nom, qui doivent rester sans nom.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from binance_spot_manager.models import SignalSource, SourceGroup
from binance_spot_manager.performance import (
    UNKNOWN_TELEGRAM,
    channel_resolver,
    losing_channel,
    stats_by,
)
from binance_spot_manager.signal_auto_execution import channel_name
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.telegram_signals import origin_label
from binance_spot_manager.trader_name import name_key, trader_of
from test_automation import events, make_position, rules, settings  # noqa: F401 - fixtures partagees
from test_journal_telegram_corrections import losing_position
from test_suivi_canaux_protection import NOW, labelled, winning_position

BODY = "\n#SOL/USDT\n📍 Entry1: 150\n🎯 TP1: 160\n🛑 Stop: 140"


@pytest.mark.parametrize("header, expected", [
    ("👑AL-MAHWASHI VIP👑\n───────────────────\n", "AL-MAHWASHI VIP"),
    ("👑 AL-MAHWASHI CRYPTO 👑\n✨ بسم الله توكلت على الله ✨\n", "AL-MAHWASHI CRYPTO"),
    ("👑 💎 HAMZAWY 💎 👑\n━━━━━━━━━━━━━━\n", "HAMZAWY"),
    ("👑 ام البنين 👑\n", "ام البنين"),
    ("📈 Trader/ Suhaib AlMashhadani\n✨ بسم الله توكلت على الله ✨\n", "Suhaib AlMashhadani"),
    ("📈 Trader/Suhaib Al-Mashhadani\n", "Suhaib Al-Mashhadani"),
    ("📈 HARMONIC TRADE DETECTED\nSuhaib AlMashhadani Harmonic Indicator\n──────\n✨ بسم الله توكلت على الله ✨\n",
     "Suhaib AlMashhadani"),
    ("*Harmonic Pattern Detected*\nAl-Afify Harmonic Indicator Ultra\nبسم الله توكلت على الله\n", "Al-Afify"),
    ("TIME-BASED TRADE DETECTED\nABD ELOUADOUD TIME CYCLE INDICATOR\nبسم الله توكلت على الله\n", "ABD ELOUADOUD"),
    ("*INCRYPTO TIME ANALYSIS INDICATOR*\nYASMINA BOUZID INDICATOR\nبسم الله الرحمن الرحيم\n", "YASMINA BOUZID"),
    ("🚨 ABK SIGNAL ALERT 🚨\n🌟 SPECIAL TRADE 🌟\n", "ABK"),
    ("SUHAIB ALMASHHADANI SIGNAL - Bat Pattern Detected\n", "SUHAIB ALMASHHADANI"),
    ("👑 ALAFIFY TP TRACKING 👑\n", "ALAFIFY"),
    ("معاينة الصفقة:\n👑 Abo yaseein 👑\n", "Abo yaseein"),
    ("Ph. Suhaib AlMashhadani\n───────────────────\n", "Suhaib AlMashhadani"),
    # Relecture du 2026-10-05 : donnée, date ou mot-dièse entre le nom et la paire, particule détachée, prénom.
    ("👑 HAMZAWY 👑\nType: Spot\nMarket: Spot\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\n05/10/2026 14:00\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\n#SOL\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\nإشارة شراء\n", "HAMZAWY"),
    ("👑 AL - MAHWASHI VIP 👑\n", "AL-MAHWASHI VIP"),
    ("👑 عبد الرحمن 👑\nبسم الله الرحمن الرحيم\n", "عبد الرحمن"),
    ("Trader: Abdallah Al-Abyed\n", "Abdallah Al-Abyed"),
    # Deuxième relecture : formule vocalisée, « NOM : événement », autres séparateurs, dates, $SOL, descriptions.
    ("👑 HAMZAWY 👑\nبسم اللّه\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\nتوكّلت على الله\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\nبسـم الله\n", "HAMZAWY"),
    ("LEGEND TRADING: NEW SIGNAL\n", "LEGEND TRADING"),
    ("ALAFIFY : Spot Trade\n", "ALAFIFY"),
    ("👑 HAMZAWY 👑\nType = Spot\nType | Spot\nType → Spot\nRisk Level - High\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\n05-10\n5/10\n14.00\n14h00\n5 Oct 2026\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\n$SOL\n*#SOL*\n• #SOL\n", "HAMZAWY"),
    ("MOHAMED BEN - New Signal\n", "MOHAMED BEN"),
    ("AL-MAHWASHI CRYPTO - VIP\n", "AL-MAHWASHI CRYPTO - VIP"),
    ("Suhaib AlMashhadani - Shark Pattern\n", "Suhaib AlMashhadani"),
    # Troisième vérification : durcissements facultatifs.
    ("2 Main Traders\n", "2 Main Traders"),                                        # « Main » n'est pas « mai »
    ("👑 HAMZAWY 👑\n(#SOL)\n1. #SOL\nt.me/legend\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\nMonday\nOctober 5\n2PM UTC\n", "HAMZAWY"),
    ("👑 HAMZAWY 👑\nSOL\nBTC Analysis\nMid-Term\nLong Term Hold\n", "HAMZAWY"),
    ("HAMZAWY - Daily Chart\n", "HAMZAWY"),
    ("SUHAIB ALMASHHADANI SIGNAL - Gartley\n", "SUHAIB ALMASHHADANI"),
    ("Suhaib AlMashhadani: Bat Pattern\n", "Suhaib AlMashhadani"),
    ("المحلل: حمزاوي\n", "حمزاوي"),
])
def test_the_trader_is_read_from_the_header(header, expected):
    assert trader_of(header + BODY) == expected


@pytest.mark.parametrize("text", [
    "┏━━━━━━━━━━━━━━━━━━┓\n┃     #RIF/USDT    ┃\n┗━━━━━━━━━━━━━━━━━━┛\n📍 Entry 1: 0.0614\n🎯 TP1: 0.065\n🛑 SL: 0.058",
    "👑 ⚡ 👑\n━━━━━━━━━━━━━━" + BODY,
    "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000",
    "#BTC/USDT\n👑 HAMZAWY 👑\nEntry1: 84000\nTP1: 90000\nStop: 80000",           # nom après la paire : non lu
    "✨ بسم الله توكلت على الله ✨" + BODY,
    "Nous avons une très belle opportunité aujourd'hui sur le marché, regardez bien ce graphique" + BODY,
    "",
    "🚨 VIP SIGNAL 🚨" + BODY,                                                  # mots banals seuls : personne
    "CRYPTO VIP" + BODY,
    "IN CRYPTO" + BODY,
    "توصيات كريبتو" + BODY,
    "Note: New Signal" + BODY,
    "Good morning traders\nVIP SIGNAL" + BODY,                                   # salutation : pas un nom
])
def test_no_name_is_invented(text):
    assert trader_of(text) == ""


def test_spelling_variants_share_one_key():
    assert len({name_key(n) for n in ("Suhaib AlMashhadani", "SUHAIB ALMASHHADANI", "Suhaib Al-Mashhadani")}) == 1
    assert len({name_key(n) for n in ("Abo yaseein", "ABO YASEEIN", "Aboyaseein")}) == 1
    assert name_key("AL-MAHWASHI CRYPTO TRADING") == name_key("AL-MAHWASHI CRYPTO") == "ALMAHWASHI CRYPTO"  # alias
    assert name_key("AL-MAHWASHI VIP") != name_key("AL-MAHWASHI CRYPTO")           # VIP reste distinct
    assert name_key("Légende") == name_key("LEGENDE")
    # forme stricte : aucun mot retiré, deux canaux qui partagent un mot restent distincts
    assert len({name_key(n) for n in ("CRYPTO LEGEND", "LEGEND TRADING", "Legend Trader")}) == 3


def test_the_written_trader_wins_over_the_forwarded_channel_and_the_relay():
    message = {"text": "👑 HAMZAWY 👑" + BODY, "chat": {"title": "Relais"},
               "forward_origin": {"type": "channel", "chat": {"title": "LEGEND TRADING"}}}
    assert origin_label(message) == "HAMZAWY"
    assert origin_label({"caption": "👑 HAMZAWY 👑" + BODY, "chat": {"title": "Relais"}}) == "HAMZAWY"
    assert origin_label(message | {"text": "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"}) == \
        "LEGEND TRADING"


def test_rows_received_before_the_change_get_their_trader_from_the_text():
    row = {"source": "telegram", "external_id": "ab:-100123:1", "origin": "", "raw": "👑 HAMZAWY 👑" + BODY}
    assert channel_name(row, {}) == "HAMZAWY"
    assert channel_name(row | {"origin": "Relais"}, {}) == "HAMZAWY"
    assert channel_name(row | {"raw": "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"}, {}) == \
        "telegram -100123"


def test_statistics_merge_spellings_under_the_most_frequent(rules):  # noqa: F811
    positions = [labelled(losing_position(rules), "Suhaib AlMashhadani"),
                 labelled(winning_position(rules), "Suhaib AlMashhadani"),
                 labelled(winning_position(rules), "SUHAIB ALMASHHADANI")]
    (group,) = stats_by(positions)
    assert group.name == "Suhaib AlMashhadani" and group.positions == 3 and group.wins == 2


def old_position(rules, row_id, label="Signal structured"):  # noqa: F811
    position = labelled(losing_position(rules), label)
    position.tags = ["signal", row_id]
    return position


def test_old_positions_find_their_trader_in_the_signal_text(rules):  # noqa: F811
    texts = {"row-1": "👑 HAMZAWY 👑" + BODY, "row-2": "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"}
    asked = []

    def raw_text(row_id):
        asked.append(row_id)
        if row_id == "row-3":
            raise OSError("boîte illisible")
        return texts.get(row_id, "")

    resolve = channel_resolver(raw_text)
    assert resolve(old_position(rules, "row-1")) == "HAMZAWY"
    assert resolve(old_position(rules, "row-1")) == "HAMZAWY" and asked == ["row-1"]       # lu une fois
    assert resolve(old_position(rules, "row-2")) == UNKNOWN_TELEGRAM                          # aucun nom écrit
    assert resolve(old_position(rules, "row-3")) == UNKNOWN_TELEGRAM
    assert resolve(old_position(rules, "row-4", label="Canal A")) == "Canal A" and "row-4" not in asked
    manual = old_position(rules, "row-1")
    manual.source_groups = [SourceGroup(source=SignalSource.MANUAL, label="Signal simple")]
    assert resolve(manual) == "HAMZAWY"                                   # signal collé à la main
    assert resolve(make_position(rules)) == "manuel"                      # trade manuel, sans signal


def test_a_losing_trader_counts_its_old_positions(rules):  # noqa: F811
    losers = [old_position(rules, f"row-{i}") for i in range(10)]
    resolve = channel_resolver(lambda row_id: "👑 HAMZAWY 👑" + BODY)
    assert losing_channel(losers, "HAMZAWY", min_trades=10) is None              # sans le texte : inconnu
    assert "« HAMZAWY » perdant" in losing_channel(losers, "HAMZAWY", min_trades=10, key=resolve)
    assert "perdant" in losing_channel(losers, "hamzawy", min_trades=10, key=resolve)


def test_the_worker_report_names_old_positions_from_the_inbox(tmp_path, rules, monkeypatch):  # noqa: F811
    from scripts import bot_worker

    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", "👑 HAMZAWY 👑" + BODY, source="telegram", external_id="ab:-100123:7")
    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    worker.signal_inbox, worker.settings = inbox, SimpleNamespace()
    monkeypatch.setattr(bot_worker, "account_scope", lambda settings: "demo")
    assert worker._channel_resolver()(old_position(rules, row["id"])) == "HAMZAWY"
    del worker.signal_inbox
    assert worker._channel_resolver()(old_position(rules, row["id"])) == UNKNOWN_TELEGRAM


def test_history_names_old_positions_by_their_trader(monkeypatch, tmp_path, rules):  # noqa: F811
    from streamlit.testing.v1 import AppTest

    from binance_spot_manager import command_store, signal_inbox
    from binance_spot_manager.position_store import PositionStore
    from ui_common import get_service

    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", "👑 HAMZAWY 👑" + BODY, source="telegram", external_id="ab:-100123:7")
    store = PositionStore(tmp_path / "positions")
    store.save(old_position(rules, row["id"]))
    store.save(labelled(winning_position(rules), "Suhaib AlMashhadani"))
    service = get_service()
    monkeypatch.setattr(service, "positions", store)
    monkeypatch.setattr(service, "fee_rates", lambda position: {})
    monkeypatch.setattr(signal_inbox, "SignalInbox", lambda: inbox)
    monkeypatch.setattr(command_store, "account_scope", lambda settings: "demo")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "pages" / "4_History.py"),
                            default_timeout=30).run()
    assert not app.exception
    tables = [frame.value for frame in app.dataframe]
    by_trader = next(t for t in tables if "PnL net" in t.columns)
    assert sorted(by_trader["Trader / canal"]) == ["HAMZAWY", "Suhaib AlMashhadani"]
    detail = next(t for t in tables if "Symbole" in t.columns)
    assert sorted(detail["Trader / canal"]) == ["HAMZAWY", "Suhaib AlMashhadani"]


def test_the_daily_report_names_old_positions_by_their_trader(tmp_path, rules, events, monkeypatch):  # noqa: F811
    from binance_spot_manager.position_store import PositionStore
    from scripts import bot_worker

    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", "👑 HAMZAWY 👑" + BODY, source="telegram", external_id="ab:-100123:7")
    position = old_position(rules, row["id"])
    position.closed_at = NOW - timedelta(hours=1)
    store = PositionStore(tmp_path / "positions")
    store.save(position)
    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    worker.signal_inbox, worker.settings, worker.positions, worker.events = inbox, SimpleNamespace(), store, events
    worker.client = SimpleNamespace(get_price=lambda symbol: None)
    sent = []
    worker.notifications = SimpleNamespace(daily_report=lambda title, body: body, notify=sent.append)
    worker.daily_report = SimpleNamespace(due=lambda: NOW, mark_sent=lambda moment: None)
    monkeypatch.setattr(bot_worker, "account_scope", lambda settings: "demo")
    worker._send_daily_report()
    assert sent and "Trader/canal (7 j) : HAMZAWY" in sent[0]
