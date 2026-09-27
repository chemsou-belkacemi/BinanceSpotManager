"""Verification de connexion Binance Demo — script non destructif.

Usage :
    python scripts/check_connection.py
    python scripts/check_connection.py --symbol BTCUSDT

Ne cree aucun ordre. Affiche : URL, mode, whitelist, ping, offset serveur,
compte, solde quote, existence de la paire, regles de filtre.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.binance_client import BinanceError, BinanceSpotClient  # noqa: E402
from binance_spot_manager.config import ALLOWED_DEMO_BASE_URLS, get_settings  # noqa: E402
from binance_spot_manager.symbol_rules import SymbolRulesCache, SymbolRulesError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verification Binance Demo")
    parser.add_argument("--symbol", default="BTCUSDT", help="Paire a tester")
    parser.add_argument("--json", action="store_true", help="Sortie JSON brute")
    args = parser.parse_args()

    settings = get_settings()
    client = BinanceSpotClient(settings)

    print("=" * 62)
    print("BinanceSpotManager — verification de connexion")
    print("=" * 62)
    print(f"Mode          : {settings.mode_label}")
    print(f"URL de base   : {settings.base_url}")
    print(f"URL autorisee : {'OUI' if settings.base_url in ALLOWED_DEMO_BASE_URLS else 'NON'}")
    print(f"Cles API      : {'presentes' if settings.has_credentials else 'ABSENTES'}")
    print("-" * 62)

    report = client.connectivity_report()

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Ping          : {'OK' if report['ping_ok'] else 'ECHEC'}")
        if report["server_time_offset_ms"] is not None:
            print(f"Offset serveur: {report['server_time_offset_ms']} ms")
        print(f"Compte        : {'OK' if report['account_ok'] else 'non verifie'}")
        if report["can_trade"] is not None:
            print(f"canTrade      : {report['can_trade']}")
        if report["quote_free"] is not None:
            print(f"{settings.quote_asset} libre   : {report['quote_free']}")
        for error in report["errors"]:
            print(f"  ! {error}")

    # Verifie la paire demandee avec ses filtres.
    print("-" * 62)
    try:
        rules = SymbolRulesCache(client).get(args.symbol, refresh=True)
    except SymbolRulesError as exc:
        print(f"{args.symbol} : paire inexistante ({exc})")
        return 1
    except BinanceError as exc:
        print(f"{args.symbol} : erreur Binance ({exc})")
        return 1

    print(f"Paire         : {rules.symbol} {'OK' if rules.is_trading else '(non TRADING)'}")
    print(f"Base / Quote  : {rules.base_asset} / {rules.quote_asset}")
    print(f"tickSize      : {rules.tick_size}")
    print(f"stepSize      : {rules.step_size}")
    print(f"minQty        : {rules.min_qty}")
    print(f"minNotional   : {rules.min_notional}")
    print(f"maxNumOrders  : {rules.max_num_orders}")
    print(f"maxNumAlgo    : {rules.max_num_algo_orders}")

    try:
        price = client.get_price(rules.symbol)
        print(f"Prix actuel   : {price} {rules.quote_asset}")
        print(f"Montant min   : {rules.min_quote_for_order(price)} {rules.quote_asset}")
    except BinanceError as exc:
        print(f"Prix indisponible : {exc}")

    print("=" * 62)
    return 0 if report["ping_ok"] and report["url_whitelisted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
