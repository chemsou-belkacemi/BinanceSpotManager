"""Alertes locales de protection : aucune requete Binance ni mutation."""

from .models import Position, SLStatus, SLTrigger, SyncStatus
from datetime import datetime, timezone


def protection_overview(positions, report=None, *, now=None):
    """Un etat local n'est jamais presente comme une preuve Binance fraiche."""
    now = now or datetime.now(timezone.utc)
    fresh = False
    if report:
        try:
            age = (now - datetime.fromisoformat(report["checked_at"])).total_seconds()
            fresh = 0 <= age <= 30
        except (ValueError, TypeError, KeyError):
            pass
    rows = []
    for p in positions:
        if not p.is_open:
            continue
        checks = [r for r in (report or {}).get("rows", []) if r["Position"] == p.position_id]
        issues = protection_alerts([p])
        if p.metrics.net_qty <= 0:
            state = "En attente d'achat"
        elif p.oco_exit is None and p.stop_loss.status is SLStatus.NONE:
            state = "Sans SL volontairement"
        elif candle_stop(p) and not issues:
            state = f"SL à la clôture {p.stop_loss.candle_interval} (surveillé par le worker)" + (
                " + stop de secours chez Binance" if backup_live(p) else "")
        elif fresh and checks and all(r["Resultat"] == "OK" for r in checks) and any("SL" in r["Sortie"] for r in checks) and not issues:
            state = "Sorties verifiees sur Binance (instantane)"
        elif issues:
            state = "Protection a controler"
        else:
            state = "Verification Binance requise" if not fresh else "Protection non confirmee"
        rows.append({"Paire": p.symbol, "Position": p.position_id, "Protection": state,
                     "Automatisation": "En pause" if p.automation.paused else "Active",
                     "Detail": " ; ".join(issue["message"] for issue in issues)})
    return rows


def candle_stop(position: Position) -> bool:
    """SL a la cloture de bougie : le worker le surveille (avec ou sans stop de secours chez Binance)."""
    sl = position.stop_loss
    return sl.trigger is SLTrigger.CANDLE_CLOSE and sl.status is not SLStatus.NONE


def backup_live(position: Position) -> bool:
    """Stop de secours d'un SL a la cloture, actif chez Binance."""
    sl = position.stop_loss
    return candle_stop(position) and sl.status is SLStatus.ACTIVE and bool(sl.order_id)


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
        elif candle_stop(position):
            # Protection assuree par le worker (cloture de bougie) ; un stop de secours prevu mais absent est signale.
            if position.stop_loss.backup_percent > 0 and not backup_live(position):
                issues.append(f"Stop de secours absent chez Binance ({position.stop_loss.status.value}) : seule la "
                              "clôture surveillée par le worker protège")
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
