"""Modeles de donnees dynamiques (Pydantic v2).

Regles structurelles imposees par le cahier des charges :
- AUCUN champ entry1/entry2/tp1/tp2 : listes de taille 1..N ;
- une strategie = un position_id ; plusieurs strategies par paire ;
- tout est serialisable en JSON UTF-8.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ==========================================================================
# Enums
# ==========================================================================


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PriceMode(str, Enum):
    """Comment l'utilisateur a saisi le niveau."""

    FIXED_PRICE = "FIXED_PRICE"
    PERCENT = "PERCENT"


class EntryReference(str, Enum):
    """Reference d'un ecart en % pour une Entry (section 28)."""

    ENTRY_1 = "ENTRY_1"
    PREVIOUS_ENTRY = "PREVIOUS_ENTRY"
    CURRENT_PRICE = "CURRENT_PRICE"


class TPReference(str, Enum):
    """Reference d'un TP (section 35)."""

    AVERAGE_PRICE = "AVERAGE_PRICE"
    ENTRY_1 = "ENTRY_1"
    CURRENT_PRICE_AT_CREATION = "CURRENT_PRICE_AT_CREATION"


class SLMode(str, Enum):
    """Mode de calcul du stop loss (section 8)."""

    FIXED_PRICE = "FIXED_PRICE"
    AVERAGE_PERCENT = "AVERAGE_PERCENT"
    ENTRY1_PERCENT = "ENTRY1_PERCENT"
    LAST_ENTRY_PERCENT = "LAST_ENTRY_PERCENT"


class SLTrigger(str, Enum):
    """Ce qui declenche le SL."""

    #: ordre STOP_LOSS_LIMIT sur Binance, declenche des que le prix touche le stop
    TOUCH = "TOUCH"
    #: aucun ordre stop sur Binance : le worker vend si une bougie cloture au stop ou dessous
    CANDLE_CLOSE = "CANDLE_CLOSE"


class SLRuleAfterTP(str, Enum):
    """Regle d'evolution du SL apres un TP (section 17)."""

    NO_CHANGE = "NO_CHANGE"
    BREAK_EVEN = "BREAK_EVEN"
    BREAK_EVEN_WITH_FEES = "BREAK_EVEN_WITH_FEES"
    PREVIOUS_TP = "PREVIOUS_TP"
    FIXED_PRICE = "FIXED_PRICE"
    CUSTOM_PERCENT = "CUSTOM_PERCENT"


class TPExecutionPolicy(str, Enum):
    """Type d'execution d'un TP declenche (section 15)."""

    MARKET_ON_TRIGGER = "MARKET_ON_TRIGGER"
    LIMIT_ON_TRIGGER = "LIMIT_ON_TRIGGER"
    LIMIT_GTC = "LIMIT_GTC"


class EntryStatus(str, Enum):
    PLANNED = "PLANNED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class TPStatus(str, Enum):
    PENDING = "PENDING"
    TRIGGERED = "TRIGGERED"
    SUBMITTED = "SUBMITTED"
    EXECUTED = "EXECUTED"
    PARTIALLY_EXECUTED = "PARTIALLY_EXECUTED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"


