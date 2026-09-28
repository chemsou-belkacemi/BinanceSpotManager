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
