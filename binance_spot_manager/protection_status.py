"""Alertes locales de protection : aucune requete Binance ni mutation."""

from .models import Position, SLStatus, SyncStatus


def protection_alerts(positions: list[Position]) -> list[dict]:
    alerts = []
    for position in positions:
        if not position.is_open or position.metrics.net_qty <= 0:
            continue
        issues = []
        critical = False
        oco = position.oco_exit
        if oco is not None:
            if oco.status in {"FAILED", "ERROR", "PARTIAL_TERMINAL"}:
                issues.append(f"OCO {oco.status} : protection complète non confirmée")
                critical = True
            elif oco.status == "PARTIAL":
                issues.append("OCO partiellement exécuté : reliquat à contrôler")
                critical = True
            elif oco.status != "ACTIVE":
                issues.append(f"État OCO {oco.status} avec quantité restante : à contrôler")
            if oco.missing_branch_alerted:
                issues.append("Une branche OCO n'a pas été retrouvée lors du dernier contrôle")
                critical = True
        elif position.stop_loss.status is not SLStatus.ACTIVE:
            issues.append("Aucun SL actif enregistré — cela peut être volontaire pour un TP seul")
        elif not position.stop_loss.order_id and not position.stop_loss.client_order_id:
            issues.append("SL déclaré actif mais aucun identifiant Binance enregistré")
            critical = True
        if position.sync_status not in {SyncStatus.SYNCED, SyncStatus.RECONCILED}:
            issues.append(f"Synchronisation à vérifier : {position.sync_status.value}")
        if position.automation.paused and oco is None:
            issues.append("Automatisation locale en pause")
        if issues:
            alerts.append({
                "position_id": position.position_id, "symbol": position.symbol,
                "severity": "CRITICAL" if critical else "WARNING",
                "message": " ; ".join(issues),
            })
    return alerts
