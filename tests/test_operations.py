"""Comptabilite, protection, alertes et archives sans effet sur Binance."""

from datetime import timedelta
import io
import json
import zipfile

import pytest

from binance_spot_manager.accounting import accounting_snapshot
from binance_spot_manager.alert_inbox import AlertInbox
from binance_spot_manager.backup_manager import build_backup, verify_backup
from binance_spot_manager.command_store import CommandStore
from binance_spot_manager.models import Position, PositionStatus, Entry, EntryStatus, TakeProfit, Commission, SLStatus, utcnow
from binance_spot_manager.position_store import PositionStore
from binance_spot_manager.protection_status import protection_overview


def test_cost_basis_includes_buy_and_sell_fees_once():
    p = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    p.entries = [Entry(executed_qty=1, average_fill_price=100, quote_spent=100,
                       commissions=[Commission(asset="USDT", amount=1)])]
    p.take_profits = [TakeProfit(executed_qty=0.5, quote_received=60, commissions=[Commission(asset="USDT", amount=0.6)])]
    p.metrics.current_price = 120
    snapshot = accounting_snapshot(p)
    assert snapshot["Realise"] == pytest.approx(8.9)
    assert snapshot["Non realise"] == pytest.approx(9.5)
    assert snapshot["Total"] == pytest.approx(18.4)
    assert snapshot == accounting_snapshot(p)
    p.entries[0].commissions.append(Commission(asset="BNB", amount=0.001))
    snapshot = accounting_snapshot(p)
    assert not snapshot["Complet"]
    assert snapshot["Frais non convertis"] == {"BNB": 0.001}


