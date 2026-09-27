"""Execution Engine — tout ce qui parle reellement a Binance.

Principes non negociables :
- aucune ecriture ne part sans `settings.assert_write_allowed()` (via le client) ;
- avant tout envoi, on regarde si le clientOrderId existe deja cote Binance :
  un retry ne doit JAMAIS creer un second ordre (sections 77 et 80) ;
- les quantites/prix partent en STR deja arrondis par SymbolRules ;
- un ordre n'est considere rempli qu'apres lecture de son statut reel ;
- en DRY_RUN, rien n'est envoye : on simule et on journalise DRY_RUN_SKIP.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .binance_client import BinanceError, BinanceSpotClient
from .config import Settings, get_settings
from .event_store import EventStore
from .models import (
    Commission,
    Entry,
    EntryStatus,
    EventType,
    OrderSide,
    OrderType,
    Position,
    SLStatus,
    TakeProfit,
    TPStatus,
    utcnow,
)
from .position_engine import QTY_EPSILON
from .symbol_rules import SymbolRules, SymbolRulesCache

logger = logging.getLogger("bsm.execution")

#: Prefixe commun a tous les ordres generes par le bot.
CLIENT_ID_PREFIX = "BSM"

#: Binance limite newClientOrderId a 36 caracteres, charset [A-Za-z0-9-_].
_MAX_CLIENT_ID_LEN = 36
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9\-_]")


class ExecutionError(RuntimeError):
    """Echec d'execution non recuperable automatiquement."""


class NotConnectedError(ExecutionError):
    pass


@dataclass
class OrderResult:
    """Resultat normalise d'un envoi d'ordre."""

    success: bool = False
    dry_run: bool = False
    order_id: Optional[int] = None
    client_order_id: str = ""
    status: str = ""
    executed_qty: float = 0.0
    cummulative_quote_qty: float = 0.0
    average_price: float = 0.0
    commissions: list[Commission] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def is_filled(self) -> bool:
        return self.status.upper() in {"FILLED"}

    @property
    def is_partially_filled(self) -> bool:
        return self.status.upper() in {"PARTIALLY_FILLED"}

    @property
    def is_open(self) -> bool:
        return self.status.upper() in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}

    @property
    def is_terminal_dead(self) -> bool:
        return self.status.upper() in {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}


# ==========================================================================
# clientOrderId
# ==========================================================================


def build_client_order_id(
    *,
    symbol: str,
    position_id: str,
    suffix: str,
    environment: str = "DEMO",
) -> str:
    """Construit un identifiant d'ordre tracable et idempotent.

    Exemple conceptuel (section 78) : BSM-BTC-a1b2c3d4-E3
    Le base asset est tronque pour rester sous les 36 caracteres Binance.
    """
    base = re.sub(r"[^A-Za-z0-9]", "", symbol.upper())
    suffix = _SANITIZE_RE.sub("", suffix.upper()) or "X"

    short_position = _SANITIZE_RE.sub("", position_id.split("_")[-1])[:8]
    env = "D" if environment.upper() == "DEMO" else "L"

    prefix = f"{CLIENT_ID_PREFIX}-{env}-"
    tail = f"-{suffix}"

    room = _MAX_CLIENT_ID_LEN - len(prefix) - len(tail) - len(short_position) - 1
    if room < 1:
        return f"{prefix}{short_position}{tail}"[:_MAX_CLIENT_ID_LEN]

    base = base[:room]
    return f"{prefix}{base}-{short_position}{tail}"


# ==========================================================================
# Normalisation des reponses
# ==========================================================================


def _commissions_from_order(order: dict[str, Any]) -> list[Commission]:
    """Extrait les commissions des fills d'une reponse FULL."""
    commissions: list[Commission] = []
    for fill in order.get("fills", []) or []:
        asset = fill.get("commissionAsset")
        amount = fill.get("commission")
        if asset and amount:
            commissions.append(Commission(asset=asset, amount=float(amount)))
    return _merge_commissions(commissions)


def _merge_commissions(commissions: list[Commission]) -> list[Commission]:
    merged: dict[str, float] = {}
    for commission in commissions:
        merged[commission.asset] = merged.get(commission.asset, 0.0) + commission.amount
    return [Commission(asset=asset, amount=amount) for asset, amount in sorted(merged.items())]


