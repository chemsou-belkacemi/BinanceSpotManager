"""Liste des ordres ouverts reels + rapprochement avec les positions locales.

Usage :
    python scripts/check_open_orders.py
    python scripts/check_open_orders.py --symbol BTCUSDT

Lecture seule : n'annule et ne cree rien.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import get_settings  # noqa: E402
from binance_spot_manager.dashboard_service import DashboardService  # noqa: E402
from binance_spot_manager.position_store import PositionStore  # noqa: E402
from binance_spot_manager.reconciliation_engine import audit_open_orders  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ordres ouverts Binance")
    parser.add_argument("--symbol", default=None, help="Filtrer sur une paire")
    args = parser.parse_args()

    settings = get_settings()
    service = DashboardService(settings)

    print("=" * 78)
    print(f"Ordres ouverts — {settings.base_url} — {settings.mode_label}")
    print("=" * 78)

    rows, error = service.open_orders(args.symbol)
    if error:
        print(f"! {error}")

    if not rows:
        print("Aucun ordre ouvert.")
    else:
        header = f"{'Symbole':<12}{'Cote':<6}{'Type':<18}{'Prix':>12}{'Stop':>12}{'Qte':>14}{'Statut':<14}{'Origine':<12}"
        print(header)
        print("-" * len(header))
        for row in rows:
            print(
                f"{row.symbol:<12}{row.side:<6}{row.order_type:<18}"
                f"{row.price:>12.6f}{row.stop_price:>12.6f}{row.orig_qty:>14.8f}"
                f"{row.status:<14}{(row.owner or 'inconnu'):<12}"
            )

    # Ordres orphelins : presents cote Binance mais non suivis par le bot.
    store = PositionStore()
    positions = store.list_open()
    symbols = [args.symbol] if args.symbol else [p.symbol for p in positions]
    orphans_found = False

    for symbol in sorted({s for s in symbols if s}):
        position = next((p for p in positions if p.symbol == symbol), None)
        if position is None:
            continue
        all_orders, _ = service.open_orders(symbol)
        raw = [
            {
                "orderId": o.order_id,
                "clientOrderId": o.client_order_id,
                "side": o.side,
                "type": o.order_type,
                "origQty": o.orig_qty,
                "price": o.price,
            }
            for o in all_orders
        ]
        findings = audit_open_orders(position, raw, tracked_positions=positions)
        if findings:
            orphans_found = True
            print("-" * 78)
            for finding in findings:
                print(f"ORPHELIN {symbol} : {finding.message}")
                print(f"          action suggeree : {finding.suggested_action}")

    print("=" * 78)
    if not orphans_found and rows:
        print("Tous les ordres ouverts appartiennent a une position suivie.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
