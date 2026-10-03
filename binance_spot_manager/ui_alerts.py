"""Sélection des alertes affichées sur toutes les pages (sans canal distant)."""

from __future__ import annotations

from typing import Any


ALERT_EVENTS = frozenset({
    "TP_EXECUTED", "SL_MOVED", "SL_EXECUTED", "POSITION_FINISHED",
    "DESYNC_DETECTED", "ERROR", "WORKER_STOPPED",
    "FEE_TOKEN_LOW",
    # Signal « À confirmer » : le propriétaire doit agir (SIGNAL_AUTO_REJECTED reste hors
    # des alertes pour ne pas noyer la boîte sous les refus de contrat).
    "SIGNAL_REVIEW_REQUIRED",
})


def unseen_alerts(
    records: list[dict[str, Any]], last_seen: str
) -> tuple[list[dict[str, Any]], str]:
    """Ne rejoue ni l'historique ni plusieurs fois le même événement."""
    if not records:
        return [], last_seen
    newest = max(str(record.get("ts") or "") for record in records)
    alerts = [
        record for record in records
        if str(record.get("ts") or "") > last_seen
        and (record.get("event") in ALERT_EVENTS or record.get("level") in {"ERROR", "CRITICAL"})
    ]
    return alerts, max(newest, last_seen)