class SLStatus(str, Enum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    REPLACING = "REPLACING"
    EXECUTED = "EXECUTED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    NONE = "NONE"


class PositionStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_ENTRIES = "PENDING_ENTRIES"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    ERROR = "ERROR"

    @property
    def is_open(self) -> bool:
        return self in {
            PositionStatus.DRAFT,
            PositionStatus.PENDING_ENTRIES,
            PositionStatus.ACTIVE,
            PositionStatus.CLOSING,
        }


class SyncStatus(str, Enum):
    """Etat de reconciliation avec Binance (section 19)."""

    SYNCED = "SYNCED"
    DESYNC_DETECTED = "DESYNC_DETECTED"
    MANUAL_CHANGE = "MANUAL_CHANGE"
    RECONCILED = "RECONCILED"


class SignalSource(str, Enum):
    MANUAL = "manual"
    TELEGRAM = "telegram"
    TRADINGVIEW = "tradingview"
    API = "api"


class CloseReason(str, Enum):
    ALL_TP_HIT = "ALL_TP_HIT"
    SL_EXECUTED = "SL_EXECUTED"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    STOP_CROSSED = "STOP_CROSSED"
    SL_CANDLE_CLOSE = "SL_CANDLE_CLOSE"
    CANCELED_BEFORE_FILL = "CANCELED_BEFORE_FILL"
    ERROR = "ERROR"


class EventType(str, Enum):
    """Journal d'evenements (section 57)."""

    WORKER_STARTED = "WORKER_STARTED"
    WORKER_STOPPED = "WORKER_STOPPED"
    POSITION_CREATED = "POSITION_CREATED"
    ENTRY_CREATED = "ENTRY_CREATED"
    ENTRY_FILLED = "ENTRY_FILLED"
    ENTRY_PARTIAL = "ENTRY_PARTIAL"
    ENTRY_CANCELED = "ENTRY_CANCELED"
    ENTRY_EXPIRED = "ENTRY_EXPIRED"
    POSITION_UPDATED = "POSITION_UPDATED"
    TP_TRIGGERED = "TP_TRIGGERED"
    TP_EXECUTED = "TP_EXECUTED"
    SL_CREATED = "SL_CREATED"
    SL_MOVED = "SL_MOVED"
    SL_EXECUTED = "SL_EXECUTED"
    DESYNC_DETECTED = "DESYNC_DETECTED"
    MANUAL_CHANGE = "MANUAL_CHANGE"
    ERROR = "ERROR"
    POSITION_FINISHED = "POSITION_FINISHED"
    DRY_RUN_SKIP = "DRY_RUN_SKIP"
    SIMPLE_BUY = "SIMPLE_BUY"
    FEE_TOKEN_LOW = "FEE_TOKEN_LOW"
    FEE_TOKEN_RECOVERED = "FEE_TOKEN_RECOVERED"
    SIGNAL_AUTO_QUEUED = "SIGNAL_AUTO_QUEUED"
    SIGNAL_AUTO_REJECTED = "SIGNAL_AUTO_REJECTED"
    SIGNAL_REVIEW_REQUIRED = "SIGNAL_REVIEW_REQUIRED"
    SIGNAL_MANUAL_CONFIRMED = "SIGNAL_MANUAL_CONFIRMED"
    SIGNAL_ROUTING_CHANGED = "SIGNAL_ROUTING_CHANGED"
    MARKET_GUARD_STARTED = "MARKET_GUARD_STARTED"
    MARKET_GUARD_ENDED = "MARKET_GUARD_ENDED"
    DAILY_REPORT_SENT = "DAILY_REPORT_SENT"
    DAILY_LOSS_STARTED = "DAILY_LOSS_STARTED"
    DAILY_LOSS_ENDED = "DAILY_LOSS_ENDED"


class WorkerState(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    MONITORING = "MONITORING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


# ==========================================================================
# Base
# ==========================================================================


class BSMModel(BaseModel):
    """Base commune : enums serialises en valeur, mutation autorisee."""

    model_config = ConfigDict(use_enum_values=False, validate_assignment=False, allow_inf_nan=False)


# ==========================================================================
# Entry
# ==========================================================================


class Commission(BSMModel):
    asset: str = ""
    amount: float = 0.0


class Fill(BSMModel):
    """Une execution partielle remontee par Binance."""

    trade_id: Optional[int] = None
    price: float = 0.0
    qty: float = 0.0
    quote_qty: float = 0.0
    commission: float = 0.0
    commission_asset: str = ""
    time: Optional[datetime] = None


class Entry(BSMModel):
    entry_id: str = Field(default_factory=lambda: new_id("ent"))
    sequence_number: int = 1

    group_id: str = ""
    signal_id: str = ""
    source: SignalSource = SignalSource.MANUAL

    order_type: OrderType = OrderType.LIMIT
    price_mode: PriceMode = PriceMode.FIXED_PRICE
    reference_mode: EntryReference = EntryReference.ENTRY_1

    requested_price: Optional[float] = None
    requested_offset_percent: Optional[float] = None
    resolved_price: Optional[float] = None

    capital_percent: float = 0.0
    quote_amount: float = 0.0

    requested_qty: float = 0.0
    binance_qty: float = 0.0
    executed_qty: float = 0.0
    net_qty: float = 0.0

    status: EntryStatus = EntryStatus.PLANNED
    order_id: Optional[int] = None
    client_order_id: Optional[str] = None

    average_fill_price: float = 0.0
    quote_spent: float = 0.0
    commissions: list[Commission] = Field(default_factory=list)
    fills: list[Fill] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    last_error: str = ""

    # -- helpers --------------------------------------------------------

    @property
    def is_open_on_binance(self) -> bool:
        return self.status in {EntryStatus.SUBMITTED, EntryStatus.PARTIALLY_FILLED}

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            EntryStatus.FILLED,
            EntryStatus.CANCELED,
            EntryStatus.EXPIRED,
            EntryStatus.REJECTED,
        }

    @property
    def has_fills(self) -> bool:
        return self.executed_qty > 0

    def commission_total(self, asset: str = "") -> float:
        return sum(c.amount for c in self.commissions if not asset or c.asset == asset)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None or self.is_terminal:
            return False
        return utcnow() >= self.expires_at


# ==========================================================================
# Take Profit
# ==========================================================================


class TakeProfit(BSMModel):
    tp_id: str = Field(default_factory=lambda: new_id("tp"))
    sequence_number: int = 1

    price_mode: PriceMode = PriceMode.PERCENT
    reference_mode: TPReference = TPReference.AVERAGE_PRICE

    target_price: Optional[float] = None
    target_percent: Optional[float] = None

    sell_percent: float = 0.0
    estimated_qty: float = 0.0
    executed_qty: float = 0.0

    status: TPStatus = TPStatus.PENDING
    execution_policy: TPExecutionPolicy = TPExecutionPolicy.MARKET_ON_TRIGGER

    order_id: Optional[int] = None
    client_order_id: Optional[str] = None
    attempt_count: int = 0

    gain_estimated: float = 0.0
    gain_realized: float = 0.0
    average_fill_price: float = 0.0
    quote_received: float = 0.0
    commissions: list[Commission] = Field(default_factory=list)

    sl_rule_after_hit: SLRuleAfterTP = SLRuleAfterTP.NO_CHANGE
    sl_rule_value: Optional[float] = None

    triggered_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    last_error: str = ""

    @property
    def is_done(self) -> bool:
        return self.status in {TPStatus.EXECUTED, TPStatus.CANCELED}

    @property
    def is_pending(self) -> bool:
        return self.status in {
            TPStatus.PENDING,
            TPStatus.TRIGGERED,
            TPStatus.SUBMITTED,
            TPStatus.FAILED,
        }

    def commission_total(self, asset: str = "") -> float:
        return sum(c.amount for c in self.commissions if not asset or c.asset == asset)


# ==========================================================================
# Stop Loss
# ==========================================================================


class StopLoss(BSMModel):
    mode: SLMode = SLMode.AVERAGE_PERCENT
    value: float = -4.0

    resolved_price: Optional[float] = None
    quantity: float = 0.0

    status: SLStatus = SLStatus.PLANNED
    order_id: Optional[int] = None
    client_order_id: Optional[str] = None

    #: decalage du prix limite sous le stopPrice, en % (STOP_LOSS_LIMIT)
    limit_offset_percent: float = 0.3

    #: TOUCH : ordre stop Binance. CANDLE_CLOSE : cloture de bougie surveillee par le worker.
    trigger: SLTrigger = SLTrigger.TOUCH
    #: intervalle Binance de la bougie surveillee (15m, 1h, 4h...) en mode CANDLE_CLOSE
    candle_interval: str = ""
    #: closeTime (ms) de la derniere bougie cloturee deja evaluee
    candle_checked_until: Optional[int] = None

    executed_qty: float = 0.0
    average_fill_price: float = 0.0
    quote_received: float = 0.0
    commissions: list[Commission] = Field(default_factory=list)

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    replace_count: int = 0
    last_error: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == SLStatus.ACTIVE

    def commission_total(self, asset: str = "") -> float:
        return sum(c.amount for c in self.commissions if not asset or c.asset == asset)


# ==========================================================================
# Metriques, PnL, historique
# ==========================================================================


class PositionMetrics(BSMModel):
    net_qty: float = 0.0
    total_bought_qty: float = 0.0
    total_sold_qty: float = 0.0

    average_price: float = 0.0
    break_even_price: float = 0.0
    break_even_with_fees: float = 0.0

    capital_committed: float = 0.0
    capital_pending: float = 0.0
    capital_planned: float = 0.0

    current_price: float = 0.0
    position_value: float = 0.0

    commissions_quote: float = 0.0
    commissions_base: float = 0.0

    max_loss_at_sl: float = 0.0
    risk_percent_of_portfolio: float = 0.0

    updated_at: Optional[datetime] = None


class PositionPnL(BSMModel):
    complete: bool = True
    unpriced_fees: dict[str, float] = Field(default_factory=dict)
    unrealized: float = 0.0
    unrealized_percent: float = 0.0
    realized: float = 0.0
    total: float = 0.0
    fees_paid: float = 0.0
    updated_at: Optional[datetime] = None


class HistoryEvent(BSMModel):
    timestamp: datetime = Field(default_factory=utcnow)
    event_type: EventType = EventType.POSITION_UPDATED
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class SourceGroup(BSMModel):
    """Un lot d'Entries ajoute a la position (section 45)."""

    group_id: str = Field(default_factory=lambda: new_id("grp"))
    signal_id: str = ""
    source: SignalSource = SignalSource.MANUAL
    label: str = ""
    added_at: datetime = Field(default_factory=utcnow)
    entry_ids: list[str] = Field(default_factory=list)


class NotificationSettings(BSMModel):
    telegram: bool = False
    email: bool = False
    on_entry_filled: bool = True
    on_tp_executed: bool = True
    on_sl_moved: bool = True
    on_sl_executed: bool = True
    on_position_finished: bool = True
    on_error: bool = True


class AutomationSettings(BSMModel):
    enabled: bool = True
    tp_execution_policy: TPExecutionPolicy = TPExecutionPolicy.MARKET_ON_TRIGGER
    maintain_single_sl: bool = True
    cancel_remaining_entries_on_first_tp: bool = False
    #: Premier TP atteint AVANT tout achat : garder l'ordre d'achat ouvert (signaux texte, réglage
    #: « signal_cancel_entry_if_tp1_first » désactivé) au lieu de l'annuler ; la position est marquée
    #: « tp1_avant_achat » pour mesurer ces achats tardifs dans History.
    keep_entry_if_tp_before_fill: bool = False
    #: Tranche de TP sous les minimums Binance reportée sur le TP suivant (politiques CSI).
    merge_below_minimum_tp: bool = False
    paused: bool = False
    last_run_at: Optional[datetime] = None


class OcoExit(BSMModel):
    """Identifiants persistés des deux branches d'une sortie OCO Demo."""

    order_list_id: int
    list_client_order_id: str
    tp_order_id: int
    sl_order_id: int
    quantity: float
    status: str = "ACTIVE"
    missing_branch_alerted: bool = False


# ==========================================================================
# Position
# ==========================================================================


class ManualExit(BSMModel):
    client_order_id: str
    order_id: Optional[int] = None
    requested_qty: float = 0.0
    executed_qty: float = 0.0
    quote_received: float = 0.0
    average_fill_price: float = 0.0
    commissions: list[Commission] = Field(default_factory=list)
    status: str = "UNKNOWN"
    close_reason: CloseReason = CloseReason.MANUAL_CLOSE

    def commission_total(self, asset: str) -> float:
        return sum(c.amount for c in self.commissions if c.asset == asset)


class Position(BSMModel):
    revision: int = 0
    order_identity_version: int = Field(default=1, ge=1, le=2)
    position_id: str = Field(default_factory=lambda: new_id("pos"))
    symbol: str = ""
    base_asset: str = ""
    quote_asset: str = "USDT"

    status: PositionStatus = PositionStatus.DRAFT
    sync_status: SyncStatus = SyncStatus.SYNCED

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    closed_at: Optional[datetime] = None
    close_reason: Optional[CloseReason] = None

    entries: list[Entry] = Field(default_factory=list)
    take_profits: list[TakeProfit] = Field(default_factory=list)
    stop_loss: StopLoss = Field(default_factory=StopLoss)

    source_groups: list[SourceGroup] = Field(default_factory=list)

    metrics: PositionMetrics = Field(default_factory=PositionMetrics)
    pnl: PositionPnL = Field(default_factory=PositionPnL)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    automation: AutomationSettings = Field(default_factory=AutomationSettings)
    oco_exit: Optional[OcoExit] = None
    manual_exits: list[ManualExit] = Field(default_factory=list)
    history: list[HistoryEvent] = Field(default_factory=list)

    #: prix courant au moment de la creation (reference CURRENT_PRICE_AT_CREATION)
    creation_price: float = 0.0
    #: capital total prevu pour la position, en quote
    planned_capital: float = 0.0
    preset_name: str = ""
    tags: list[str] = Field(default_factory=list)
    environment: str = "DEMO"

    # -- acces ----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    def entry_by_id(self, entry_id: str) -> Optional[Entry]:
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    def tp_by_id(self, tp_id: str) -> Optional[TakeProfit]:
        return next((t for t in self.take_profits if t.tp_id == tp_id), None)

    @property
    def sorted_entries(self) -> list[Entry]:
        return sorted(self.entries, key=lambda e: e.sequence_number)

    @property
    def sorted_tps(self) -> list[TakeProfit]:
        return sorted(self.take_profits, key=lambda t: t.sequence_number)

    @property
    def first_entry(self) -> Optional[Entry]:
        entries = self.sorted_entries
        return entries[0] if entries else None

    @property
    def last_entry(self) -> Optional[Entry]:
        entries = self.sorted_entries
        return entries[-1] if entries else None

    @property
    def filled_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.executed_qty > 0]

    @property
    def open_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.is_open_on_binance]

    @property
    def pending_tps(self) -> list[TakeProfit]:
        return [t for t in self.sorted_tps if t.is_pending]

    @property
    def next_tp(self) -> Optional[TakeProfit]:
        pending = self.pending_tps
        return pending[0] if pending else None

    @property
    def executed_tps(self) -> list[TakeProfit]:
        return [t for t in self.sorted_tps if t.status == TPStatus.EXECUTED]

    @property
    def hit_tps(self) -> list[TakeProfit]:
        """TP atteints : ceux qui ont vendu quelque chose (EXECUTED ou PARTIALLY_EXECUTED).

        L'arrondi au stepSize laisse souvent une vente un peu sous la quantite estimee, marquee
        PARTIALLY_EXECUTED : ce TP a bien ete atteint (compteurs, stop « au TP precedent »).
        """
        return [t for t in self.sorted_tps if t.status == TPStatus.EXECUTED or t.executed_qty > 1e-12]

    # -- mutation -------------------------------------------------------

    def touch(self) -> None:
        self.updated_at = utcnow()

    def log(
        self,
        event_type: EventType,
        message: str = "",
        **data: Any,
    ) -> HistoryEvent:
        event = HistoryEvent(event_type=event_type, message=message, data=data)
        self.history.append(event)
        self.touch()
        return event

    def next_entry_sequence(self) -> int:
        return max((e.sequence_number for e in self.entries), default=0) + 1

    def next_tp_sequence(self) -> int:
        return max((t.sequence_number for t in self.take_profits), default=0) + 1


