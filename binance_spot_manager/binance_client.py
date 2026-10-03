"""Client Binance Spot minimal, signe HMAC-SHA256.

Principes :
- toute requete signee utilise le server time (offset calcule une fois) ;
- toute ecriture passe par `settings.assert_write_allowed()` ;
- les erreurs Binance sont journalisees SANS jamais exposer les secrets ;
- aucune valeur de filtre (tickSize, stepSize...) n'est codee en dur ici.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import time
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

import requests

from .config import DATA_DIR, ALLOWED_DEMO_BASE_URLS, SecurityError, Settings, get_settings
from .order_journal import OrderJournal

logger = logging.getLogger("bsm.binance")

#: Verbes HTTP consideres comme des ecritures (soumis a la whitelist Demo).
_WRITE_METHODS = frozenset({"POST", "PUT", "DELETE"})


class BinanceError(RuntimeError):
    """Erreur renvoyee par l'API Binance ou par le transport HTTP."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[int] = None,
        status: Optional[int] = None,
        endpoint: str = "",
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.endpoint = endpoint
        self.retry_after = retry_after

    def __str__(self) -> str:  # pragma: no cover - affichage
        parts = [self.message]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.status is not None:
            parts.append(f"http={self.status}")
        if self.endpoint:
            parts.append(f"endpoint={self.endpoint}")
        return " | ".join(parts)

    @property
    def is_unknown_order(self) -> bool:
        """-2011 / -2013 : ordre inconnu ou deja disparu cote Binance."""
        return self.code in (-2011, -2013)

    @property
    def is_duplicate_client_order_id(self) -> bool:
        """-2010 avec message de duplication : l'ordre existe deja."""
        return self.code == -2010 and "duplicate" in self.message.lower()

    @property
    def is_stop_would_trigger(self) -> bool:
        """Refus d'un ordre stop : le prix a deja franchi le niveau demande."""
        message = self.message.lower()
        return "trigger immediately" in message or "immediately trigger" in message

    @property
    def is_filter_failure(self) -> bool:
        return "filter failure" in self.message.lower()

    @property
    def is_ambiguous_write(self) -> bool:
        """La requete a pu atteindre Binance sans reponse exploitable."""
        return (
            self.code in {-1006, -1007}
            or self.status is not None and self.status >= 500
            or self.status is None and self.code is None
            and self.message.lower().startswith("echec reseau")
        )


