"""Service de surveillance (« bot muet ») : alerte si le worker se tait ou s'arrête, avec les positions dont le stop
n'est pas posé chez Binance ; retour annoncé ; veille avec positions ouvertes ; signal externe seulement quand le
worker vit. Rien n'est écrit, rien n'est passé chez Binance."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from binance_spot_manager.models import BotRuntime, SLStatus, SLTrigger, WorkerState, utcnow
from binance_spot_manager.notification_engine import NotificationEngine
from binance_spot_manager.watchdog import Watchdog, exposure, stop_on_binance
from test_automation import make_position, rules, settings  # noqa: F401 - fixtures partagees

NOW = 1_000_000.0


def runtime(age_seconds=5.0, state=WorkerState.MONITORING):
    return BotRuntime(state=state, heartbeat_at=utcnow() - timedelta(seconds=age_seconds))


def candle(position):
    position.stop_loss.trigger = SLTrigger.CANDLE_CLOSE
    position.stop_loss.order_id = None
    return position


class Box:
    def __init__(self, tmp_path, positions=(), **options):
        self.clock = [NOW]
        self.state = runtime()
        self.sent = []
        self.pings = []
        self.flag = tmp_path / "bot_stop.flag"
        self.positions = list(positions)
        self.watchdog = Watchdog(lambda: self.state, lambda: self.positions, self.sent.append,
                                 SimpleNamespace(worker_silent=lambda minutes, **k: ("silent", minutes, k["stopped"],
                                                                                     k["exposure"]),
                                                 worker_back=lambda minutes: ("back", minutes),
                                                 worker_standby=lambda held: ("standby", held)),
                                 standby_flag=self.flag, clock=lambda: self.clock[0],
                                 ping=lambda url: self.pings.append((url, self.clock[0] - NOW)),
                                 **options)

    def at(self, seconds, state=None):
        self.clock[0] = NOW + seconds
        if state is not None:
            self.state = state
        return self.watchdog.check()


def test_a_silent_worker_is_announced_with_unprotected_positions_then_its_return(tmp_path, rules):  # noqa: F811
    on_binance, by_candle = make_position(rules), candle(make_position(rules))
    box = Box(tmp_path, [on_binance, by_candle], ping_url="https://hc.example/ping")
    assert box.at(0) == [] and box.at(60, runtime(age_seconds=200)) == []          # démarrage : délai de grâce
    assert box.at(130, runtime(age_seconds=330)) == ["SILENT"]
    kind, minutes, stopped, held = box.sent[-1]
    assert kind == "silent" and minutes == 5 and not stopped
    assert held.open_positions == 2 and held.unprotected == ["BTCUSDT"]            # seule la position « bougie »
    assert box.at(600, runtime(age_seconds=800)) == []                             # pas de rafale
    assert box.at(130 + 1800, runtime(age_seconds=2100)) == ["SILENT"]              # rappel toutes les 30 min
    assert box.at(2000, runtime(age_seconds=3)) == ["BACK"]
    assert box.sent[-1][0] == "back" and box.sent[-1][1] >= 30
    assert box.pings == [("https://hc.example/ping", 0), ("https://hc.example/ping", 2000)]   # jamais en silence


def test_a_stopped_worker_is_announced_as_stopped(tmp_path, rules):  # noqa: F811
    box = Box(tmp_path, [make_position(rules)])
    box.at(0)
    stopped = runtime(age_seconds=1, state=WorkerState.STOPPED)
    assert box.at(200, stopped) == [] and box.at(230, stopped) == []                 # arrêt propre : délai
    assert box.at(380, stopped) == []                                                  # < 3 min : sauvegarde, redémarrage
    assert box.at(420, stopped) == ["SILENT"]
    assert box.sent[-1][2] is True


def test_a_short_planned_stop_is_quiet_but_a_frozen_heartbeat_is_not(tmp_path):
    box = Box(tmp_path)
    box.at(0)
    stopped = runtime(age_seconds=1, state=WorkerState.STOPPED)
    for second in (200, 230, 260, 290):                                               # 1 min 30 d'arrêt propre
        assert box.at(second, stopped) == []
    assert box.at(320, runtime()) == [] and box.sent == []                            # revenu : rien à dire
    assert box.at(500, runtime(age_seconds=300)) == []                                 # plantage : 1er contrôle
    assert box.at(530, runtime(age_seconds=330)) == ["SILENT"]                         # pas de délai d'arrêt propre


def test_a_worker_in_error_that_keeps_beating_is_alive(tmp_path):
    box = Box(tmp_path)
    box.at(0)
    for second in range(30, 600, 30):                                                  # erreur un tour sur deux
        state = WorkerState.ERROR if second % 60 else WorkerState.MONITORING
        assert box.at(second, runtime(age_seconds=3, state=state)) == []
    assert box.sent == []


def test_one_slow_check_is_not_an_alert_and_the_limit_follows_the_settings(tmp_path):
    box = Box(tmp_path, stale=lambda: 905.0)                                         # cadence 300 s : 3 × 300 + 5
    box.at(0)
    assert box.at(1000, runtime(age_seconds=600)) == [] and box.at(1030, runtime(age_seconds=630)) == []
    assert box.at(1060, runtime(age_seconds=1000)) == []                               # un seul contrôle trop vieux
    assert box.at(1090, runtime(age_seconds=1030)) == ["SILENT"]
    broken = Box(tmp_path, stale=lambda: (_ for _ in ()).throw(OSError("réglages illisibles")))
    assert broken.watchdog.limit() == 120


def test_standby_with_open_positions_is_announced_and_reminded(tmp_path, rules):  # noqa: F811
    box = Box(tmp_path, [candle(make_position(rules))])
    box.flag.write_text("arret demande")
    paused = runtime(state=WorkerState.PAUSED)
    assert box.at(0, paused) == ["STANDBY"] and box.sent[-1][1].unprotected == ["BTCUSDT"]
    assert box.at(3600, paused) == []
    assert box.at(6 * 3600 + 1, paused) == ["STANDBY"]
    box.flag.unlink()
    assert box.at(7 * 3600, runtime()) == []
    box.positions = []
    box.flag.write_text("arret demande")
    assert box.at(8 * 3600, paused) == []                                           # rien d'ouvert : rien à dire


def test_no_external_ping_without_url_and_ping_failures_are_harmless(tmp_path):
    box = Box(tmp_path)
    assert box.at(0) == [] and box.pings == []

    def broken(url):
        raise OSError("réseau")

    failing = Box(tmp_path, ping_url="https://hc.example/ping")
    failing.watchdog.ping = broken
    assert failing.at(0) == []                                                      # pas d'exception


def test_exposure_counts_only_held_positions_and_binance_stops(rules):  # noqa: F811
    held = make_position(rules)
    assert stop_on_binance(held)
    no_order = make_position(rules)
    no_order.stop_loss.order_id = None
    failed = make_position(rules)
    failed.stop_loss.status = SLStatus.FAILED
    empty = make_position(rules)
    empty.metrics.net_qty = 0.0
    result = exposure([held, no_order, failed, empty])
    assert result.open_positions == 3 and result.unprotected == ["BTCUSDT", "BTCUSDT"]


def test_watchdog_messages_say_what_is_unprotected(settings, rules):  # noqa: F811
    from binance_spot_manager.watchdog import Exposure

    engine = NotificationEngine(settings)
    silent = engine.worker_silent(12, stopped=False, last_heartbeat=utcnow(),
                                  exposure=Exposure(open_positions=3, unprotected=["FETUSDT", "ZKPUSDT"]))
    assert silent.event == "WORKER_OFFLINE" and silent.level == "CRITICAL" and "12 min" in silent.title
    assert "Stops NON poses chez Binance : 2 (FETUSDT, ZKPUSDT)" in silent.body
    safe = engine.worker_silent(3, stopped=True, last_heartbeat=None, exposure=Exposure(open_positions=1))
    assert safe.title == "Worker arrete" and "restent actifs sans le worker" in safe.body
    standby = engine.worker_standby(Exposure(open_positions=2, unprotected=["FETUSDT"]))
    assert standby.level == "WARNING" and "Rien n'est suivi en veille" in standby.body
    assert engine.worker_back(7).title == "Worker revenu"


def test_the_service_script_only_reads(tmp_path, monkeypatch, rules):  # noqa: F811
    """Le script lit le heartbeat et les positions sans créer ni écrire aucun fichier."""
    from scripts import watchdog as service
    from binance_spot_manager.position_store import atomic_write_json

    data = tmp_path / "data"
    (data / "positions").mkdir(parents=True)
    position = make_position(rules)
    atomic_write_json(data / "positions" / f"{position.position_id}.json", position.model_dump(mode="json"))
    atomic_write_json(data / "bot_runtime.json", runtime().model_dump(mode="json"))
    monkeypatch.setattr(service, "BOT_RUNTIME_FILE", data / "bot_runtime.json")
    monkeypatch.setattr(service, "POSITIONS_DIR", data / "positions")
    before = sorted(p.name for p in data.rglob("*"))
    assert service.read_runtime().state is WorkerState.MONITORING
    assert [p.position_id for p in service.read_positions()] == [position.position_id]
    assert sorted(p.name for p in data.rglob("*")) == before
    monkeypatch.setattr(service, "BOT_RUNTIME_FILE", data / "absent.json")
    assert service.read_runtime().heartbeat_at is None