# ==========================================================================
# Runtime worker
# ==========================================================================


class BotRuntime(BSMModel):
    command_scope: str = ""
    command_capabilities: list[str] = Field(default_factory=list)
    state: WorkerState = WorkerState.STOPPED
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    loop_count: int = 0
    positions_monitored: int = 0
    environment: str = "DEMO"
    run_mode: str = "DRY_RUN"
    base_url: str = ""
    last_error: str = ""
    last_message: str = ""
    price_diagnostics: dict[str, Any] = Field(default_factory=dict)
    telegram_diagnostics: dict[str, Any] = Field(default_factory=dict)

    def is_alive(self, stale_after_seconds: int = 20) -> bool:
        if self.state in {WorkerState.STOPPED, WorkerState.ERROR}:
            return False
        if self.heartbeat_at is None:
            return False
        age = (utcnow() - self.heartbeat_at).total_seconds()
        return age <= stale_after_seconds

    def heartbeat_age(self) -> Optional[float]:
        if self.heartbeat_at is None:
            return None
        return (utcnow() - self.heartbeat_at).total_seconds()


# ==========================================================================
# Signal normalise
# ==========================================================================


class Signal(BSMModel):
    signal_id: str = Field(default_factory=lambda: new_id("sig"))
    source: SignalSource = SignalSource.MANUAL
    source_name: str = ""
    symbol: str = ""
    received_at: datetime = Field(default_factory=utcnow)

    entry_prices: list[float] = Field(default_factory=list)
    tp_prices: list[float] = Field(default_factory=list)
    sl_price: Optional[float] = None

    raw_text: str = ""
    external_id: str = ""
    content_hash: str = ""
    is_complete: bool = False
    validation_errors: list[str] = Field(default_factory=list)
