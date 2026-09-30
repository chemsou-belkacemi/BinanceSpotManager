"""Offline parser, durable deduplication and preparation safety tests."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from types import SimpleNamespace

import pytest

from binance_spot_manager.signal_parser import content_hash, parse_signal
from binance_spot_manager.signal_inbox import SignalInbox
from binance_spot_manager.signal_plan import (
    automatic_entry_allocations,
    automatic_signal_selection,
    automatic_tp_allocations,
    custom_signal_allocations,
    prepare_signal,
)
from binance_spot_manager.telegram_signals import (
    TelegramSignalPoller,
    chat_allowlist,
    import_telegram,
)
from binance_spot_manager.symbol_rules import parse_symbol_rules


def structured(pair, entries, targets, stop, platform="Binance"):
    return (f"📈 Trader/ Suhaib AlMashhadani\n✨ بسم الله توكلت على الله ✨\n"
            f"💎 PAIR: {pair}\n🔶 ENTRY ZONE:\n"
            + "\n".join(f"✨ ENTRY {i}: {p}" for i, p in enumerate(entries, 1))
            + "\n🎯 TARGETS:\n"
            + "\n───────────────────\n".join(f"{i}️⃣ T{i}: {p} (+2.08%)" for i, p in enumerate(targets, 1))
            + f"\n🛑 SL: {stop} (1h) (-0.81%)\n📅 Date: Monday - 2026-09-28\n"
              "⏰IndicatorTime :- 06:25 GMT+3\n"
            + f"🟠️ Platform: {platform}\n☪️ الحكم الشرعي: مباح ✅")


BICO = structured("BICO/USDT", [.02162, .02143], [.02207, .02225, .02244, .02264, .02292, .02327], .02135)
ARK = structured("ARK/USDT", [.2631, .2573], [.2677, .2714, .2742, .2784, .2826, .2884, .2936], .2547)
METIS = structured("METIS/USDT", [3.415, 3.3767], [3.4617, 3.5050, 3.5488, 3.5932, 3.6382, 3.6837], 3.335, "Bitget")
ABK = """🚨 ABK SIGNAL ALERT 🚨
🔰 Coin: LSKUSDT 🔰
▫️ Entry Zone: 0.3689 – 0.3673
🎯 Target 1 → 0.3765 [+2.06%]
🎯 Target 2 → 0.3835 [+3.96%]
🎯 Target 3 → 0.3961 [+7.37%]
🎯 Target 4 → 0.4214 [+14.23%]
🎯 Target 5 → 0.4529 [+22.77%]
🎯 Target 6 → 0.4969 [+34.70%]
🔴 Stop Loss: 0.3654 (15min) [-0.73%]
💡 Tip: إدارة رأس المال تسبق البحث عن الربح"""
SIMPLE = "PAIR: BTC/USDT\nENTRY 1: 84000\nT1: 90000\nSL: 80000"
GALA = """👑AL-MAHWASHI VIP👑
───────────────────
#GALA/USDT
───────────────────
📍 Entry1: 0.002116
───────────────────
🎯 TARGETS
───────────────────
🎯 TP1: 0.002210 (4.44%)
🎯 TP2: 0.002330 (10.11%)
🎯 TP3: 0.002514 (18.81%)
🎯 TP4: 0.002684 (26.84%)
🎯 TP5: 0.002974 (40.55%)
🎯 TP6: 0.003394 (60.40%)
───────────────────
🛑 Stop: 0.002032(1h)
───────────────────
☪️ الحكم الشرعي: مباح ✅
───────────────────
📅 Date: Monday - 2026-09-28"""


def test_mahwashi_gala_signal_extracts_all_prices_and_conditional_stop():
    result = parse_signal(GALA)
    assert result.errors == []
    assert result.template == "numbered"
    assert result.symbol == "GALAUSDT"
    assert result.direction == "BUY"
    assert result.entries == [.002116]
    assert result.targets == [.002210, .002330, .002514, .002684, .002974, .003394]
    assert result.stop == .002032
    assert result.stop_timeframe == "1h"
    assert not parse_signal(GALA, "numbered").errors


@pytest.mark.parametrize("text", [
    GALA.replace("Entry1", "Entry2"), GALA.replace("TP1:", "TP2:"),
    GALA.replace("Stop: 0.002032", "Stop: 0.002132"),
    GALA + "\nSHORT SELL", GALA + "\nLEVERAGE: 5X",
])
def test_numbered_template_does_not_bypass_direction_or_price_checks(text):
    assert parse_signal(text).errors


def test_reanalysis_refreshes_old_parser_errors_without_creating_new_signal(tmp_path):
    import json
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", GALA)
    legacy = dict(row["parsed"], template="simple", direction="", entries=[], stop=None,
                  errors=["Direction d'achat non identifiable.", "Un seul stop loss explicite est requis."])
    with inbox.connect() as db, db:
        db.execute("UPDATE signals SET parsed=? WHERE id=?", (json.dumps(legacy), row["id"]))
    refreshed = inbox.reanalyse("demo", row["id"])
    assert refreshed["id"] == row["id"]
    assert refreshed["parsed"]["errors"] == []
    assert refreshed["parsed"]["stop_timeframe"] == "1h"
    assert refreshed["payload"] is None
    assert len(inbox.recent("demo")) == 1
    inbox.freeze("demo", row["id"], {"plan": 1})
    with pytest.raises(ValueError, match="déjà confirmé"):
        inbox.reanalyse("demo", row["id"])


def test_importing_the_same_text_again_refreshes_a_stale_analysis_but_never_a_frozen_one(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", GALA)
    stale = dict(row["parsed"], entries=[], errors=["Objectif ambigu (ancien parseur)."])
    with inbox.connect() as db, db:
        db.execute("UPDATE signals SET parsed=? WHERE id=?", (json.dumps(stale), row["id"]))
    again = inbox.receive("demo", GALA)
    assert again["id"] == row["id"] and len(inbox.recent("demo")) == 1
    assert again["parsed"]["errors"] == [] and again["parsed"]["entries"] == [.002116]

    inbox.freeze("demo", row["id"], {"plan": 1})
    with inbox.connect() as db, db:
        db.execute("UPDATE signals SET parsed=? WHERE id=?", (json.dumps(stale), row["id"]))
    assert inbox.receive("demo", GALA)["parsed"]["errors"] == stale["errors"]

    edited = inbox.receive("demo", BICO, source="telegram", external_id="chat:9", edited=True)
    assert inbox.receive("demo", BICO)["parsed"]["errors"] == edited["parsed"]["errors"] != []


def test_reanalysis_preserves_source_edit_blocks_and_account_scope(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", GALA, source="telegram", external_id="chat:1", edited=True)
    with pytest.raises(ValueError, match="édité"):
        inbox.reanalyse("demo", row["id"])
    with pytest.raises(ValueError, match="absent"):
        inbox.reanalyse("other", row["id"])
    assert inbox.recent("demo")[0]["parsed"]["errors"] == row["parsed"]["errors"]


@pytest.mark.parametrize("text,symbol,entries,targets,stop,timeframe", [
    (BICO, "BICOUSDT", [.02162, .02143], [.02207, .02225, .02244, .02264, .02292, .02327], .02135, "1h"),
    (ARK, "ARKUSDT", [.2631, .2573], [.2677, .2714, .2742, .2784, .2826, .2884, .2936], .2547, "1h"),
    (ABK, "LSKUSDT", [.3689, .3673], [.3765, .3835, .3961, .4214, .4529, .4969], .3654, "15min"),
])
def test_three_supported_examples(text, symbol, entries, targets, stop, timeframe):
    result = parse_signal(text)
    assert not result.errors
    assert result.symbol == symbol
    assert result.entries == entries
    assert result.targets == targets
    assert result.stop == stop
    assert result.stop_timeframe == timeframe


ABO_YASEEIN = """ABO YASEEIN
──────────────────────
✨ بسم الله توكلت على الله ✨
──────────────────────
💎 PAIR: MOVR/USDT
🔶 ENTRY ZONE:
✨ENTRY 1: 1.135
✨ENTRY 2: 1.11861
──────────────────────
🎯 TARGETS:
1️⃣ T1: 1.15602 📉 (1.35%)
──────────────────────
2️⃣ T2: 1.17221 📉 (2.77%)
──────────────────────
3️⃣ T3: 1.338 📉 (17.31%)
──────────────────────
🛑 SL: 1.1092 (30m) (1.81%)
──────────────────────
📅Date: Tuesday - 2026-09-29

