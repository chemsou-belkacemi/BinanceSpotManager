"""Controle ponctuel des sorties : lectures Binance uniquement, sans persistance."""

from __future__ import annotations

import math

from .config import ALLOWED_DEMO_BASE_URLS, Settings
from .models import Position, utcnow
from .symbol_rules import SymbolRulesCache


def inspect_exits(settings: Settings, client, positions: list[Position]) -> dict:
    if not settings.is_demo or settings.base_url not in ALLOWED_DEMO_BASE_URLS:
        raise ValueError("Controle disponible uniquement sur les hotes Binance Demo autorises.")
    if settings.dry_run or not settings.has_credentials:
        raise ValueError("Le controle des ordres reels exige le mode Demo et des cles API.")

    rows = []
    rules_cache = SymbolRulesCache(client)
    for position in positions:
        oco = position.oco_exit
        if oco is not None:
            targets = [
                ("TP OCO", oco.tp_order_id, None, oco.status, oco.quantity, oco.order_list_id),
                ("SL OCO", oco.sl_order_id, None, oco.status, oco.quantity, oco.order_list_id),
            ]
        else:
            targets = [
                (f"TP {tp.sequence_number}", tp.order_id, tp.client_order_id,
                 tp.status.value, tp.estimated_qty, None)
                for tp in position.take_profits if not tp.is_done
            ]
            sl = position.stop_loss
            if sl.status.value in {"ACTIVE", "REPLACING", "FAILED"}:
                targets.append(("SL", sl.order_id, sl.client_order_id,
                                sl.status.value, sl.quantity, None))

        for name, order_id, client_id, local_status, quantity, list_id in targets:
            row = {
                "Position": position.position_id, "Paire": position.symbol,
                "Sortie": name, "Etat local": local_status, "Ordre": order_id,
                "Etat Binance": "—", "Quantite locale": quantity,
                "Quantite Binance": None, "Execute": None,
                "Prix limite": None, "Prix stop": None,
                "Resultat": "INFO", "Detail": "Sortie locale : aucun ordre Binance reference.",
            }
            rows.append(row)
            if order_id is None and not client_id:
                if local_status in {"ACTIVE", "SUBMITTED", "REPLACING", "PARTIAL"}:
                    row.update(Resultat="ECART", Detail="Identifiant Binance manquant pour une sortie suivie.")
                continue
            try:
                order = client.get_order(position.symbol, order_id=order_id, client_order_id=client_id if order_id is None else None)
                status = order["status"]
                actual_qty = float(order["origQty"])
                row.update({
                    "Ordre": order["orderId"], "Etat Binance": status,
                    "Quantite Binance": actual_qty, "Execute": float(order["executedQty"]),
                    "Prix limite": float(order.get("price", 0)),
                    "Prix stop": float(order.get("stopPrice", 0)),
                })
                issues = []
                if not all(math.isfinite(value) for value in (
                    actual_qty, row["Execute"], row["Prix limite"], row["Prix stop"],
                )):
                    issues.append("Valeurs numeriques Binance invalides")
                if row["Execute"] != 0:
                    issues.append("Execution a rapprocher avant de confirmer la protection")
                if name.startswith("SL"):
                    expected_stop = position.stop_loss.resolved_price
                    if order.get("type") != "STOP_LOSS_LIMIT" or not (row["Prix stop"] > row["Prix limite"] > 0):
                        issues.append("Type ou prix de protection SL inattendu")
                    if not expected_stop or not math.isclose(row["Prix stop"], expected_stop, rel_tol=0, abs_tol=1e-8):
                        issues.append("Niveau SL different ou non defini localement")
                    if expected_stop:
                        expected_limit = float(rules_cache.get(position.symbol).round_price(
                            expected_stop * (1 - position.stop_loss.limit_offset_percent / 100), mode="down",
                        ))
                        if not math.isclose(row["Prix limite"], expected_limit, rel_tol=0, abs_tol=1e-8):
                            issues.append("Prix limite du SL different")
                else:
                    tp = next((t for t in position.take_profits if t.order_id == order_id or client_id and t.client_order_id == client_id), None)
                    if oco and position.take_profits:
                        tp = position.take_profits[0]
                    if order.get("type") not in {"LIMIT", "LIMIT_MAKER"}:
                        issues.append("Type de TP limite inattendu")
                    if tp is None or not tp.target_price or not math.isclose(row["Prix limite"], tp.target_price, rel_tol=0, abs_tol=1e-8):
                        issues.append("Niveau TP different ou non defini localement")
                if local_status not in {"ACTIVE", "SUBMITTED"}:
                    issues.append(f"Etat local {local_status} : controle requis")
                if order.get("symbol") != position.symbol or order.get("side") != "SELL":
                    issues.append("Paire ou sens de l'ordre inattendu")
                if order_id is not None and order["orderId"] != order_id:
                    issues.append("Identifiant d'ordre different")
                if client_id and order.get("clientOrderId") != client_id:
                    issues.append("Identifiant client different")
                if list_id is not None and order.get("orderListId") != list_id:
                    issues.append("Liste OCO differente")
                if quantity <= 0:
                    issues.append("Quantite locale non definie")
                elif not math.isclose(actual_qty, quantity, rel_tol=0, abs_tol=1e-12):
                    issues.append("Quantite differente")
                if status != "NEW":
                    issues.append(f"Ordre {status} : verifier sa prise en compte locale")
                if issues:
                    row.update(Resultat="ECART", Detail=" ; ".join(issues))
                else:
                    row.update(Resultat="OK", Detail="Ordre ouvert : identifiants, quantite, type et niveau TP/SL coherents.")
            except Exception as exc:  # noqa: BLE001 - poursuivre les autres controles
                row.update(Resultat="INVERIFIABLE", Detail=str(exc))
    return {"checked_at": utcnow().isoformat(), "rows": rows}
