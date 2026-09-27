"""Dashboard Service — agr egats et lectures pour les pages Streamlit.

Centralise tout ce que l'UI affiche : etat du worker, portefeuille, positions,
ordres Binance reels, prix courants, journal d'evenements. Les pages Streamlit
ne parlent jamais directement au client Binance : elles passent par ici.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from .binance_client import BinanceError, BinanceSpotClient
from .bot_process_manager import BotProcessManager, WorkerStatus
from .config import Settings, get_settings
from .event_store import EventStore
from .models import (
    BotRuntime,
    Position,
    PositionStatus,
    SyncStatus,
    utcnow,
)
from .notification_engine import NotificationEngine
from .market_price_stream import DemoMarketPriceStream
from .wallet_valuation import WalletValuation, conversion_rate, value_wallet
from .position_engine import recompute_position
from .position_store import PositionStore, get_presets_store, get_settings_store, summarize
from .risk_engine import PortfolioSnapshot, RiskEngine, RiskLimits
from .symbol_rules import SymbolRulesCache
from .binance_client import BinanceSpotClient as _Client  # noqa: F401

logger = logging.getLogger("bsm.dashboard")


@dataclass
class PortfolioView:
    """Tout ce que le Dashboard affiche dans la vue globale (section 51)."""

    quote_asset: str = "USDT"
    quote_free: float = 0.0
    quote_locked: float = 0.0
    base_balances: dict[str, float] = field(default_factory=dict)

    capital_committed: float = 0.0
    capital_pending: float = 0.0
    capital_reserved: float = 0.0
    capital_total: float = 0.0

    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    total_pnl: float = 0.0

    open_positions: int = 0
    total_risk_quote: float = 0.0
    total_risk_percent: float = 0.0

    errors: list[str] = field(default_factory=list)
    source: str = "local"

    @property
    def exposure_percent(self) -> float:
        total = self.quote_free + self.quote_locked + self.capital_committed
        if total <= 0:
            return 0.0
        return self.capital_committed / total * 100.0


@dataclass
class PositionRow:
    """Ligne de la liste des positions (section 51)."""

    position_id: str
    symbol: str
    status: str
    sync_status: str
    average_price: float
    current_price: float
    net_qty: float
    pnl_total: float
    pnl_percent: float
    sl_price: Optional[float]
    next_tp_price: Optional[float]
    next_tp_number: Optional[int]
    entries_total: int
    entries_filled: int
    tps_total: int
    tps_executed: int
    capital_committed: float
    has_desync: bool
    automation_paused: bool

    @property
    def progress_percent(self) -> float:
        if self.tps_total <= 0:
            return 0.0
        return self.tps_executed / self.tps_total * 100.0


@dataclass
class OpenOrderRow:
    """Ordre reellement ouvert cote Binance (section 53)."""

    order_id: int
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    price: float
    stop_price: float
    orig_qty: float
    executed_qty: float
    status: str
    time_ms: Optional[int]
    owner: str = ""
    owned_by_bot: bool = False

    @property
    def remaining_qty(self) -> float:
        return max(self.orig_qty - self.executed_qty, 0.0)


class DashboardService:
    """Facade de lecture pour l'interface."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        position_store: Optional[PositionStore] = None,
        client: Optional[BinanceSpotClient] = None,
        process_manager: Optional[BotProcessManager] = None,
        events: Optional[EventStore] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.positions = position_store or PositionStore()
        self.client = client or BinanceSpotClient(self.settings)
        self.rules_cache = SymbolRulesCache(self.client)
        self.process_manager = process_manager or BotProcessManager(self.settings)
        self.events = events or EventStore()
        self.notifications = NotificationEngine(self.settings)
        self.market_prices = DemoMarketPriceStream(self.settings)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def worker_status(self) -> WorkerStatus:
        return self.process_manager.status()

    def runtime(self) -> BotRuntime:
        return self.process_manager.runtime_snapshot()

    # ------------------------------------------------------------------
    # Portefeuille
    # ------------------------------------------------------------------

    def wallet_valuation(self) -> WalletValuation:
        """Soldes réels libres + bloqués, cotés sur la même API Demo."""
        if not self.settings.is_demo or not self.settings.has_credentials:
            raise ValueError("Compte Binance Demo connecté requis")
        balances = self.client.get_balances()
        prices = self.client.get_prices()
        return value_wallet(balances, prices)

    def portfolio(self, *, live: bool = True) -> PortfolioView:
        """Vue portefeuille. `live=False` evite tout appel reseau (tests/DRY_RUN)."""
        view = PortfolioView(quote_asset=self.settings.quote_asset)
        positions = self.positions.list_all()

        if live and self.settings.has_credentials and not self.settings.dry_run:
            try:
                balances = self.client.get_balances()
                quote = balances.get(self.settings.quote_asset, {})
                view.quote_free = float(quote.get("free", 0.0))
                view.quote_locked = float(quote.get("locked", 0.0))
                view.base_balances = {
                    asset: data["free"]
                    for asset, data in balances.items()
                    if asset != self.settings.quote_asset
                }
                view.source = "binance"
            except BinanceError as exc:
                view.errors.append(f"Balances Binance indisponibles : {exc}")
                view.source = "local"
        else:
            view.quote_free = self._last_known_quote()
            view.base_balances = self._last_known_bases(positions)

        open_positions = [p for p in positions if p.status.is_open]
        quote_assets = {p.quote_asset for p in positions}
        rates = {self.settings.quote_asset: 1.0}
        if quote_assets - rates.keys():
            try:
                prices = self.client.get_prices() if live else {}
                for asset in quote_assets - rates.keys():
                    rate = conversion_rate(asset, self.settings.quote_asset, prices)
                    if rate is None:
                        view.errors.append(
                            f"Taux {asset}/{self.settings.quote_asset} indisponible : "
                            "totaux des positions incomplets"
                        )
                    else:
                        rates[asset] = rate
            except Exception as exc:
                view.errors.append(f"Taux de conversion indisponibles : {exc}")
        view.open_positions = len(open_positions)
        view.capital_committed = sum(
            p.metrics.capital_committed * rates.get(p.quote_asset, 0.0) for p in open_positions
        )
        view.capital_pending = sum(
            p.metrics.capital_pending * rates.get(p.quote_asset, 0.0) for p in open_positions
        )
        view.unrealized_pnl = sum(
            p.pnl.unrealized * rates.get(p.quote_asset, 0.0) for p in open_positions
        )
        view.realized_pnl = sum(
            p.pnl.realized * rates.get(p.quote_asset, 0.0) for p in positions
        )
        view.total_pnl = view.unrealized_pnl + view.realized_pnl
        view.total_risk_quote = sum(
            abs(p.metrics.max_loss_at_sl) * rates.get(p.quote_asset, 0.0)
            for p in open_positions
        )

        total = view.quote_free + view.quote_locked + view.capital_committed
        view.capital_total = total
        view.capital_reserved = total * self.settings.capital_reserve_percent / 100.0
        if total > 0:
            view.total_risk_percent = view.total_risk_quote / total * 100.0

        return view

    def _last_known_quote(self) -> float:
        store = get_settings_store()
        payload = store.load()
        value = payload.get("last_known_quote_balance")
        return float(value) if isinstance(value, (int, float)) else 0.0

    @staticmethod
    def _last_known_bases(positions: list[Position]) -> dict[str, float]:
        bases: dict[str, float] = {}
        for position in positions:
            if position.is_open and position.metrics.net_qty > 0:
                bases[position.base_asset] = (
                    bases.get(position.base_asset, 0.0) + position.metrics.net_qty
                )
        return bases

    def risk_snapshot(self, quote_free: Optional[float] = None) -> PortfolioSnapshot:
        if quote_free is None:
            quote_free = self.portfolio(live=False).quote_free
        positions = self.positions.list_all()
        quote_assets = {p.quote_asset for p in positions if p.is_open}
        rates = {self.settings.quote_asset: 1.0}
        if quote_assets - rates.keys():
            prices = self.client.get_prices()
            for asset in quote_assets - rates.keys():
                rate = conversion_rate(asset, self.settings.quote_asset, prices)
                if rate is None:
                    raise ValueError(f"Taux {asset}/{self.settings.quote_asset} indisponible")
                rates[asset] = rate
        return RiskEngine.snapshot(positions, quote_free, quote_rates=rates)

    def risk_limits(self) -> RiskLimits:
        saved = get_settings_store().load()
        saved = saved if isinstance(saved, dict) else {}

        def number(key: str, default: float, minimum: float, maximum: float) -> float:
            try:
                value = float(saved.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if minimum <= value <= maximum else default

        return RiskLimits(
            max_risk_per_position_percent=number(
                "max_risk_per_position_percent", self.settings.max_risk_per_position_percent, 0.1, 100
            ),
            max_total_risk_percent=number(
                "max_total_risk_percent", self.settings.max_total_risk_percent, 0.1, 100
            ),
            max_open_positions=int(number(
                "max_open_positions", self.settings.max_open_positions, 1, 50
            )),
            max_exposure_per_symbol_percent=number(
                "max_exposure_per_symbol_percent", self.settings.max_exposure_per_symbol_percent, 1, 100
            ),
            min_reserve_percent=number(
                "capital_reserve_percent", self.settings.capital_reserve_percent, 0, 90
            ),
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def position_rows(
        self, positions: Optional[list[Position]] = None, *, with_prices: bool = True
    ) -> list[PositionRow]:
        positions = positions if positions is not None else self.positions.list_all()
        prices: dict[str, float] = {}
        if with_prices:
            symbols = [p.symbol for p in positions if p.is_open]
            if symbols and not self.settings.dry_run:
                try:
                    prices = self.client.get_prices(symbols)
                except BinanceError as exc:
                    logger.warning("Prix indisponibles : %s", exc)

        rows: list[PositionRow] = []
        for position in positions:
            current = prices.get(position.symbol) or position.metrics.current_price
            if current:
                position.metrics.current_price = current
                recompute_position(position)

            next_tp = position.next_tp
            rows.append(
                PositionRow(
                    position_id=position.position_id,
                    symbol=position.symbol,
                    status=position.status.value,
                    sync_status=position.sync_status.value,
                    average_price=position.metrics.average_price,
                    current_price=current or 0.0,
                    net_qty=position.metrics.net_qty,
                    pnl_total=position.pnl.total,
                    pnl_percent=position.pnl.unrealized_percent,
                    sl_price=position.stop_loss.resolved_price,
                    next_tp_price=next_tp.target_price if next_tp else None,
                    next_tp_number=next_tp.sequence_number if next_tp else None,
                    entries_total=len(position.entries),
                    entries_filled=len(position.filled_entries),
                    tps_total=len(position.take_profits),
                    tps_executed=len(position.executed_tps),
                    capital_committed=position.metrics.capital_committed,
                    has_desync=position.sync_status
                    not in {SyncStatus.SYNCED, SyncStatus.RECONCILED},
                    automation_paused=position.automation.paused,
                )
            )
        return rows

    def open_positions(self) -> list[Position]:
        return self.positions.list_open()

    def get_position(self, position_id: str) -> Optional[Position]:
        return self.positions.load(position_id)

    def find_by_symbol(self, symbol: str) -> Optional[Position]:
        return self.positions.find_active_by_symbol(symbol)

    # ------------------------------------------------------------------
    # Prix
    # ------------------------------------------------------------------

    def current_price(self, symbol: str) -> Optional[float]:
        streamed = self.market_prices.prices([symbol]).get(symbol.upper())
        if streamed is not None:
            return streamed
        if self.settings.dry_run or not self.settings.has_credentials:
            # DRY_RUN : le prix public reste accessible sans cle.
            pass
        try:
            return self.client.get_price(symbol)
        except BinanceError as exc:
            logger.warning("Prix %s indisponible : %s", symbol, exc)
            return None

    def prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        streamed = self.market_prices.prices(symbols)
        missing = [symbol for symbol in symbols if symbol.upper() not in streamed]
        if not missing:
            return streamed
        try:
            return {**streamed, **self.client.get_prices(missing)}
        except BinanceError as exc:
            logger.warning("Prix indisponibles : %s", exc)
            return streamed

    # ------------------------------------------------------------------
    # Ordres Binance reels
    # ------------------------------------------------------------------

    def open_orders(self, symbol: Optional[str] = None) -> tuple[list[OpenOrderRow], str]:
        """Ordres ouverts reels + message d'erreur eventuel.

        Les TPs surveilles par le worker n'apparaissent ici que s'ils ont ete
        envoyes ; le SL unique y figure. Distinction stricte avec l'historique
        local imposee par la section 53.
        """
        if self.settings.dry_run:
            return [], "DRY_RUN : aucun ordre reel n'existe"
        if not self.settings.has_credentials:
            return [], "Cles API absentes : ordres Binance non consultables"

        try:
            raw = self.client.get_open_orders(symbol)
        except BinanceError as exc:
            return [], f"Ordres ouverts indisponibles : {exc}"

        ownership = self._ownership_map()
        rows: list[OpenOrderRow] = []
        for order in raw:
            client_id = order.get("clientOrderId") or ""
            rows.append(
                OpenOrderRow(
                    order_id=int(order.get("orderId", 0)),
                    client_order_id=client_id,
                    symbol=order.get("symbol", ""),
                    side=order.get("side", ""),
                    order_type=order.get("type", ""),
                    price=float(order.get("price", 0) or 0),
                    stop_price=float(order.get("stopPrice", 0) or 0),
                    orig_qty=float(order.get("origQty", 0) or 0),
                    executed_qty=float(order.get("executedQty", 0) or 0),
                    status=order.get("status", ""),
                    time_ms=order.get("time"),
                    owner=ownership.get(client_id, ""),
                    owned_by_bot=client_id.startswith("BSM-"),
                )
            )
        return rows, ""

    def _ownership_map(self) -> dict[str, str]:
        """{clientOrderId: description lisible} pour tous les ordres du bot."""
        mapping: dict[str, str] = {}
        for position in self.positions.list_all():
            for entry in position.entries:
                if entry.client_order_id:
                    mapping[entry.client_order_id] = f"Entry {entry.sequence_number}"
            for tp in position.take_profits:
                if tp.client_order_id:
                    mapping[tp.client_order_id] = f"TP {tp.sequence_number}"
            if position.stop_loss.client_order_id:
                mapping[position.stop_loss.client_order_id] = "SL"
        return mapping

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.events.tail(limit=limit)

    def recent_errors(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.events.errors(limit=limit)

    # ------------------------------------------------------------------
    # Persistance de preference
    # ------------------------------------------------------------------

    def remember_quote_balance(self, value: float) -> None:
        get_settings_store().update({"last_known_quote_balance": float(value)})

    def save_user_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        return get_settings_store().update(values)

    def user_settings(self) -> dict[str, Any]:
        return get_settings_store().load()

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def presets(self) -> dict[str, Any]:
        return get_presets_store().load()

    def save_preset(self, name: str, payload: dict[str, Any]) -> None:
        store = get_presets_store()
        current = store.load()
        current[name] = {**payload, "saved_at": utcnow().isoformat()}
        store.save(current)

    def delete_preset(self, name: str) -> None:
        store = get_presets_store()
        current = store.load()
        current.pop(name, None)
        store.save(current)

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return summarize(self.positions.list_all())

    def history(self, days: int = 30) -> list[Position]:
        cutoff = utcnow() - timedelta(days=days)
        return [
            p
            for p in self.positions.list_all()
            if not p.is_open and (p.closed_at or p.updated_at) >= cutoff
        ]
