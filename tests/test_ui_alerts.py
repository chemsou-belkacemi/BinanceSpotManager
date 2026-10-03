"""Alertes du Dashboard : pas de rejeu historique ni de doublon."""

from binance_spot_manager.ui_alerts import unseen_alerts


def test_only_new_notifiable_events_are_selected():
    records = [
        {"ts": "2026-01-01T00:00:00.000001Z", "event": "TP_EXECUTED", "level": "INFO"},
        {"ts": "2026-01-01T00:00:01.000001Z", "event": "POSITION_UPDATED", "level": "INFO"},
        {"ts": "2026-01-01T00:00:02.000001Z", "event": "SL_MOVED", "level": "INFO"},
    ]

    selected, cursor = unseen_alerts(records, "2026-01-01T00:00:00.000001Z")
    again, _ = unseen_alerts(records, cursor)

    assert [record["event"] for record in selected] == ["SL_MOVED"]
    assert again == []



def test_signal_review_reaches_alerts_but_contract_rejections_do_not(tmp_path):
    from binance_spot_manager.alert_inbox import AlertInbox

    records = [
        {"ts": "2026-01-01T00:00:01.000001Z", "event": "SIGNAL_REVIEW_REQUIRED", "level": "WARNING",
         "message": "Signal Telegram à confirmer"},
        {"ts": "2026-01-01T00:00:02.000001Z", "event": "SIGNAL_AUTO_REJECTED", "level": "WARNING",
         "message": "Signal refusé"},
    ]
    selected, _ = unseen_alerts(records, "")
    assert [record["event"] for record in selected] == ["SIGNAL_REVIEW_REQUIRED"]
    inbox = AlertInbox(tmp_path / "alerts.db")
    for record in records:
        inbox.ingest(record)
    assert [a["record"]["event"] for a in inbox.recent()] == ["SIGNAL_REVIEW_REQUIRED"]