def _average_price(order: dict[str, Any]) -> float:
    executed = float(order.get("executedQty", 0) or 0)
    quote = float(order.get("cummulativeQuoteQty", 0) or 0)
    if executed > 0 and quote > 0:
        return quote / executed
    fills = order.get("fills") or []
    qty = sum(float(f.get("qty", 0) or 0) for f in fills)
    quote_from_fills = sum(
        float(f.get("qty", 0) or 0) * float(f.get("price", 0) or 0) for f in fills
    )
    return (quote_from_fills / qty) if qty > 0 else 0.0


def normalize_order_response(
    order: dict[str, Any],
    *,
    client_order_id: str = "",
    dry_run: bool = False,
) -> OrderResult:
    """Convertit une reponse Binance en OrderResult uniforme.

    Utilise partout (creation, lecture de statut, myTrades) pour que le reste
    du code ne connaisse qu'un seul format.
    """
    if not order:
        return OrderResult(success=False, error="Reponse Binance vide")

    executed = float(order.get("executedQty", 0) or 0)
    return OrderResult(
        success=True,
        dry_run=dry_run,
        order_id=order.get("orderId"),
        client_order_id=order.get("clientOrderId") or client_order_id,
        status=str(order.get("status", "")),
        executed_qty=executed,
        cummulative_quote_qty=float(order.get("cummulativeQuoteQty", 0) or 0),
        average_price=_average_price(order),
        commissions=_commissions_from_order(order),
        raw=order,
    )


# ==========================================================================
# Execution Engine
# ==========================================================================


