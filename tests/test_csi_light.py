"""Garde-fou « Feu de protection CSI » (csi_light.py) : désactivé par défaut et sans effet ; ROUGE retient les entrées
automatiques (et manuelles seulement si réglé) ; ORANGE réduit la taille comme le trader perdant ; CSI injoignable ou
INCONNU → « aucune action » ou « prudence » ; positions ouvertes jamais touchées ; formulaire Settings ; contrôle de
la mutation (garde-fou ignoré → le signal partirait). Aucun réseau (tests/conftest.py), aucun ordre."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_automation import events, rules, settings  # noqa: F401 - fixtures partagées
from test_signal_auto_execution import SIMPLE, enabled_preferences, executor, telegram_id

from binance_spot_manager import csi_light as cl
from binance_spot_manager.command_processor import NEW_ENTRY_ACTIONS
from binance_spot_manager.csi_client import CsiClient, CsiUnavailable
from binance_spot_manager.notification_engine import NotificationEngine
from binance_spot_manager.signal_inbox import SignalInbox

ON = {"csi_light_enabled": True}


def reading(color: str, explanation: str = "") -> dict:
    return {"color": color, "explanation": explanation or f"Feu {color} : raison de test.",
            "computed_at": "2026-10-08T10:00:00+00:00", "places_orders": False}


class FakeCsi:
    """Client CSI factice : une suite de réponses (dict) ou de pannes (exception), la dernière se répète."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def meteo(self, *, timeout=None):
        assert timeout == (2.0, 3.0)                                 # connexion, lecture : le worker n'attend pas
        self.calls += 1
        answer = self.answers[min(self.calls, len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


def guard(tmp_path, client, preferences, clock=None):
    clock = clock or [1_000_000.0]
    return cl.CsiLightGuard(client, lambda: preferences, path=tmp_path / "csi_light.json", clock=lambda: clock[0]), clock


# -- réglages et règle pure --------------------------------------------------------------------------------------

def test_settings_are_off_by_default_and_bounded():
    assert cl.LightPolicy.from_mapping({}) == cl.LightPolicy(enabled=False, red_action="AUTO", orange_action="REDUCE",
                                                             kept_percent=50.0, when_unavailable="NONE")
    odd = cl.LightPolicy.from_mapping({"csi_light_red_action": "tout", "csi_light_orange_action": "x",
                                       "csi_light_orange_kept_percent": 5, "csi_light_when_unavailable": 3})
    assert (odd.red_action, odd.orange_action, odd.kept_percent, odd.when_unavailable) == ("AUTO", "REDUCE", 10.0, "NONE")
    assert cl.LightPolicy.from_mapping({"csi_light_orange_kept_percent": float("nan")}).kept_percent == 50.0
    assert cl.LightPolicy.from_mapping({"csi_light_orange_kept_percent": "abc"}).kept_percent == 50.0
    assert cl.LightPolicy(kept_percent=50.0).reduced(90.0) == 45.0 and cl.LightPolicy(kept_percent=30.0).reduced(10.0) == 3.0


@pytest.mark.parametrize("color", ["VERT", "ORANGE", "ROUGE", "INCONNU"])
def test_disabled_does_nothing_whatever_the_color(color):
    effect = cl.effect_of(cl.LightPolicy(), reading(color))
    assert effect == cl.LightEffect(cl.DISABLED, detail=effect.detail) and not effect.active


def test_the_rule_of_each_color():
    auto, everything = cl.LightPolicy(enabled=True), cl.LightPolicy(enabled=True, red_action="ALL")
    red = cl.effect_of(auto, reading("ROUGE", "Feu ROUGE : volatilité très haute."))
    assert red.block_auto and not red.block_manual and red.kept_percent is None and "volatilité très haute" in red.detail
    assert cl.effect_of(everything, reading("ROUGE")).block_manual
    orange = cl.effect_of(auto, reading("ORANGE"))
    assert orange.kept_percent == 50.0 and not orange.block_auto and "50 %" in orange.detail
    assert not cl.effect_of(cl.LightPolicy(enabled=True, orange_action="NONE"), reading("ORANGE")).active
    assert not cl.effect_of(auto, reading("VERT")).active
    assert not cl.effect_of(auto, reading("VIOLET")).active                             # couleur illisible : INCONNU
    caution = cl.LightPolicy(enabled=True, when_unavailable="CAUTION")
    assert cl.effect_of(caution, reading("INCONNU")).kept_percent == 50.0
    assert cl.effect_of(caution, reading("VIOLET")).kept_percent == 50.0


@pytest.mark.parametrize("when,expected", [("NONE", None), ("CAUTION", 50.0)])
def test_unreachable_csi_gives_no_action_or_caution(tmp_path, when, expected):
    client = FakeCsi(CsiUnavailable("CSI injoignable sur http://csi-api:8503 (ConnectionError)"))
    light, _ = guard(tmp_path, client, ON | {"csi_light_when_unavailable": when})
    assert light.check() == []
    effect = light.effect()
    assert effect.color == cl.UNREACHABLE and effect.kept_percent == expected and not effect.block_auto
    assert "injoignable" in effect.detail
    # Même chose sans client CSI configuré.
    nobody, _ = guard(tmp_path / "x", None, ON | {"csi_light_when_unavailable": when})
    (tmp_path / "x").mkdir()
    nobody.check()
    assert nobody.effect().color == cl.UNREACHABLE and nobody.effect().kept_percent == expected


# -- lecture, cache, événements ----------------------------------------------------------------------------------

def test_disabled_guard_never_calls_csi(tmp_path):
    client = FakeCsi(reading("ROUGE"))
    light, _ = guard(tmp_path, client, {})
    assert light.check() == [] and client.calls == 0 and light.effect().color == cl.DISABLED
    assert light.auto_refusal() == "" and light.manual_refusal() == "" and light.status_line() == "Feu CSI : désactivé"


def test_red_starts_and_ends_once_and_survives_a_restart(tmp_path):
    client = FakeCsi(reading("ROUGE"), reading("VERT"))
    light, clock = guard(tmp_path, client, ON)
    started = light.check()
    assert [kind for kind, _ in started] == ["STARTED"] and "aucune nouvelle entrée automatique" in started[0][1]
    assert light.auto_refusal() and light.manual_refusal() == ""
    clock[0] += 60
    assert light.check() == [] and client.calls == 1                                   # cache de 5 minutes
    restarted, _ = guard(tmp_path, FakeCsi(reading("ROUGE")), ON, clock)
    assert restarted.check() == [] and restarted.auto_refusal()                          # pas de second « début »
    clock[0] += cl.CACHE_SECONDS
    ended = light.check()
    assert [kind for kind, _ in ended] == ["ENDED"] and "VERT" in ended[0][1] and light.auto_refusal() == ""
    assert light.check() == []


def test_a_short_csi_outage_keeps_the_last_reading_then_lets_go(tmp_path):
    client = FakeCsi(reading("ROUGE"), CsiUnavailable("CSI injoignable"))
    light, clock = guard(tmp_path, client, ON)
    light.check()
    clock[0] += cl.CACHE_SECONDS
    assert light.check() == [] and light.effect().block_auto                            # 5 min : lecture gardée
    clock[0] += cl.STALE_SECONDS
    assert [kind for kind, _ in light.check()] == ["ENDED"]                             # trop vieille : injoignable
    assert light.effect().color == cl.UNREACHABLE and not light.effect().active


def test_disabling_in_settings_lifts_the_red_at_once(tmp_path):
    preferences = dict(ON)
    light, _ = guard(tmp_path, FakeCsi(reading("ROUGE")), preferences)
    light.check()
    preferences["csi_light_enabled"] = False
    ended = light.check()
    assert [kind for kind, _ in ended] == ["ENDED"] and "désactivé" in ended[0][1] and not light.effect().active


def test_dashboard_status_shows_only_an_active_recent_light(tmp_path):
    light, clock = guard(tmp_path, FakeCsi(reading("ORANGE")), ON)
    light.check()
    state = cl.active_status(tmp_path / "csi_light.json", now=clock[0] + 60)
    assert state is not None and state["kept_percent"] == 50.0 and not state["block_auto"]
    assert cl.active_status(tmp_path / "csi_light.json", now=clock[0] + cl.DISPLAY_MAX_AGE_SECONDS + 1) is None
    green, _ = guard(tmp_path / "g", FakeCsi(reading("VERT")), ON, clock)
    (tmp_path / "g").mkdir()
    green.check()
    assert cl.active_status(tmp_path / "g" / "csi_light.json", now=clock[0]) is None


def test_the_client_reads_get_meteo_with_the_token():
    seen = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return reading("VERT")

    def request(method, url, **kwargs):
        seen.update(method=method, url=url, headers=kwargs["headers"], timeout=kwargs["timeout"])
        return Response()

    client = CsiClient("http://csi-api:8503", "jeton", session=SimpleNamespace(request=request))
    assert client.meteo(timeout=(2.0, 3.0))["color"] == "VERT"
    assert seen == {"method": "GET", "url": "http://csi-api:8503/meteo",
                    "headers": {"Accept": "application/json", "Authorization": "Bearer jeton"}, "timeout": (2.0, 3.0)}


# -- routage automatique ----------------------------------------------------------------------------------------

def queued(tmp_path, *, reason="", kept=None, detail=""):
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences())
    worker.csi_light_reason, worker.csi_light_kept_percent, worker.csi_light_detail = reason, kept, detail
    outcome = worker.process_pending()
    command = commands.get_by_request_key("demo", f"signal:{row['id']}")
    return outcome, worker, inbox, (command or {}).get("payload")


def test_disabled_changes_nothing_in_the_routing(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, _, _, baseline = queued(tmp_path / "a")
    light, _ = guard(tmp_path, FakeCsi(reading("ROUGE")), {})                             # garde-fou désactivé
    light.check()
    effect = light.effect()
    outcome, _, _, payload = queued(tmp_path / "b", reason=light.auto_refusal(), kept=effect.kept_percent)
    assert outcome == ["QUEUED"] and payload["route"]["metrics"]["budget"] == baseline["route"]["metrics"]["budget"] == 90
    assert "csi_light_reduction" not in payload["route"]["metrics"]


def test_red_holds_automatic_signals_in_the_inbox(tmp_path):
    outcome, worker, inbox, payload = queued(tmp_path, reason="Feu CSI ROUGE : aucune nouvelle entrée automatique")
    assert outcome == [] and payload is None
    assert worker.snapshot()["state"] == "FEU_CSI" and "ROUGE" in worker.snapshot()["last_detail"]
    assert inbox.recent("demo")[0]["auto_state"] == ""                                   # reste dans la boîte
    worker.csi_light_reason = ""
    assert worker.process_pending() == ["QUEUED"]


def test_orange_reduces_the_size_like_a_losing_trader(tmp_path):
    outcome, _, _, payload = queued(tmp_path, kept=50.0, detail="Feu CSI ORANGE : taille réduite à 50 %.")
    metrics = payload["route"]["metrics"]
    assert outcome == ["QUEUED"] and metrics["budget"] == pytest.approx(45.0)
    assert metrics["csi_light_reduction"] == {"detail": "Feu CSI ORANGE : taille réduite à 50 %.", "kept_percent": 50.0,
                                              "budget_before": 90.0, "budget": 45.0}
    entries = payload["position"]["entries"]
    assert sum(e["quote_amount"] for e in entries) == pytest.approx(45.0, abs=0.01)


# -- worker : manuel, /statut, notifications, positions ------------------------------------------------------------

def light_worker(tmp_path, events, preferences, answers):  # noqa: F811
    from scripts import bot_worker

    worker = bot_worker.Worker.__new__(bot_worker.Worker)
    worker.csi_light, _ = guard(tmp_path, FakeCsi(*answers), preferences)
    worker.events = events
    worker.auto_signal_executor = SimpleNamespace(csi_light_reason="", csi_light_kept_percent=None, csi_light_detail="")
    sent = []
    worker.notifications = SimpleNamespace(csi_light=lambda kind, detail: (kind, detail), notify=sent.append)
    worker.licence_gate = SimpleNamespace(refusal=lambda: "")
    worker.daily_guard = SimpleNamespace(refusal=lambda: "")

    class Untouchable:
        """Positions, client Binance et automatisation : le feu ne doit jamais y toucher."""

        def __getattr__(self, name):
            raise AssertionError(f"le feu CSI a touché à {name}")

    worker.client = worker.execution = worker.automation = Untouchable()
    worker.positions = SimpleNamespace(list_open=list)
    return worker, sent


def test_red_blocks_manual_entries_only_when_set(tmp_path, events):  # noqa: F811
    (tmp_path / "auto").mkdir()
    (tmp_path / "all").mkdir()
    worker, sent = light_worker(tmp_path / "auto", events, ON, [reading("ROUGE")])
    worker._check_csi_light()
    assert worker.auto_signal_executor.csi_light_reason and worker._entry_refusal() == ""   # manuel permis
    assert sent and sent[0][0] == "STARTED" and events.tail(limit=1)[0]["event"] == "CSI_LIGHT_STARTED"
    status = worker._status_text()
    assert status.count("Blocage : Feu CSI ROUGE") == 1 and "Feu CSI : ROUGE" not in status  # une seule fois
    everything, _ = light_worker(tmp_path / "all", events, ON | {"csi_light_red_action": "ALL"}, [reading("ROUGE")])
    everything._check_csi_light()
    assert "manuelle ou automatique" in everything._entry_refusal()
    assert "SUBMIT_POSITION" in NEW_ENTRY_ACTIONS                                         # seul un achat est refusé
    assert not any(word in action for action in NEW_ENTRY_ACTIONS for word in ("CLOSE", "SELL", "CANCEL", "STOP"))


def test_orange_and_green_reach_the_executor_and_status(tmp_path, events):  # noqa: F811
    worker, sent = light_worker(tmp_path, events, ON | {"csi_light_orange_kept_percent": 30}, [reading("ORANGE")])
    worker._check_csi_light()
    assert worker.auto_signal_executor.csi_light_kept_percent == 30.0 and worker.auto_signal_executor.csi_light_reason == ""
    assert worker._entry_refusal() == "" and sent == [] and "Feu CSI : ORANGE" in worker._status_text()


def test_a_broken_guard_never_stops_the_worker(tmp_path, events):  # noqa: F811
    worker, _ = light_worker(tmp_path, events, ON, [reading("ROUGE")])
    worker._check_csi_light()

    def broken():
        raise OSError("disque")

    worker.csi_light = SimpleNamespace(check=broken)
    worker._check_csi_light()                                                             # jamais bloquant
    assert worker.auto_signal_executor.csi_light_reason


def test_light_messages(settings):  # noqa: F811
    engine = NotificationEngine(settings)
    started = engine.csi_light("STARTED", "Feu CSI ROUGE : aucune nouvelle entrée automatique")
    assert started.event == "CSI_LIGHT" and started.level == "WARNING" and "aucun gain demontre" in started.body
    assert engine.csi_light("ENDED", "fin").level == "INFO"


def test_mutation_the_guard_ignored_lets_the_red_signal_go(tmp_path, events, monkeypatch):  # noqa: F811
    """Contrôle de la mutation : le même tour sans le garde-fou (contrôle remplacé par rien) enverrait le signal ; avec
    le garde-fou, il reste dans la boîte. Si le worker ou le routage ignorait le feu, le premier cas échouerait."""
    from scripts import bot_worker

    def run(ignore: bool, folder: Path):
        folder.mkdir()
        inbox = SignalInbox(folder / "signals.db")
        inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
        executor_, _ = executor(folder, inbox, enabled_preferences())
        worker, _ = light_worker(folder, events, ON, [reading("ROUGE")])
        worker.auto_signal_executor = executor_
        if ignore:
            monkeypatch.setattr(bot_worker.Worker, "_check_csi_light", lambda self: None)
        worker._check_csi_light()
        return executor_.process_pending()

    assert run(False, tmp_path / "garde") == []
    assert run(True, tmp_path / "mutant") == ["QUEUED"]


# -- formulaire Settings ------------------------------------------------------------------------------------------

def test_settings_save_the_csi_light(monkeypatch, tmp_path):
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
    assert app.toggle(key="csi_light_enabled_toggle").value is False                     # désactivé par défaut
    assert app.selectbox(key="csi_light_red_select").value == "AUTO"
    assert app.selectbox(key="csi_light_unavailable_select").value == "NONE"
    assert any("aucun gain démontré" in c.value for c in app.caption)
    app.toggle(key="csi_light_enabled_toggle").set_value(True)
    app.selectbox(key="csi_light_red_select").set_value("ALL")
    app.selectbox(key="csi_light_orange_select").set_value("REDUCE")
    app.number_input(key="csi_light_kept_input").set_value(40.0)
    app.selectbox(key="csi_light_unavailable_select").set_value("CAUTION")
    next(b for b in app.button if b.label == "Enregistrer le feu CSI").click().run()
    assert not app.exception
    saved = store.load()
    assert saved["csi_light_enabled"] is True and saved["csi_light_red_action"] == "ALL"
    assert saved["csi_light_orange_kept_percent"] == 40.0 and saved["csi_light_when_unavailable"] == "CAUTION"
    assert cl.LightPolicy.from_mapping(saved) == cl.LightPolicy(True, "ALL", "REDUCE", 40.0, "CAUTION")


# -- relecture du 2026-10-08 : écritures, NaN, réponses mal formées, délai de grâce, redémarrage -------------------

class CountingStore:
    """Fichier d'état en mémoire qui compte les écritures ; `fail` simule un disque plein."""

    def __init__(self, fail=False):
        self.data, self.writes, self.fail = None, 0, fail

    def read(self, path):
        return self.data

    def write(self, path, payload):
        self.writes += 1
        if self.fail:
            raise OSError("disque plein")
        import json
        self.data = json.loads(json.dumps(payload, allow_nan=False))                 # comme atomic_write_json


def counted(store, client, preferences, clock):
    return cl.CsiLightGuard(client, lambda: preferences, path=Path("csi_light.json"), clock=lambda: clock[0],
                            read=store.read, write=store.write)


def test_ten_disabled_checks_write_nothing():
    store, clock = CountingStore(), [1_000_000.0]
    light = counted(store, FakeCsi(reading("ROUGE")), {}, clock)
    for _ in range(10):
        clock[0] += 30
        assert light.check() == []
    assert store.writes == 0
    store.data = {"enabled": False, "color": cl.DISABLED, "active": False, "red_active": False}   # ancien état désactivé
    again = counted(store, FakeCsi(reading("ROUGE")), {"csi_light_enabled": "false"}, clock)  # texte « false » : désactivé
    for _ in range(10):
        again.check()
    assert store.writes == 0


def test_enabled_writes_only_on_change_or_every_few_minutes():
    store, clock = CountingStore(), [1_000_000.0]
    light = counted(store, FakeCsi(reading("ORANGE")), ON, clock)
    for _ in range(20):                                                               # 20 tours de 5 s
        light.check()
        clock[0] += 5
    assert store.writes == 1
    clock[0] += cl.REFRESH_SECONDS
    light.check()
    assert store.writes == 2 and store.data["checked_at"] == clock[0]


@pytest.mark.parametrize("answer", [
    {"color": "ROUGE", "explanation": "Feu ROUGE", "computed_at": float("nan")},     # NaN : refusé par allow_nan=False
    {"color": "ROUGE", "explanation": float("inf"), "computed_at": {"x": float("nan")}},
])
def test_nan_in_the_answer_never_breaks_the_state(tmp_path, answer):
    light, _ = guard(tmp_path, FakeCsi(answer), ON)                                  # vraie écriture JSON atomique
    assert [kind for kind, _ in light.check()] == ["STARTED"] and light.effect().block_auto
    saved = cl.active_status(tmp_path / "csi_light.json", now=light.clock())
    assert saved is not None and saved["csi_computed_at"] == "" and saved["red_active"] is True


@pytest.mark.parametrize("answer", [["ROUGE"], "ROUGE", None, {"color": 5}, {"colour": "ROUGE"}, {"color": ["ROUGE"]}])
def test_malformed_answers_are_never_read_as_red(tmp_path, answer):
    light, _ = guard(tmp_path, FakeCsi(answer), ON)
    assert light.check() == []
    assert light.effect().color in (cl.UNKNOWN, cl.UNREACHABLE) and not light.effect().active


def test_a_failed_write_never_freezes_the_guard(tmp_path, events, caplog):  # noqa: F811
    store, clock = CountingStore(fail=True), [1_000_000.0]
    worker, sent = light_worker(tmp_path, events, ON, [reading("ROUGE")])
    worker.csi_light = counted(store, FakeCsi(reading("ROUGE"), reading("VERT")), ON, clock)
    with caplog.at_level("WARNING", logger="bsm.csi_light"):
        worker._check_csi_light()
        assert worker.auto_signal_executor.csi_light_reason and [k for k, _ in sent] == ["STARTED"]
        clock[0] += 60
        worker._check_csi_light()                                                     # pas de second « début »
        clock[0] += cl.CACHE_SECONDS
        worker._check_csi_light()                                                     # VERT : levé malgré le disque
    assert worker.auto_signal_executor.csi_light_reason == "" and [k for k, _ in sent] == ["STARTED", "ENDED"]
    assert store.writes >= 2 and sum("non enregistré" in r.message for r in caplog.records) == 1   # journalisé une fois


def test_the_grace_period_is_fifteen_minutes_and_survives_a_restart(tmp_path):
    clock = [1_000_000.0]
    light, _ = guard(tmp_path, FakeCsi(reading("ROUGE"), CsiUnavailable("CSI injoignable")), ON, clock)
    t0 = clock[0]
    light.check()
    clock[0] = t0 + 10 * 60
    restarted, _ = guard(tmp_path, FakeCsi(CsiUnavailable("CSI injoignable")), ON, clock)   # redémarrage, CSI coupé
    assert restarted.check() == [] and restarted.effect().block_auto                  # lecture reprise du fichier
    clock[0] = t0 + cl.STALE_SECONDS
    assert restarted.check() == [] and restarted.effect().block_auto                   # 15 min pile : encore valable
    clock[0] = t0 + cl.STALE_SECONDS + 1
    assert [kind for kind, _ in restarted.check()] == ["ENDED"]                        # au-delà : injoignable
    assert restarted.effect().color == cl.UNREACHABLE


def test_string_switches_are_read_correctly():
    assert cl.LightPolicy.from_mapping({"csi_light_enabled": "false"}).enabled is False
    assert cl.LightPolicy.from_mapping({"csi_light_enabled": "0"}).enabled is False
    assert cl.LightPolicy.from_mapping({"csi_light_enabled": "true"}).enabled is True
    assert cl.LightPolicy.from_mapping({"csi_light_enabled": 1}).enabled is False


def test_losing_trader_and_orange_light_keep_a_quarter(tmp_path, rules):  # noqa: F811
    from test_journal_telegram_corrections import losing_position
    from test_signal_auto_execution import positions_stub
    from test_suivi_canaux_protection import labelled

    losers = [labelled(losing_position(rules), "Canal A") for _ in range(10)]
    preferences = enabled_preferences(signal_channel_review_enabled=True, signal_channel_review_min_trades=10,
                                      signal_channel_action="REDUCE", signal_channel_kept_percent=50)
    inbox = SignalInbox(tmp_path / "signals.db")
    row = inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995,
                        origin="Canal A")
    worker, commands = executor(tmp_path, inbox, preferences, positions=positions_stub(losers))
    worker.csi_light_kept_percent, worker.csi_light_detail = 50.0, "Feu CSI ORANGE"
    assert worker.process_pending() == ["QUEUED"]
    metrics = commands.get_by_request_key("demo", f"signal:{row['id']}")["payload"]["route"]["metrics"]
    assert metrics["budget"] == pytest.approx(22.5)                                   # 90 × 50 % × 50 % = 25 %
    assert metrics["channel_reduction"]["budget"] == 45.0 and metrics["csi_light_reduction"]["budget_before"] == 45.0


def test_a_size_cut_below_min_notional_goes_to_review(tmp_path):
    inbox = SignalInbox(tmp_path / "signals.db")
    inbox.receive("demo", SIMPLE, source="telegram", external_id=telegram_id(1), source_timestamp=995)
    worker, commands = executor(tmp_path, inbox, enabled_preferences(signal_fixed_budget=40))
    worker.csi_light_kept_percent, worker.csi_light_detail = 10.0, "Feu CSI ORANGE"   # 40 → 4 USDT < minNotional 5
    assert worker.process_pending() == ["REVIEW"] and commands.list_recent("demo") == []
    assert '"D_PLAN"' in inbox.recent("demo")[0]["route"]