🟠 Platform: Binance
──────────────────────
☪️ الحكم الشرعي: مباح ✅"""
HARMONIC = """📈 HARMONIC TRADE DETECTED
Suhaib AlMashhadani Harmonic Indicator
──────────────────────
💎 PAIR: SAGA/USDT
🔶 ENTRY ZONE:
✨ENTRY 1 ✅: 0.02542
✨ENTRY 2 ✅: 0.024803
──────────────────────
🎯 TARGETS:
1️⃣ T1: 0.025907 📉 (1.92%)
──────────────────────
2️⃣ T2: 0.026407 📉 (3.88%)
──────────────────────
🛑 SL: 0.02446 (15m) (2.59%)
──────────────────────
📅Date: Wednesday - 2026-09-30
⏰IndicatorTime :- 23:44 GMT+3
──────────────────────
🟠 Platform: Binance"""


@pytest.mark.parametrize("text,symbol,entries,targets,stop,timeframe", [
    (ABO_YASEEIN, "MOVRUSDT", [1.135, 1.11861], [1.15602, 1.17221, 1.338], 1.1092, "30m"),
    (HARMONIC, "SAGAUSDT", [.02542, .024803], [.025907, .026407], .02446, "15m"),
])
def test_decorative_emoji_between_label_price_and_percentage_are_ignored(text, symbol, entries, targets, stop, timeframe):
    result = parse_signal(text)
    assert result.errors == []
    assert (result.symbol, result.entries, result.targets) == (symbol, entries, targets)
    assert (result.stop, result.stop_timeframe) == (stop, timeframe)


def test_emoji_never_joins_two_numbers_and_does_not_change_the_dedup_hash():
    assert parse_signal(SIMPLE.replace("90000", "90📉000")).errors
    assert content_hash(ABO_YASEEIN) != content_hash(ABO_YASEEIN.replace(" 📉", ""))


@pytest.mark.parametrize("text,reason", [
    ("🔔XAU/USD🔔\nDirection: BUY\nEntry Price: 4155.00\nTP1       4160.00\nTP2       4175.00\nTP3       4205.00\nSL          4105.00", "USDT/USDC"),
    (METIS, "autre que Binance"),
    ("**NIFTY**\n23700 -\nLongs - 2.03L (Intraday - 1.01L)\nShorts - 22934\n**BANKNIFTY**\nData neutral. VIX 13.24% up.", "Rapport de marché"),
    ("Sell Limit XRPUSD\nEntry: 1.4877\nSL: 1.5019\nTP: 1.4584\nRRR: 1:2.1\nThis order will become invalid within the next 24 hours.", "Short"),
    ("SHORT SELL #GWEIUSDT AT 0.02215\nTARGET - 0.01000+\nSTOPLOSS - 0.029400\nLEVERAGE 2X - 5X", "Short"),
    ("➡️ SHORT GOLD / XAUT\nEntry: 4476.00000000 - 4609.25000000\nTarget 1: 4340.75000000\nTarget 2: 4206.50000000\nTarget 3: 4072.25000000\nTarget 4: 3938.00000000\nStoploss: 4688.38875000\nLeverage: 15x", "Short"),
])
def test_six_unsupported_examples(text, reason):
    assert any(reason in error for error in parse_signal(text).errors)


@pytest.mark.parametrize("text", [
    SIMPLE + "\nPAIR: ETHUSDT", SIMPLE + "\nT1: 95000", SIMPLE + "\nSL: 81000",
    SIMPLE.replace("84000", "84,000"), SIMPLE.replace("84000", "-84000"),
    SIMPLE.replace("90000", "5%"), SIMPLE.replace("90000", "90000+"),
    SIMPLE.replace("T1", "T2"), SIMPLE.replace("80000", "85000"),
    SIMPLE.replace("T1: 90000", "T1: 90000\nT2: 89000"),
    "", "a" * 20001,
])
def test_ambiguous_or_invalid_signal_is_blocked(text):
    assert parse_signal(text).errors


def test_dates_quotes_and_forced_template():
    assert parse_signal(BICO).published_at == "2026-09-28T03:25:00+00:00"
    assert not parse_signal(SIMPLE.replace("USDT", "USDC")).errors
    assert parse_signal(BICO, "abk").errors
    assert parse_signal(BICO, "structured").errors == []


def test_missing_embedded_date_warning_is_ignored_only_for_telegram(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    manual = inbox.receive("manual", SIMPLE)
    telegram = inbox.receive(
        "telegram", SIMPLE, source="telegram", external_id="99:1",
        source_timestamp=1_790_763_600,
    )

    assert any("Date source non vérifiable" in warning for warning in manual["parsed"]["warnings"])
    assert not any("Date source non vérifiable" in warning for warning in telegram["parsed"]["warnings"])


def test_old_stored_telegram_warning_is_hidden_when_decoded(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive(
        "demo", SIMPLE, source="telegram", external_id="99:2",
        source_timestamp=1_790_763_600,
    )
    with inbox.connect() as db, db:
        parsed = json.loads(db.execute(
            "SELECT parsed FROM signals WHERE id=?", (row["id"],),
        ).fetchone()[0])
        parsed["warnings"].append(
            "Date source non vérifiable : contrôler manuellement la validité du signal."
        )
        db.execute("UPDATE signals SET parsed=? WHERE id=?", (json.dumps(parsed), row["id"]))

    saved = inbox.recent("demo")[0]
    assert not any("Date source non vérifiable" in warning for warning in saved["parsed"]["warnings"])


def test_deduplication_and_immutable_confirmation(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: inbox.receive("demo", SIMPLE)["id"], range(8)))
    assert len(set(ids)) == 1
    assert inbox.receive("demo", SIMPLE, source="telegram", external_id="1:2")["id"] == ids[0]
    assert inbox.receive("other", SIMPLE)["id"] != ids[0]
    inbox.freeze("demo", ids[0], {"plan": 1})
    assert SignalInbox(inbox.path).freeze("demo", ids[0], {"plan": 1}) == {"plan": 1}
    with pytest.raises(ValueError, match="déjà confirmé"):
        inbox.freeze("demo", ids[0], {"plan": 2})
    with pytest.raises(ValueError):
        inbox.freeze("another", ids[0], {})


@pytest.fixture
def rules():
    return parse_symbol_rules({"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001", "maxQty": "100"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "NOTIONAL", "minNotional": "5"}]})


def prepare(text, rules, **overrides):
    kwargs = dict(budget=200, available_quote=1000, reserve_percent=20, current_price=84500,
                  signal_id="test", validity_confirmed=True, touch_stop=True)
    return prepare_signal(parse_signal(text), rules, **(kwargs | overrides))


def test_preparation_preserves_levels_and_converts_remaining_percentages(rules):
    plan, payload = prepare(SIMPLE.replace("T1: 90000", "T1: 90000\nT2: 95000\nT3: 100000"), rules)
    position = payload["position"]
    assert [t["sell_percent"] for t in position["take_profits"]] == pytest.approx([100 / 3, 50, 100])
    assert [t.sell_percent for t in plan.take_profits] == pytest.approx([100 / 3] * 3)
    assert position["entries"][0]["resolved_price"] == 84000
    assert position["entries"][0]["order_type"] == "LIMIT"
    assert position["automation"]["cancel_remaining_entries_on_first_tp"]
    assert position["source_groups"][0]["signal_id"] == "test"
    assert position["entries"][0]["signal_id"] == "test"


def test_automatic_selection_and_tp_allocations_do_not_mutate_parsed_signal(rules):
    text = SIMPLE.replace(
        "ENTRY 1: 84000",
        "ENTRY 1: 84000\nENTRY 2: 83000",
    ).replace(
        "T1: 90000",
        "T1: 90000\nT2: 95000\nT3: 100000",
    )
    parsed = parse_signal(text)
    selected = automatic_signal_selection(parsed, entry_count=1, tp_count=2)
    assert selected.entries == [84000]
    assert selected.targets == [90000, 95000]
    assert parsed.entries == [84000, 83000]
    assert parsed.targets == [90000, 95000, 100000]
    assert automatic_tp_allocations(1) == [100]
    assert automatic_tp_allocations(2) == [70, 30]
    assert automatic_tp_allocations(3) == [50, 30, 20]
    assert automatic_tp_allocations(2, "EQUAL") == [50, 50]
    assert automatic_entry_allocations(2, "CUSTOM", "30;70") == [30, 70]
    assert automatic_tp_allocations(3, "CUSTOM", "50/30/20") == [50, 30, 20]
    assert custom_signal_allocations("33,3;66,7", 2, "entrées") == [33.3, 66.7]

    _, payload = prepare_signal(
        selected, rules, budget=200, available_quote=1000,
        reserve_percent=20, current_price=84500, signal_id="policy",
        validity_confirmed=True, touch_stop=True,
        tp_allocations=automatic_tp_allocations(2),
    )
    assert [tp["sell_percent"] for tp in payload["position"]["take_profits"]] == pytest.approx([70, 100])


@pytest.mark.parametrize("raw,count", [
    ("70;20", 2), ("70;30", 3), ("70;-30;60", 3), ("abc;30", 2),
])
def test_custom_allocations_are_strictly_validated(raw, count):
    with pytest.raises(ValueError):
        custom_signal_allocations(raw, count, "TP")


@pytest.mark.parametrize("overrides", [
    {"validity_confirmed": False}, {"budget": 0}, {"budget": float("nan")},
    {"budget": 1}, {"current_price": 90000}, {"current_price": 79999},
])
def test_preparation_blocks_unsafe_inputs(rules, overrides):
    with pytest.raises(ValueError):
        prepare(SIMPLE, rules, **overrides)


def test_conditional_stop_requires_explicit_interpretation(rules):
    with pytest.raises(ValueError, match="SL conditionnel"):
        prepare(SIMPLE.replace("SL: 80000", "SL: 80000 (1h)"), rules, touch_stop=False)


def test_telegram_allowlist_offset_and_edits(tmp_path):
    inbox = SignalInbox(tmp_path / "inbox.db")
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 99}, "message_id": 1, "text": SIMPLE}},
        {"update_id": 2, "channel_post": {"chat": {"id": -123}, "message_id": 2, "text": SIMPLE}},
        {"update_id": 3, "edited_channel_post": {"chat": {"id": -123}, "message_id": 2, "text": SIMPLE.replace("90000", "91000")}},
    ]
    calls = []
    session = SimpleNamespace(get=lambda url, **kw: calls.append(kw) or SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": updates}))
    received = import_telegram("secret", {-123}, inbox, "demo", session=session)
    assert len(received) == 2
    assert not received[0]["parsed"]["errors"]
    assert any("édité" in e for e in received[1]["parsed"]["errors"])
    assert len(inbox.recent("demo")) == 2
    original = next(row for row in inbox.recent("demo") if row["raw"] == SIMPLE)
    assert any("ancienne version invalidée" in error for error in original["parsed"]["errors"])
    with pytest.raises(ValueError, match="bloqué"):
        inbox.freeze("demo", original["id"], {"plan": 1})
    import_telegram("secret", {-123}, inbox, "demo", session=session)
    assert calls[1]["params"]["offset"] == 4
    assert len(inbox.recent("demo")) == 2
    assert chat_allowlist("-123, 42") == {-123, 42}
    with pytest.raises(ValueError):
        chat_allowlist("@channel")


def test_telegram_never_exposes_token(tmp_path):
    import requests
    def fail(*args, **kwargs):
        raise requests.ConnectionError("https://api.telegram.org/botSECRET/getUpdates")
    with pytest.raises(ValueError) as exc:
        import_telegram("SECRET", {1}, SignalInbox(tmp_path / "inbox.db"), "demo", session=SimpleNamespace(get=fail))
    assert "SECRET" not in str(exc.value)


def test_telegram_long_poll_uses_durable_inbox_and_reports_diagnostics(tmp_path):
    inbox = SignalInbox(tmp_path / "inbox.db")
    update = {
        "update_id": 7,
        "message": {"chat": {"id": 99}, "message_id": 4, "text": SIMPLE},
    }
    calls = []
    session = SimpleNamespace(get=lambda url, **kwargs: calls.append(kwargs) or SimpleNamespace(
        status_code=200, json=lambda: {"ok": True, "result": [update]},
    ))
    poller = TelegramSignalPoller(
        "secret", "demo",
        lambda: {
            "signal_telegram_enabled": True,
            "signal_telegram_auto_enabled": True,
            "signal_telegram_chats": "99",
        },
        inbox=inbox, session=session, poll_timeout=20, clock=lambda: 1234.0,
    )

    received = poller.poll_once()

    assert len(received) == 1
    assert calls[0]["params"]["timeout"] == 20
    assert calls[0]["timeout"] == (3, 25)
    assert inbox.offset(hashlib.sha256(b"secret").hexdigest()) == 8
    assert poller.snapshot() == {
        "state": "CONNECTED", "running": True,
        "last_poll_at": 1234.0, "last_received_at": 1234.0,
        "last_batch_count": 1, "received_total": 1,
        "failures": 0, "last_error": "", "poll_timeout_seconds": 20,
    }


def test_telegram_poller_does_not_call_api_when_automatic_reader_is_disabled(tmp_path):
    calls = []
    session = SimpleNamespace(get=lambda *args, **kwargs: calls.append(1))
    preferences = {
        "signal_telegram_enabled": True,
        "signal_telegram_auto_enabled": False,
        "signal_telegram_chats": "99",
    }
    poller = TelegramSignalPoller(
        "secret", "demo", lambda: preferences,
        inbox=SignalInbox(tmp_path / "inbox.db"), session=session,
    )

    assert poller.poll_once() == []
    assert calls == []
    assert poller.snapshot()["state"] == "MANUAL"


def test_edited_message_invalidates_both_versions_even_if_new_text_already_imported(tmp_path):
    inbox = SignalInbox(tmp_path / "inbox.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id="chat:1")
    edited_text = SIMPLE.replace("90000", "91000")
    manual = inbox.receive("demo", edited_text)
    edited = inbox.receive("demo", edited_text, source="telegram", external_id="chat:1", edited=True)
    assert edited["id"] == manual["id"]
    assert all(row["parsed"]["errors"] for row in inbox.recent("demo"))