class ExecutionEngine:
    """Envoi d'ordres, avec idempotence et simulation DRY_RUN."""

    def __init__(
        self,
        client: Optional[BinanceSpotClient] = None,
        rules_cache: Optional[SymbolRulesCache] = None,
        *,
        settings: Optional[Settings] = None,
        events: Optional[EventStore] = None,
        confirm: Optional[Callable[[str, dict[str, Any]], bool]] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or BinanceSpotClient(self.settings)
        self.rules_cache = rules_cache or SymbolRulesCache(self.client)
        self.events = events or EventStore()
        #: callback de validation humaine (mode DEMO_MANUAL). None = pas de garde.
        self.confirm = confirm

    # ------------------------------------------------------------------
    # Regles & helpers
    # ------------------------------------------------------------------

    def rules(self, symbol: str, *, refresh: bool = False) -> SymbolRules:
        return self.rules_cache.get(symbol, refresh=refresh)

    def _dry_run(self) -> bool:
        return self.settings.dry_run

    def _confirm(self, action: str, payload: dict[str, Any]) -> bool:
        """Validation humaine eventuelle avant envoi."""
        if self.confirm is None:
            return True
        return bool(self.confirm(action, payload))

    def _log_skip(self, action: str, symbol: str, position_id: str, **data: Any) -> None:
        self.events.append(
            EventType.DRY_RUN_SKIP,
            f"DRY_RUN : {action} non envoye ({symbol})",
            position_id=position_id,
            symbol=symbol,
            action=action,
            **data,
        )

    def _simulated_result(
        self, *, client_order_id: str, price: float, qty: float
    ) -> OrderResult:
        """Resultat simule en DRY_RUN — jamais envoye, jamais compte comme fill."""
        return OrderResult(
            success=True,
            dry_run=True,
            order_id=None,
            client_order_id=client_order_id,
            status="DRY_RUN",
            executed_qty=0.0,
            cummulative_quote_qty=0.0,
            average_price=price,
            raw={"simulated": True, "price": price, "quantity": qty},
        )

    # ------------------------------------------------------------------
    # Envoi d'une Entry
    # ------------------------------------------------------------------

    def place_entry(
        self,
        position: Position,
        entry: Entry,
        *,
        current_price: float,
    ) -> OrderResult:
        """Envoie l'ordre d'une Entry (Market ou Limit)."""
        client_order_id = entry.client_order_id or build_client_order_id(
            symbol=position.symbol,
            position_id=position.position_id,
            suffix=f"E{entry.sequence_number}",
            environment=position.environment,
        )

        # Idempotence : l'ordre existe-t-il deja cote Binance ?
        existing = self.find_existing_order(position.symbol, client_order_id)
        if existing is not None:
            logger.info("Ordre deja present cote Binance : %s", client_order_id)
            result = normalize_order_response(existing, client_order_id=client_order_id)
            entry.order_id = result.order_id
            entry.client_order_id = client_order_id
            if result.is_open:
                entry.status = EntryStatus.SUBMITTED
            return result

        if self._dry_run():
            self._log_skip("create_entry", position.symbol, position.position_id, qty=entry.binance_qty)
            result = self._simulated_result(
                client_order_id=client_order_id,
                price=entry.resolved_price or current_price,
                qty=entry.binance_qty,
            )
            entry.client_order_id = client_order_id
            return result

        if not self._confirm(
            "PLACE_ENTRY",
            {
                "symbol": position.symbol,
                "entry": entry.sequence_number,
                "type": entry.order_type.value,
                "qty": entry.binance_qty,
                "price": entry.resolved_price,
            },
        ):
            return OrderResult(success=False, error="Annule par l'utilisateur")

        market = entry.order_type is OrderType.MARKET
        result = self._create_order_safe(
            symbol=position.symbol,
            side=OrderSide.BUY.value,
            order_type=entry.order_type.value,
            quantity=entry.binance_qty,
            price=None if market else entry.resolved_price,
            client_order_id=client_order_id,
            market=market,
        )

        entry.client_order_id = client_order_id
        entry.order_id = result.order_id

        if result.success:
            entry.submitted_at = utcnow()
            if result.is_filled:
                entry.status = EntryStatus.FILLED
            elif result.is_partially_filled:
                entry.status = EntryStatus.PARTIALLY_FILLED
            elif result.is_open:
                entry.status = EntryStatus.SUBMITTED
            elif result.is_terminal_dead:
                entry.status = EntryStatus.REJECTED
        else:
            entry.status = EntryStatus.REJECTED
            entry.last_error = result.error

        self.events.append(
            EventType.ENTRY_CREATED if result.success else EventType.ERROR,
            f"Entry {entry.sequence_number} {position.symbol} — statut {result.status or 'ECHEC'}",
            position_id=position.position_id,
            symbol=position.symbol,
            level="INFO" if result.success else "ERROR",
            client_order_id=client_order_id,
            error=result.error,
        )
        return result

    # ------------------------------------------------------------------
    # Envoi d'un TP (vente au declenchement)
    # ------------------------------------------------------------------

    def place_tp_sell(
        self,
        position: Position,
        tp: TakeProfit,
        *,
        quantity: float,
        current_price: float,
    ) -> OrderResult:
        """Vend la quantite prevue par un TP (section 14, etapes 3 a 6)."""
        rules = self.rules(position.symbol)
        qty = float(rules.round_qty(quantity, market=True))

        min_qty = rules.market_min_qty or rules.min_qty
        if qty <= 0 or qty < float(min_qty):
            return OrderResult(
                success=False,
                error=f"Quantite a vendre sous minQty ({qty} < {min_qty})",
            )
        if rules.min_notional > 0:
            notional = qty * current_price
            if notional < float(rules.min_notional):
                return OrderResult(
                    success=False,
                    error=(
                        f"Notionnel TP sous minNotional "
                        f"({notional:.8f} < {rules.min_notional})"
                    ),
                )

        client_order_id = tp.client_order_id or build_client_order_id(
            symbol=position.symbol,
            position_id=position.position_id,
            suffix=(
                f"TP{tp.sequence_number}R{tp.attempt_count}"
                if tp.attempt_count else f"TP{tp.sequence_number}"
            ),
            environment=position.environment,
        )

        existing = self.find_existing_order(position.symbol, client_order_id)
        if existing is not None:
            result = normalize_order_response(existing, client_order_id=client_order_id)
            tp.order_id = result.order_id
            tp.client_order_id = client_order_id
            return result

        if self._dry_run():
            self._log_skip("tp_sell", position.symbol, position.position_id, qty=qty)
            result = self._simulated_result(
                client_order_id=client_order_id, price=current_price, qty=qty
            )
            tp.client_order_id = client_order_id
            return result

        if not self._confirm(
            "TP_SELL",
            {
                "symbol": position.symbol,
                "tp": tp.sequence_number,
                "qty": qty,
                "target": tp.target_price,
            },
        ):
            return OrderResult(success=False, error="Annule par l'utilisateur")

        market = tp.execution_policy.value == "MARKET_ON_TRIGGER"
        result = self._create_order_safe(
            symbol=position.symbol,
            side=OrderSide.SELL.value,
            order_type=OrderType.MARKET.value if market else OrderType.LIMIT.value,
            quantity=qty,
            price=None if market else tp.target_price,
            client_order_id=client_order_id,
            market=market,
            time_in_force=None if market else "FOK",
        )

        tp.client_order_id = client_order_id
        tp.order_id = result.order_id
        if result.success:
            tp.status = (
                TPStatus.EXECUTED
                if result.is_filled
                else TPStatus.SUBMITTED
                if result.is_open
                else TPStatus.FAILED
            )
        else:
            tp.status = TPStatus.FAILED
            tp.last_error = result.error

        self.events.append(
            EventType.TP_EXECUTED if result.success else EventType.ERROR,
            f"TP {tp.sequence_number} {position.symbol} — statut {result.status or 'ECHEC'}",
            position_id=position.position_id,
            symbol=position.symbol,
            level="INFO" if result.success else "ERROR",
            error=result.error,
        )
        return result

    # ------------------------------------------------------------------
    # Stop Loss unique cote Binance
    # ------------------------------------------------------------------

    def _sl_client_order_id(self, position: Position, attempt: int = 0) -> str:
        suffix = "SL" if attempt == 0 else f"SL{attempt}"
        return build_client_order_id(
            symbol=position.symbol,
            position_id=position.position_id,
            suffix=suffix,
            environment=position.environment,
        )

    def place_stop_loss(
        self,
        position: Position,
        *,
        stop_price: float,
        quantity: float,
        attempt: int = 0,
    ) -> OrderResult:
        """Cree l'unique SL Binance (STOP_LOSS_LIMIT) sur la quantite restante."""
        rules = self.rules(position.symbol)
        qty = float(rules.round_qty(quantity, market=False))
        if qty <= 0:
            return OrderResult(success=False, error="Quantite SL nulle")

        stop = float(rules.round_price(stop_price, mode="down"))
        limit_price = float(
            rules.round_price(
                stop * (1.0 - position.stop_loss.limit_offset_percent / 100.0),
                mode="down",
            )
        )

        client_order_id = self._sl_client_order_id(position, attempt)

        existing = self.find_existing_order(position.symbol, client_order_id)
        if existing is not None and normalize_order_response(existing).is_open:
            position.stop_loss.order_id = existing.get("orderId")
            position.stop_loss.client_order_id = client_order_id
            position.stop_loss.status = SLStatus.ACTIVE
            return normalize_order_response(existing, client_order_id=client_order_id)

        if self._dry_run():
            self._log_skip(
                "create_sl", position.symbol, position.position_id, stop=stop, qty=qty
            )
            position.stop_loss.client_order_id = client_order_id
            return self._simulated_result(
                client_order_id=client_order_id, price=stop, qty=qty
            )

        if not self._confirm(
            "CREATE_SL", {"symbol": position.symbol, "stop": stop, "qty": qty}
        ):
            return OrderResult(success=False, error="Annule par l'utilisateur")

        try:
            raw = self.client.create_order(
                symbol=position.symbol,
                side=OrderSide.SELL.value,
                order_type="STOP_LOSS_LIMIT",
                quantity=rules.qty_str(qty),
                price=rules.price_str(limit_price),
                stop_price=rules.price_str(stop),
                time_in_force="GTC",
                client_order_id=client_order_id,
            )
            result = normalize_order_response(raw, client_order_id=client_order_id)
        except BinanceError as exc:
            result = OrderResult(success=False, error=str(exc))

        if result.success:
            position.stop_loss.order_id = result.order_id
            position.stop_loss.client_order_id = client_order_id
            position.stop_loss.resolved_price = stop
            position.stop_loss.quantity = qty
            position.stop_loss.status = SLStatus.ACTIVE
            position.stop_loss.created_at = position.stop_loss.created_at or utcnow()
            position.stop_loss.updated_at = utcnow()
            self.events.append(
                EventType.SL_CREATED,
                f"SL {position.symbol} @ {stop} sur {qty}",
                position_id=position.position_id,
                symbol=position.symbol,
                stop=stop,
                qty=qty,
            )
        else:
            position.stop_loss.status = SLStatus.FAILED
            position.stop_loss.last_error = result.error
            self.events.append(
                EventType.ERROR,
                f"Echec creation SL {position.symbol} : {result.error}",
                position_id=position.position_id,
                symbol=position.symbol,
                level="ERROR",
            )
        return result

    def move_stop_loss(
        self,
        position: Position,
        *,
        new_stop_price: float,
        quantity: float,
    ) -> OrderResult:
        """Deplace le SL : annule l'ancien puis cree le nouveau (section 16).

        On annule d'abord pour ne jamais depasser MAX_NUM_ALGO_ORDERS, et
        l'echec de creation est journalise comme une fenetre non protegee.
        """
        previous_order_id = position.stop_loss.order_id
        previous_client_id = position.stop_loss.client_order_id

        position.stop_loss.status = SLStatus.REPLACING

        if not self._dry_run() and previous_order_id:
            if not self._confirm(
                "CANCEL_SL", {"symbol": position.symbol, "order_id": previous_order_id}
            ):
                return OrderResult(success=False, error="Annule par l'utilisateur")
            cancelled = self.cancel_order(
                position.symbol,
                order_id=previous_order_id,
                client_order_id=previous_client_id,
            )
            if not cancelled.success:
                # On ne cree pas de second SL tant que l'ancien vit encore :
                # cela creerait une double protection et deux ventes.
                position.stop_loss.status = SLStatus.ACTIVE
                position.stop_loss.last_error = (
                    f"Annulation SL impossible : {cancelled.error}"
                )
                self.events.append(
                    EventType.ERROR,
                    f"SL non deplace ({position.symbol}) : annulation impossible",
                    position_id=position.position_id,
                    symbol=position.symbol,
                    level="ERROR",
                )
                return cancelled

        position.stop_loss.replace_count += 1
        result = self.place_stop_loss(
            position,
            stop_price=new_stop_price,
            quantity=quantity,
            attempt=position.stop_loss.replace_count,
        )

        if result.success:
            self.events.append(
                EventType.SL_MOVED,
                f"SL {position.symbol} deplace vers {new_stop_price}",
                position_id=position.position_id,
                symbol=position.symbol,
                new_stop=new_stop_price,
            )
        else:
            # Fenetre non protegee : signalee explicitement, jamais masquee.
            self.events.append(
                EventType.ERROR,
                f"ATTENTION : SL {position.symbol} non recree apres deplacement "
                f"({result.error}). Position potentiellement non protegee.",
                position_id=position.position_id,
                symbol=position.symbol,
                level="CRITICAL",
            )
        return result

    # ------------------------------------------------------------------
    # Annulation
    # ------------------------------------------------------------------

    def cancel_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """Annule un ordre. Un ordre deja disparu n'est pas une erreur."""
        if self._dry_run():
            self._log_skip("cancel_order", symbol, "", order_id=order_id)
            return OrderResult(success=True, dry_run=True)

        try:
            raw = self.client.cancel_order(
                symbol, order_id=order_id, client_order_id=client_order_id
            )
            return normalize_order_response(
                raw or {}, client_order_id=client_order_id or ""
            )
        except BinanceError as exc:
            if exc.is_unknown_order:
                return OrderResult(
                    success=True, error="Ordre deja absent cote Binance"
                )
            return OrderResult(success=False, error=str(exc))

    def cancel_entry(self, position: Position, entry: Entry) -> OrderResult:
        result = self.cancel_order(
            position.symbol, order_id=entry.order_id, client_order_id=entry.client_order_id
        )
        if result.success or "deja absent" in result.error:
            entry.status = EntryStatus.CANCELED
            self.events.append(
                EventType.ENTRY_CANCELED,
                f"Entry {entry.sequence_number} annulee ({position.symbol})",
                position_id=position.position_id,
                symbol=position.symbol,
            )
        return result

    def cancel_open_entries(self, position: Position) -> list[OrderResult]:
        """Annule toutes les Entries encore ouvertes (regle de la section 46)."""
        return [self.cancel_entry(position, e) for e in position.open_entries]

    # ------------------------------------------------------------------
    # Idempotence & lecture d'etat
    # ------------------------------------------------------------------

    def find_existing_order(
        self, symbol: str, client_order_id: str
    ) -> Optional[dict[str, Any]]:
        """Cherche un ordre par clientOrderId : d'abord ouvert, puis historique."""
        if not client_order_id or self._dry_run():
            return None

        try:
            for order in self.client.get_open_orders(symbol):
                if order.get("clientOrderId") == client_order_id:
                    return order
        except BinanceError as exc:
            logger.warning("openOrders indisponible pour %s : %s", symbol, exc)

        return self.client.find_order(symbol, client_order_id=client_order_id)

    def fetch_order_status(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Optional[OrderResult]:
        """Statut reel d'un ordre. None si inconnu des deux cotes."""
        if self._dry_run():
            return None
        try:
            raw = self.client.get_order(
                symbol, order_id=order_id, client_order_id=client_order_id
            )
        except BinanceError as exc:
            if exc.is_unknown_order:
                return None
            raise
        return normalize_order_response(raw)

    def fetch_my_trades(
        self, symbol: str, *, order_id: Optional[int] = None
    ) -> list[dict[str, Any]]:
        if self._dry_run():
            return []
        return self.client.get_my_trades(symbol, order_id=order_id)

    def get_open_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        if self._dry_run():
            return []
        return self.client.get_open_orders(symbol)

    def get_balances(self) -> dict[str, dict[str, float]]:
        if self._dry_run():
            return {}
        return self.client.get_balances()

    def get_free_quote(self, asset: Optional[str] = None) -> float:
        asset = (asset or self.settings.quote_asset).upper()
        if self._dry_run():
            return 0.0
        return self.client.get_free_balance(asset)

    def get_free_balance(self, asset: str) -> float:
        if self._dry_run():
            return 0.0
        return self.client.get_free_balance(asset)

    # ------------------------------------------------------------------
    # Cœur : creation d'ordre avec un seul retry controle
    # ------------------------------------------------------------------

    def _create_order_safe(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float],
        client_order_id: str,
        market: bool,
        time_in_force: Optional[str] = None,
        max_attempts: int = 2,
    ) -> OrderResult:
        """Envoie un ordre avec retry SANS jamais risquer de doublon.

        Sequence imposee (section 80) :
          1. si la reponse est un echec transport/5xx, on ne renvoie rien
             avant d'avoir demande a Binance si l'ordre a ete accepte ;
          2. si l'ordre existe deja, on l'adopte au lieu d'en creer un second ;
          3. si l'erreur est un filtre ou une erreur metier, aucun retry.
        """
        rules = self.rules(symbol)
        qty_str = rules.qty_str(quantity, market=market)
        price_str = None if price is None else rules.price_str(price)

        last_error = ""
        for attempt in range(1, max_attempts + 1):
            try:
                raw = self.client.create_order(
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=qty_str,
                    price=price_str,
                    time_in_force=None if market else (time_in_force or "GTC"),
                    client_order_id=client_order_id,
                )
                return normalize_order_response(raw, client_order_id=client_order_id)

            except BinanceError as exc:
                last_error = str(exc)

                # Une erreur de filtre ne se retente jamais : elle se corrige.
                if exc.is_filter_failure:
                    return OrderResult(success=False, error=last_error)

                # Doublon : l'ordre existe deja, on l'adopte.
                if exc.is_duplicate_client_order_id:
                    existing = self.client.find_order(
                        symbol, client_order_id=client_order_id
                    )
                    if existing:
                        return normalize_order_response(
                            existing, client_order_id=client_order_id
                        )
                    return OrderResult(success=False, error=last_error)

                # 5xx / transport : on verifie AVANT tout retry.
                if attempt < max_attempts:
                    time.sleep(0.5)
                    existing = self.client.find_order(
                        symbol, client_order_id=client_order_id
                    )
                    if existing:
                        logger.warning(
                            "Ordre retrouve apres echec transport : %s", client_order_id
                        )
                        return normalize_order_response(
                            existing, client_order_id=client_order_id
                        )
                    continue

                return OrderResult(success=False, error=last_error)

        return OrderResult(success=False, error=last_error or "Echec inconnu")

    # ------------------------------------------------------------------
    # Application du resultat d'un ordre sur la position
    # ------------------------------------------------------------------

    @staticmethod
    def fill_metrics(result: OrderResult) -> tuple[float, float, float]:
        """(quantite executee, prix moyen, montant quote) d'un OrderResult."""
        return result.executed_qty, result.average_price, result.cummulative_quote_qty

    def entry_fill_applied(self, entry: Entry) -> bool:
        """Vrai si l'Entry a deja une execution enregistree (garde d'idempotence)."""
        return entry.executed_qty > QTY_EPSILON