def _clean_params(params: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Retire les None — Binance rejette les parametres vides."""
    if not params:
        return {}
    return {k: v for k, v in params.items() if v is not None}


class BinanceSpotClient:
    """Client Spot. Une instance par process suffit."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "BinanceSpotManager/2.0"})
        self._time_offset_ms: int = 0
        self._time_synced_at: float = 0.0
        self._cooldown_until: float = 0.0
        self._order_journal = OrderJournal(DATA_DIR / "order_intents.sqlite3")

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self.settings.base_url

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}{endpoint}"

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        method = method.upper()
        payload = _clean_params(params)

        if method in _WRITE_METHODS:
            # Garde-fou : refuse toute ecriture hors Demo whitelistee.
            self.settings.assert_write_allowed(f"{method} {endpoint}")
            if self.settings.dry_run:
                raise SecurityError("DRY_RUN : toute ecriture Binance est interdite")

        if signed and (not self.settings.is_demo or self.base_url not in ALLOWED_DEMO_BASE_URLS):
            raise SecurityError("Requete signee interdite hors Binance Demo")

        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            raise BinanceError(
                "Limite Binance active : attendre avant une nouvelle requete",
                status=429, endpoint=endpoint, retry_after=remaining,
            )

        intent_payload = dict(payload)
        headers: dict[str, str] = {}
        if signed:
            if not self.settings.has_credentials:
                raise BinanceError(
                    "Cles API absentes : renseigner BSM_DEMO_API_KEY / BSM_DEMO_API_SECRET dans .env",
                    endpoint=endpoint,
                )
            headers["X-MBX-APIKEY"] = self.settings.api_key
            payload["timestamp"] = self._timestamp_ms()
            payload["recvWindow"] = self.settings.recv_window
            query = urlencode(payload, doseq=True)
            payload["signature"] = self._sign(query)

        if method == "POST" and endpoint in {"/api/v3/order", "/api/v3/orderList/oco"}:
            if self._cooldown_until > time.monotonic():
                raise BinanceError("Limite Binance active apres synchronisation", status=429, endpoint=endpoint)
            client_id = intent_payload.get("newClientOrderId") or intent_payload.get("listClientOrderId")
            if not client_id:
                raise SecurityError("Identifiant stable requis avant de creer un ordre Demo")
            namespace = self._journal_namespace()
            if not self._order_journal.claim(namespace, str(intent_payload.get("symbol", "")), str(client_id), intent_payload):
                raise BinanceError(
                    "Intention deja enregistree : consulter Binance, aucun renvoi automatique",
                    code=-1007, endpoint=endpoint,
                )

        try:
            response = self._session.request(
                method,
                self._url(endpoint),
                params=payload,
                headers=headers,
                timeout=self.settings.http_timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # Les exceptions requests peuvent contenir l'URL signee complete.
            logger.error("Transport Binance KO %s %s : %s", method, endpoint, type(exc).__name__)
            raise BinanceError(f"Echec reseau : {type(exc).__name__}", endpoint=endpoint) from None

        body = self._parse(response, endpoint)
        if method == "POST" and endpoint == "/api/v3/order":
            if not isinstance(body, dict) or not body.get("orderId") or not body.get("status"):
                raise BinanceError("Reponse d'ordre incomplete : statut inconnu", code=-1006, endpoint=endpoint)
        if method == "POST" and endpoint == "/api/v3/orderList/oco":
            if not isinstance(body, dict) or "orderListId" not in body or len(body.get("orders", [])) != 2:
                raise BinanceError("Reponse OCO incomplete : statut inconnu", code=-1006, endpoint=endpoint)
        return body

    def _journal_namespace(self) -> str:
        """Compte Binance des intentions : URL + empreinte de la cle, jamais la cle elle-meme."""
        return self.base_url + ":" + hashlib.sha256(self.settings.api_key.encode()).hexdigest()

    def intent_created_at(self, symbol: str, client_order_id: str):
        """Date (UTC) d'inscription de l'intention d'ordre `client_order_id`, ou None.

        L'intention est inscrite APRES la signature de la requete : son horodatage Binance est
        anterieur ou egal a cette date.
        """
        return self._order_journal.created_at(
            self._journal_namespace(), symbol.upper(), client_order_id
        )

    def _parse(self, response: requests.Response, endpoint: str) -> Any:
        try:
            body = response.json()
        except ValueError:
            body = None

        if 300 <= response.status_code < 400:
            raise BinanceError("Redirection Binance refusee", code=-1006, endpoint=endpoint)
        if response.status_code >= 400:
            code = None
            message = "Reponse HTTP Binance en erreur"
            retry_after = None
            if response.status_code in (418, 429):
                try:
                    parsed_retry_after = float(response.headers.get("Retry-After", ""))
                    retry_after = (
                        max(parsed_retry_after, 1.0)
                        if math.isfinite(parsed_retry_after) else None
                    )
                except (TypeError, ValueError):
                    pass
                if retry_after is None:
                    retry_after = 120.0 if response.status_code == 418 else 60.0
                self._cooldown_until = max(
                    self._cooldown_until, time.monotonic() + retry_after
                )
            if isinstance(body, dict):
                code = body.get("code")
                message = str(body.get("msg") or message)
                for secret in (self.settings.api_key, self.settings.api_secret):
                    if secret:
                        message = message.replace(secret, "[REDACTED]")
            logger.error(
                "Erreur Binance %s %s code=%s msg=%s",
                response.status_code,
                endpoint,
                code,
                message,
            )
            raise BinanceError(
                message,
                code=code if isinstance(code, int) else None,
                status=response.status_code,
                endpoint=endpoint,
                retry_after=retry_after,
            )

        if body is None:
            raise BinanceError("Reponse Binance non JSON : statut inconnu", code=-1006, endpoint=endpoint)
        return body

    def _sign(self, query: str) -> str:
        return hmac.new(
            self.settings.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ------------------------------------------------------------------
    # Temps serveur
    # ------------------------------------------------------------------

    def _timestamp_ms(self) -> int:
        if time.time() - self._time_synced_at > 300:
            self.sync_time()
        return int(time.time() * 1000) + self._time_offset_ms

    def sync_time(self) -> int:
        """Aligne l'horloge locale sur le server time. Retourne l'offset ms."""
        try:
            server_ms = int(self.get_server_time()["serverTime"])
        except (BinanceError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Sync server time impossible : %s", exc)
            self._time_synced_at = time.time()
            return self._time_offset_ms
        self._time_offset_ms = server_ms - int(time.time() * 1000)
        self._time_synced_at = time.time()
        return self._time_offset_ms

    # ------------------------------------------------------------------
    # Endpoints publics
    # ------------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        return self._request("GET", "/api/v3/ping") or {}

    def get_server_time(self) -> dict[str, Any]:
        return self._request("GET", "/api/v3/time")

    def get_exchange_info(self, symbol: Optional[str] = None) -> dict[str, Any]:
        params = {"symbol": symbol.upper()} if symbol else None
        return self._request("GET", "/api/v3/exchangeInfo", params=params)

    def get_symbol_info(self, symbol: str) -> Optional[dict[str, Any]]:
        """Retourne le bloc symbole, ou None si la paire n'existe pas."""
        try:
            info = self.get_exchange_info(symbol)
        except BinanceError as exc:
            # Binance renvoie -1121 "Invalid symbol" pour une paire inconnue.
            if exc.code == -1121 or "invalid symbol" in exc.message.lower():
                return None
            raise
        symbols = info.get("symbols") or []
        return symbols[0] if symbols else None

    def get_klines(
        self, symbol: str, interval: str, *, start_time: Optional[int] = None, limit: int = 500
    ) -> list[list[Any]]:
        """Bougies publiques, la plus ancienne d'abord ; la derniere est souvent encore ouverte."""
        params = {"symbol": symbol.upper(), "interval": interval, "limit": max(1, min(int(limit), 1000))}
        if start_time is not None:
            params["startTime"] = int(start_time)
        data = self._request("GET", "/api/v3/klines", params=params)
        if not isinstance(data, list):
            raise BinanceError("Bougies Binance invalides", endpoint="/api/v3/klines")
        return data

    def get_price(self, symbol: str) -> float:
        data = self._request(
            "GET", "/api/v3/ticker/price", params={"symbol": symbol.upper()}
        )
        value = float(data["price"])
        if not math.isfinite(value) or value <= 0:
            raise BinanceError("Prix Binance invalide", endpoint="/api/v3/ticker/price")
        return value

    def get_prices(self, symbols: Optional[list[str]] = None) -> dict[str, float]:
        """Prix de plusieurs paires en un appel."""
        wanted = sorted({symbol.upper() for symbol in symbols or [] if symbol})
        if symbols is not None and not wanted:
            return {}
        if len(wanted) == 1:
            symbol = wanted[0]
            return {symbol: self.get_price(symbol)}
        params = {"symbols": json.dumps(wanted, separators=(",", ":"))} if wanted else None
        data = self._request("GET", "/api/v3/ticker/price", params=params)
        prices: dict[str, float] = {}
        for row in data or []:
            try:
                symbol, value = str(row["symbol"]), float(row["price"])
            except (KeyError, TypeError, ValueError):
                continue
            # Le catalogue Demo peut contenir des marches inactifs a prix nul.
            # Ils ne doivent pas rendre inutilisables tous les prix valides.
            if math.isfinite(value) and value > 0:
                prices[symbol] = value
        if not prices:
            raise BinanceError("Prix Binance invalide", endpoint="/api/v3/ticker/price")
        if symbols is None:
            return prices
        return {k: v for k, v in prices.items() if k in wanted}

    def get_ticker_24h(self, symbol: str) -> dict[str, Any]:
        return self._request(
            "GET", "/api/v3/ticker/24hr", params={"symbol": symbol.upper()}
        )

    # ------------------------------------------------------------------
    # Endpoints prives (lecture)
    # ------------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/api/v3/account", signed=True)

    def get_commission_rates(self, symbol: str) -> dict[str, Any]:
        """Commissions et reduction du compte pour une paire, en lecture seule."""
        return self._request(
            "GET", "/api/v3/account/commission",
            params={"symbol": symbol.strip().upper()}, signed=True,
        )

    def get_balances(self) -> dict[str, dict[str, float]]:
        """{asset: {"free": x, "locked": y}} — soldes non nuls seulement."""
        account = self.get_account()
        balances: dict[str, dict[str, float]] = {}
        for row in account.get("balances", []):
            free = float(row.get("free", 0) or 0)
            locked = float(row.get("locked", 0) or 0)
            if free or locked:
                balances[row["asset"]] = {"free": free, "locked": locked}
        return balances

    def get_free_balance(self, asset: str) -> float:
        return self.get_balances().get(asset.upper(), {}).get("free", 0.0)

    def get_open_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol.upper()} if symbol else None
        orders = self._request("GET", "/api/v3/openOrders", params=params, signed=True) or []
        return [self._with_fills(order) for order in orders]

    def _with_fills(self, order: dict[str, Any]) -> dict[str, Any]:
        """Recupere les commissions absentes de GET order avant tout calcul net."""
        executed = float(order.get("executedQty") or 0)
        if executed <= 0 or order.get("fills"):
            return order
        trades = self.get_my_trades(order["symbol"], order_id=order["orderId"], limit=1000)
        quantity = sum(float(trade.get("qty") or 0) for trade in trades)
        if not math.isclose(quantity, executed, rel_tol=1e-9, abs_tol=1e-12):
            raise BinanceError("Historique des executions incomplet : quantite nette non confirmee")
        if any("commission" not in trade or not trade.get("commissionAsset") for trade in trades):
            raise BinanceError("Commissions des executions indisponibles")
        return order | {"fills": [
            {"price": t["price"], "qty": t["qty"], "commission": t["commission"],
             "commissionAsset": t["commissionAsset"], "tradeId": t.get("id")}
            for t in trades
        ]}

    def get_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if order_id is None and client_order_id is None:
            raise ValueError("order_id ou client_order_id requis")
        order = self._request(
            "GET",
            "/api/v3/order",
            params={
                "symbol": symbol.upper(),
                "orderId": order_id,
                "origClientOrderId": client_order_id,
            },
            signed=True,
        )
        return self._with_fills(order)

    def get_order_list(
        self, *, order_list_id: Optional[int] = None,
        list_client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if order_list_id is None and list_client_order_id is None:
            raise ValueError("order_list_id ou list_client_order_id requis")
        return self._request(
            "GET", "/api/v3/orderList",
            params={"orderListId": order_list_id, "origClientOrderId": list_client_order_id},
            signed=True,
        )

    def find_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Comme get_order mais retourne None si l'ordre est inconnu.

        C'est la brique d'idempotence : avant tout retry, on demande a Binance
        si l'ordre existe deja (sections 77 et 80 du cahier des charges).
        """
        try:
            return self.get_order(
                symbol, order_id=order_id, client_order_id=client_order_id
            )
        except BinanceError as exc:
            if exc.is_unknown_order:
                return None
            raise

    def get_my_trades(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v3/myTrades",
            params={"symbol": symbol.upper(), "orderId": order_id, "limit": limit},
            signed=True,
        ) or []

    # ------------------------------------------------------------------
    # Endpoints prives (ecriture) — soumis a la whitelist Demo
    # ------------------------------------------------------------------

    def create_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[str] = None,
        price: Optional[str] = None,
        stop_price: Optional[str] = None,
        time_in_force: Optional[str] = None,
        client_order_id: Optional[str] = None,
        quote_order_qty: Optional[str] = None,
        test: bool = False,
    ) -> dict[str, Any]:
        """Cree un ordre. `test=True` utilise /order/test (aucune execution).

        Les quantites/prix sont passes en STR deja arrondis par symbol_rules,
        pour eviter toute notation scientifique ou perte de precision.
        """
        endpoint = "/api/v3/order/test" if test else "/api/v3/order"
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
            "price": price,
            "stopPrice": stop_price,
            "timeInForce": time_in_force,
            "newClientOrderId": client_order_id,
            "quoteOrderQty": quote_order_qty,
            "newOrderRespType": "FULL",
        }
        return self._request("POST", endpoint, params=params, signed=True)

    def create_oco_sell(
        self, params: Mapping[str, str], *, experimental_confirmation: bool = False
    ) -> dict[str, Any]:
        """Transport OCO Spot Demo, non utilisé par le worker V2.

        L'appelant doit d'abord gérer la migration du SL, la réservation du
        solde et la persistance des deux ordres. Aucun appel automatique ici.
        """
        if not experimental_confirmation:
            raise ValueError("OCO expérimental : confirmation explicite requise")
        if params.get("side") != "SELL":
            raise ValueError("Seul un OCO de vente est pris en charge")
        return self._request(
            "POST", "/api/v3/orderList/oco", params=params, signed=True
        )

    def cancel_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if order_id is None and client_order_id is None:
            raise ValueError("order_id ou client_order_id requis")
        return self._request(
            "DELETE",
            "/api/v3/order",
            params={
                "symbol": symbol.upper(),
                "orderId": order_id,
                "origClientOrderId": client_order_id,
            },
            signed=True,
        )

    def cancel_order_safe(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Annule sans lever si l'ordre a deja disparu (deja rempli/annule)."""
        try:
            return self.cancel_order(
                symbol, order_id=order_id, client_order_id=client_order_id
            )
        except BinanceError as exc:
            if exc.is_unknown_order:
                logger.info("Annulation ignoree : ordre deja absent (%s)", symbol)
                return None
            raise

    # ------------------------------------------------------------------
    # Diagnostic
    # ------------------------------------------------------------------

    def connectivity_report(self) -> dict[str, Any]:
        """Verification non destructive, utilisable dans l'UI et les scripts."""
        report: dict[str, Any] = {
            "base_url": self.base_url,
            "environment": self.settings.environment.value,
            "run_mode": self.settings.run_mode.value,
            "url_whitelisted": None,
            "ping_ok": False,
            "server_time_offset_ms": None,
            "credentials_set": self.settings.has_credentials,
            "account_ok": False,
            "can_trade": None,
            "quote_free": None,
            "errors": [],
        }

        try:
            self.settings.assert_write_allowed("connectivity_report")
            report["url_whitelisted"] = True
        except Exception as exc:  # SecurityError
            report["url_whitelisted"] = False
            report["errors"].append(str(exc))

        try:
            self.ping()
            report["ping_ok"] = True
            report["server_time_offset_ms"] = self.sync_time()
        except BinanceError as exc:
            report["errors"].append(f"ping : {exc}")
            return report

        if not self.settings.has_credentials:
            report["errors"].append("Cles API non renseignees dans .env")
            return report

        try:
            account = self.get_account()
            report["account_ok"] = True
            report["can_trade"] = bool(account.get("canTrade"))
            quote = self.settings.quote_asset
            for row in account.get("balances", []):
                if row.get("asset") == quote:
                    report["quote_free"] = float(row.get("free", 0) or 0)
                    break
        except BinanceError as exc:
            report["errors"].append(f"account : {exc}")

        return report
