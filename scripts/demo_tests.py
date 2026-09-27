"""Script d'essais Demo — non destructif par defaut.

Usage :
    python scripts/demo_tests.py              # lecture seule
    python scripts/demo_tests.py --execute    # ajoute le test order (/order/test)

Chaque etape affiche OK / ECHEC / IGNORE. Aucun ordre reel n'est cree sans
`--execute`, et meme alors on utilise l'endpoint /order/test qui n'execute rien.

Ne jamais lancer ce script automatiquement au demarrage de l'application.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from decimal import Decimal
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.binance_client import BinanceError, BinanceSpotClient  # noqa: E402
from binance_spot_manager.config import ALLOWED_DEMO_BASE_URLS, get_settings  # noqa: E402
from binance_spot_manager.symbol_rules import SymbolRulesCache, SymbolRulesError  # noqa: E402

SYMBOL = "BTCUSDT"


class Runner:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def step(self, name: str, func: Callable[[], None]) -> bool:
        try:
            func()
        except SkipTest as exc:
            self.results.append((name, "IGNORE", str(exc)))
            print(f"[IGNORE] {name} — {exc}")
            return True
        except Exception as exc:  # noqa: BLE001
            self.results.append((name, "ECHEC", str(exc)))
            print(f"[ECHEC ] {name} — {exc}")
            traceback.print_exc()
            return False
        self.results.append((name, "OK", ""))
        print(f"[OK    ] {name}")
        return True

    def summary(self) -> int:
        print("=" * 62)
        ok = sum(1 for _, status, _ in self.results if status == "OK")
        skipped = sum(1 for _, status, _ in self.results if status == "IGNORE")
        failed = sum(1 for _, status, _ in self.results if status == "ECHEC")
        print(f"Resultat : {ok} OK · {skipped} ignores · {failed} echecs")
        return 0 if failed == 0 else 1


class SkipTest(Exception):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Essais Binance Demo")
    parser.add_argument("--execute", action="store_true", help="Inclut le test order")
    parser.add_argument("--symbol", default=SYMBOL)
    args = parser.parse_args()

    settings = get_settings()
    client = BinanceSpotClient(settings)
    runner = Runner()

    print("=" * 62)
    print(f"BinanceSpotManager — essais Demo ({settings.mode_label})")
    print(f"URL : {settings.base_url}")
    print("=" * 62)

    def test_url() -> None:
        if settings.base_url not in ALLOWED_DEMO_BASE_URLS:
            raise AssertionError(
                f"SECURITE : URL hors Demo — {settings.base_url}"
            )

    def test_ping() -> None:
        client.ping()

    def test_time() -> None:
        offset = client.sync_time()
        print(f"          offset serveur : {offset} ms")

    def test_symbol() -> None:
        info = client.get_symbol_info(args.symbol)
        if info is None:
            raise AssertionError("Paire introuvable")

    def test_invalid_symbol() -> None:
        if client.get_symbol_info("BTCCUSDT") is not None:
            raise AssertionError("Une paire invalide a ete acceptee")

    def test_price() -> None:
        price = client.get_price(args.symbol)
        if price <= 0:
            raise AssertionError("Prix invalide")
        print(f"          prix          : {price}")

    def test_rules() -> None:
        rules = SymbolRulesCache(client, ttl=0).get(args.symbol, refresh=True)
        if rules.tick_size <= 0 or rules.step_size <= 0:
            raise AssertionError("Filtres incomplets")
        print(
            f"          tick/step     : {rules.tick_size} / {rules.step_size} · "
            f"minNotional {rules.min_notional}"
        )

    def require_credentials() -> None:
        if not settings.has_credentials:
            raise SkipTest("Cles API absentes (.env)")

    def test_account() -> None:
        require_credentials()
        client.get_account()

    def test_balances() -> None:
        require_credentials()
        balances = client.get_balances()
        print(f"          actifs non nuls : {len(balances)}")

    def test_open_orders() -> None:
        require_credentials()
        orders = client.get_open_orders(args.symbol)
        print(f"          ordres ouverts : {len(orders)}")

    def test_my_trades() -> None:
        require_credentials()
        trades = client.get_my_trades(args.symbol, limit=5)
        print(f"          trades recents : {len(trades)}")

    def test_order_endpoint() -> None:
        require_credentials()
        if not args.execute:
            raise SkipTest("Utiliser --execute pour inclure le test order")

        rules = SymbolRulesCache(client, ttl=0).get(args.symbol, refresh=True)
        price = client.get_price(args.symbol)

        # Montant respectant a la fois minQty ET minNotional. min_quote_for_order
        # renvoie le plus contraignant des deux (5 USDT pour BTCUSDT), et on
        # ajoute 5 % de marge pour absorber l'arrondi vers le bas au stepSize.
        min_quote = rules.min_quote_for_order(price)
        qty = rules.qty_for_quote(min_quote * Decimal("1.05"), price)
        qty = max(qty, rules.round_qty(rules.min_qty))

        notion = qty * Decimal(str(price))
        print(f"          quantite      : {rules.qty_str(qty)} (~{notion:.2f} USDT)")

        if rules.check_notional(price, qty):
            raise AssertionError(
                "La quantite de test reste sous minNotional — verifier min_quote_for_order"
            )

        # Ordre 50 % sous le marche, jamais executoire, sur /order/test :
        # aucune execution, aucun ordre cree.
        client.create_order(
            symbol=args.symbol,
            side="BUY",
            order_type="LIMIT",
            quantity=rules.qty_str(qty),
            price=rules.price_str(price * 0.5),
            time_in_force="GTC",
            client_order_id="BSM-D-DEMOTEST",
            test=True,
        )

    steps = [
        ("1. URL dans le perimetre Demo", test_url),
        ("2. Ping Binance", test_ping),
        ("3. Server time", test_time),
        ("4. Symbole valide", test_symbol),
        ("5. Symbole invalide refuse", test_invalid_symbol),
        ("6. Prix courant", test_price),
        ("7. Filtres du symbole", test_rules),
        ("8. Compte lisible", test_account),
        ("9. Balances lisibles", test_balances),
        ("10. Ordres ouverts lisibles", test_open_orders),
        ("11. Historique des trades", test_my_trades),
        ("12. Test order (optionnel)", test_order_endpoint),
    ]

    for name, func in steps:
        if not runner.step(name, func):
            # Une etape non optionnelle qui echoue peut invalider la suite.
            if name.startswith(("1.", "2.")):
                break

    return runner.summary()


if __name__ == "__main__":
    raise SystemExit(main())