def test_base_fees_reduce_inventory_and_increase_cost():
    p = Position(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT")
    p.entries = [Entry(executed_qty=1, quote_spent=100, commissions=[Commission(asset="BTC", amount=0.01)])]
    p.take_profits = [TakeProfit(executed_qty=0.49, quote_received=60, commissions=[Commission(asset="BTC", amount=0.01)])]
    assert accounting_snapshot(p)["Quantite restante"] == pytest.approx(0.49)
    assert accounting_snapshot(p)["Cout unitaire frais inclus"] == pytest.approx(100 / 0.99)


def test_persistent_alert_delivery_ack_and_recurrence(tmp_path):
    inbox = AlertInbox(tmp_path / "alerts.db")
    record = {"event": "ERROR", "level": "CRITICAL", "message": "Protection manquante", "ts": "2026-01-01T00:00:00Z"}
    assert inbox.claim_delivery(record)
    assert not AlertInbox(inbox.path).claim_delivery(record)
    alert = inbox.recent()[0]
    assert alert["occurrences"] == 1
    inbox.acknowledge(alert["id"])
    assert not inbox.recent(unread=True)
    record["ts"] = "2026-01-01T00:00:01Z"
    assert inbox.claim_delivery(record)
    assert inbox.recent()[0]["occurrences"] == 2
    assert len(inbox.recent(unread=True)) == 1


def test_bulk_actions_cover_alerts_beyond_visible_page(tmp_path):
    inbox = AlertInbox(tmp_path / "alerts.db")
    for i in range(105):
        inbox.ingest({"event": "ERROR", "level": "ERROR", "message": f"Erreur {i}", "ts": "2026-01-01T00:00:00Z"})
    assert len(inbox.recent()) == 100
    assert inbox.counts() == {"total": 105, "unread": 105, "read": 0}
    assert inbox.acknowledge_all() == 105
    assert inbox.recent(unread=True) == []
    assert inbox.counts() == {"total": 105, "unread": 0, "read": 105}
    assert inbox.acknowledge_all() == 0
    assert inbox.delete_all() == 105
    assert AlertInbox(inbox.path).recent() == []
    assert inbox.delete_all() == 0
    assert inbox.counts() == {"total": 0, "unread": 0, "read": 0}


def test_alert_filter_applies_before_page_limit(tmp_path):
    inbox = AlertInbox(tmp_path / "alerts.db")
    old = {"event": "ERROR", "level": "ERROR", "message": "Ancienne non lue", "ts": "2026-01-01T00:00:00Z"}
    inbox.ingest(old)
    for i in range(101):
        newer = dict(old, message=f"Lue {i}", ts="2026-01-02T00:00:00Z")
        inbox.ingest(newer)
        inbox.acknowledge(inbox.key(newer))
    assert inbox.key(old) not in {a["id"] for a in inbox.recent()}
    assert [a["id"] for a in inbox.recent(unread=True)] == [inbox.key(old)]
    assert len(inbox.recent(read=True)) == 100
    assert inbox.counts() == {"total": 102, "unread": 1, "read": 101}


@pytest.mark.parametrize("delete_all", [False, True])
def test_deleted_alert_is_not_replayed_but_new_occurrence_is_delivered(tmp_path, delete_all):
    inbox = AlertInbox(tmp_path / "alerts.db")
    record = {"event": "ERROR", "level": "ERROR", "message": "Protection absente", "ts": "2026-01-01T00:00:00Z"}
    other = dict(record, message="Autre alerte")
    inbox.ingest(record)
    inbox.ingest(other)
    if delete_all:
        assert inbox.delete_all() == 2
    else:
        assert inbox.delete(inbox.key(record)) == 1
        assert inbox.recent()[0]["record"]["message"] == "Autre alerte"
    reopened = AlertInbox(inbox.path)
    assert not reopened.claim_delivery(record)
    assert reopened.claim_delivery(dict(record, ts="2026-01-01T00:00:01Z"))


def test_mark_all_read_suppresses_pending_delivery_but_allows_recurrence(tmp_path):
    inbox = AlertInbox(tmp_path / "alerts.db")
    record = {"event": "ERROR", "level": "ERROR", "message": "Erreur", "ts": "2026-01-01T00:00:00Z"}
    inbox.ingest(record)
    inbox.acknowledge_all()
    assert not inbox.claim_delivery(record)
    assert inbox.claim_delivery(dict(record, ts="2026-01-01T00:00:01Z"))


@pytest.mark.parametrize("action", ["acknowledge", "delete"])
def test_stale_single_action_preserves_new_occurrence(tmp_path, action):
    ui = AlertInbox(tmp_path / "alerts.db")
    worker = AlertInbox(ui.path)
    record = {"event": "ERROR", "level": "CRITICAL", "message": "Protection absente", "ts": "2026-01-01T00:00:00Z"}
    worker.ingest(record)
    observed = ui.recent()[0]
    newer = dict(record, ts="2026-01-01T00:00:01Z")
    worker.ingest(newer)
    assert getattr(ui, action)(observed["id"], expected_last_seen=observed["last_seen"]) == 0
    assert ui.recent(unread=True)[0]["record"] == newer
    assert ui.claim_delivery(newer)
    assert getattr(ui, action)(observed["id"], expected_last_seen=newer["ts"]) == 1


@pytest.mark.parametrize("action", ["acknowledge_all", "delete_all"])
def test_bulk_snapshot_preserves_new_alerts_and_recurrences(tmp_path, action):
    ui = AlertInbox(tmp_path / "alerts.db")
    worker = AlertInbox(ui.path)
    record = {"event": "ERROR", "level": "ERROR", "message": "Ancienne", "ts": "2026-01-01T00:00:00Z"}
    worker.ingest(record)
    recurring = dict(record, message="Recurrente")
    worker.ingest(recurring)
    versions = ui.versions()
    worker.ingest(dict(recurring, ts="2026-01-01T00:00:01Z"))
    worker.ingest(dict(record, message="Nouvelle", ts="2026-01-01T00:00:02Z"))
    assert getattr(ui, action)(expected_versions=versions) == 1
    assert {a["record"]["message"] for a in ui.recent(unread=True)} == {"Recurrente", "Nouvelle"}
    assert getattr(ui, action)(expected_versions={}) == 0


def test_backup_excludes_env_and_checks_json_and_sqlite(tmp_path):
    # Secrets factices dans un repertoire de test, jamais le .env du projet.
    data = tmp_path / "data"
    store = PositionStore(data / "positions")
    store.save(Position(symbol="BTCUSDT"))
    (data / "private.txt").write_text("fake-secret")
    CommandStore(data / "commands.sqlite3").enqueue("test", "CLOSE_LOCAL", {}, request_key="1")
    raw = build_backup(data, worker_running=False)
    manifest = verify_backup(raw)
    assert len(manifest["files"]) == 2
    assert "private.txt" not in str(manifest)
    with pytest.raises(ValueError, match="worker"):
        build_backup(data, worker_running=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as old:
        content = {name: old.read(name) for name in old.namelist()}
    position_name = next(n for n in content if "positions/" in n)
    content[position_name] = b"{}"
    corrupt = io.BytesIO()
    with zipfile.ZipFile(corrupt, "w") as archive:
        for name, value in content.items():
            archive.writestr(name, value)
    with pytest.raises(ValueError, match="Empreinte"):
        verify_backup(corrupt.getvalue())


@pytest.mark.parametrize("name", ["../outside.json", "data/../outside.json", "data/.env", "C:/private", "data\\private"])
def test_backup_rejects_unexpected_paths(name):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr(name, "payload")
    with pytest.raises(ValueError, match="autorise"):
        verify_backup(raw.getvalue())


def test_only_fresh_binance_snapshot_can_confirm_protection():
    p = Position(symbol="BTCUSDT", status=PositionStatus.ACTIVE)
    p.metrics.net_qty = 1
    p.stop_loss.status, p.stop_loss.order_id = SLStatus.ACTIVE, 7
    now = utcnow()
    assert "requise" in protection_overview([p])[0]["Protection"]
    report = {"checked_at": now.isoformat(), "rows": [{"Position": p.position_id, "Sortie": "SL", "Resultat": "OK"}]}
    assert "verifiees" in protection_overview([p], report, now=now)[0]["Protection"]
    assert "requise" in protection_overview([p], report, now=now + timedelta(seconds=31))[0]["Protection"]


def test_operations_page_renders_without_network(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    import ui_common
    from binance_spot_manager.config import Settings
    from binance_spot_manager.dashboard_service import DashboardService
    from binance_spot_manager.event_store import EventStore
    service = DashboardService(Settings(), position_store=PositionStore(tmp_path / "positions"), events=EventStore(tmp_path / "events.jsonl"))
    service.commands = CommandStore(tmp_path / "commands.db")
    monkeypatch.setattr(ui_common, "get_service", lambda: service)
    from pathlib import Path
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=20).run()
    assert not app.exception
    app.switch_page("pages/7_Operations.py").run()
    assert not app.exception
    assert any("Demandes" in heading.value for heading in app.subheader)
    inbox = service.events.alert_inbox()
    for i in range(2):
        inbox.ingest({"event": "ERROR", "level": "ERROR", "message": f"Test {i}", "ts": "2026-01-01T00:00:00Z"})
    app.run()
    app.selectbox(key="operations_alert_filter").select("Non lues").run()
    assert not app.exception
    selected_id = app.selectbox(key="operations_alert_selection").value
    old_alert = next(a for a in inbox.recent() if a["id"] == selected_id)
    inbox.ingest(dict(old_alert["record"], ts="2026-01-01T00:00:01Z"))
    next(b for b in app.button if b.label == "Marquer cette alerte comme lue").click().run()
    assert not app.exception
    assert len(inbox.recent(unread=True)) == 2
    assert any("a évolué" in message.value for message in app.info)
    next(b for b in app.button if b.label == "Tout marquer comme lu").click().run()
    assert not app.exception
    assert not inbox.recent(unread=True)
    assert any(c.value == "Aucune alerte pour ce filtre." for c in app.caption)
    assert next(b for b in app.button if b.label == "Tout marquer comme lu").disabled
    app.selectbox(key="operations_alert_filter").select("Lues").run()
    assert not app.exception
    next(b for b in app.button if b.label == "Supprimer cette alerte").click().run()
    assert not app.exception
    assert len(inbox.recent()) == 1
    next(b for b in app.button if b.label == "Tout supprimer").click().run()
    assert not app.exception
    assert inbox.recent() == []
