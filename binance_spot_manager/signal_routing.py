"""Routage d'un signal : exécution automatique Demo ou confirmation manuelle.

Règle du propriétaire : « la confirmation manuelle doit être pour les cas où il y a
un grand risque ou une confiance faible ». Lecture retenue : confirmation manuelle
obligatoire si le risque est élevé OU si la confiance est faible ou inconnue ;
exécution automatique sur Binance Demo seulement si l'automatisation est activée et
que le signal n'a AUCUN motif de revue. La confirmation manuelle n'est jamais retirée.

Trois issues, par ordre de priorité :

* REJECT : personne ne peut exécuter le signal (contrat violé : erreur d'analyse,
  version retirée, texte CSI hors dépôt TXT, EXPIRES_AT dépassé, écart d'entrée CSI) ;
* REVIEW (« À confirmer ») : au moins un motif de confiance, de risque ou de données ;
* AUTO : aucun motif.

La confiance est toujours une DÉCLARATION, jamais une mesure : statut DEMO_ELIGIBLE du
producteur CSI, groupe Telegram déclaré de confiance et actif validé par le
propriétaire. Le JSON v1 est toujours manuel. Les seuils de risque sont des garde-fous
de routage, non calibrés : ils décident qui confirme et ne disent rien du résultat.

Module pur : aucun appel réseau, aucun Streamlit. L'appelant lit soldes, prix,
positions et commandes puis les transmet ; aucune fonction publique de décision ne
lève d'exception vers le worker.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import re
from typing import Any, Iterable, Mapping

from .candle_stop import kline_interval
from .investment_plan import InvestmentPreview, investment_risk_context
from .models import EntryStatus, Position, SLStatus, SLTrigger
from .risk_engine import RiskEngine, RiskLimits
from .signal_parser import CsiSignalFormatError, read_csi_signal
from .wallet_valuation import conversion_rate

ROUTING_POLICY_VERSION = 1
#: Frais Binance de base par exécution, sans remise BNB (hypothèse prudente).
FEE_RATE = 0.001

AUTO, REVIEW, REJECT = "AUTO", "REVIEW", "REJECT"
CONFIANCE, RISQUE, DONNEES, CONTRAT = "CONFIANCE", "RISQUE", "DONNEES", "CONTRAT"
CATEGORY_LABELS = {CONFIANCE: "Confiance", RISQUE: "Risque", DONNEES: "Données", CONTRAT: "Contrat"}

KIND_CSI, KIND_TELEGRAM, KIND_JSON_V1, KIND_MANUAL = "CSI", "TELEGRAM", "JSON_V1", "MANUAL"
KIND_LABELS = {KIND_CSI: "CSI (dépôt TXT)", KIND_TELEGRAM: "Telegram", KIND_JSON_V1: "Dépôt JSON v1",
               KIND_MANUAL: "Collage manuel"}

#: Univers de base de CryptoSignalIntelligence : proposition pré-remplie, à valider
#: par le propriétaire (univers halal). Une liste enregistrée vide n'autorise AUCUN actif.
CSI_UNIVERSE_BASE_ASSETS = (
    "BTC", "ETH", "SOL", "XRP", "NEAR", "AVAX", "HBAR", "LINK",
    "XLM", "ADA", "TRX", "FIL", "ALGO", "DOT", "ATOM", "ETC",
)

#: Motifs qui renvoient en revue sans aucun appel Binance.
EARLY_REVIEW_CODES = frozenset({"C_DEMO_MANUAL", "C_STALE"})
#: États de commande qui n'ont envoyé aucun ordre (exclus du compteur de coupe-circuit).
NO_ORDER_STATES = frozenset({"FAILED", "EXPIRED", "CANCELED"})
RUN_MODES = frozenset({"DRY_RUN", "DEMO_MANUAL", "DEMO_AUTO"})
_TELEGRAM_EXTERNAL_ID = re.compile(r"^[0-9a-f]{64}:(-?\d{1,20}):\d{1,20}$")
_ASSET = re.compile(r"^[A-Z0-9]{1,20}$")


class RoutingDataError(ValueError):
    """Donnée nécessaire au routage absente ou incohérente : le signal part en revue."""


# ==========================================================================
# Motifs et décision
# ==========================================================================


@dataclass(frozen=True)
class Reason:
    code: str
    category: str
    message: str
    value: float | None = None
    threshold: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: _finite(value) if isinstance(value, float) else value
                for key, value in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Reason":
        return cls(code=str(data.get("code") or ""), category=str(data.get("category") or DONNEES),
                   message=str(data.get("message") or ""), value=data.get("value"),
                   threshold=data.get("threshold"))


@dataclass
class RouteDecision:
    outcome: str
    reasons: list[Reason] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def codes(self) -> list[str]:
        return [reason.code for reason in self.reasons]

    def to_dict(self) -> dict[str, Any]:
        return {"version": ROUTING_POLICY_VERSION, "outcome": self.outcome,
                "reasons": [reason.to_dict() for reason in self.reasons],
                "metrics": clean_metrics(self.metrics)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True)

    def summary_fr(self) -> str:
        if not self.reasons:
            return "Aucun motif de revue"
        return " · ".join(reason.message for reason in self.reasons)[:1000]


def decide(reasons: Iterable[Reason], metrics: Mapping[str, Any] | None = None, *,
           reject: str = "") -> RouteDecision:
    """REJECT l'emporte sur REVIEW, qui l'emporte sur AUTO."""
    reasons = list(reasons)
    metrics = dict(metrics or {})
    if reject:
        return RouteDecision(REJECT, [Reason("X_CONTRACT", CONTRAT, reject)] + reasons, metrics)
    return RouteDecision(REVIEW if reasons else AUTO, reasons, metrics)


def decision_from_json(text: str) -> RouteDecision | None:
    """Décision enregistrée sur une ligne de la boîte des signaux ; None si absente ou illisible."""
    if not text:
        return None
    try:
        data = json.loads(text)
        return RouteDecision(str(data.get("outcome") or REVIEW),
                             [Reason.from_dict(item) for item in data.get("reasons") or []],
                             dict(data.get("metrics") or {}))
    except (ValueError, TypeError, AttributeError):
        return None


def grouped_reasons(reasons: Iterable[Reason]) -> dict[str, list[Reason]]:
    groups: dict[str, list[Reason]] = {}
    for reason in reasons:
        groups.setdefault(reason.category, []).append(reason)
    return groups


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    return value


def clean_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Métriques sérialisables en JSON strict (aucun NaN ni infini), y compris dans les dicts imbriqués."""
    cleaned: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, float):
            cleaned[key] = _finite(value)
        elif isinstance(value, Mapping):
            cleaned[key] = clean_metrics(value)
        elif isinstance(value, (list, tuple)):
            cleaned[key] = [_finite(item) for item in value]
        else:
            cleaned[key] = value
    return cleaned


# ==========================================================================
# Politique (réglages bornés, jamais plus larges que les limites dures)
# ==========================================================================


def _number(values: Mapping[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(values.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and minimum <= value <= maximum else default


def _flag(values: Mapping[str, Any], key: str, default: bool) -> bool:
    value = values.get(key, default)
    return value if isinstance(value, bool) else default


def parse_chat_ids(value: Any) -> frozenset[int]:
    """Identifiants numériques de conversations (liste ou texte séparé par des virgules)."""
    items = value if isinstance(value, (list, tuple, set, frozenset)) else re.split(r"[,;\s]+", str(value or ""))
    chats = set()
    for item in items:
        text = str(item).strip()
        if re.fullmatch(r"-?\d{1,20}", text):
            chats.add(int(text))
    return frozenset(chats)


def parse_assets(value: Any) -> tuple[str, ...]:
    items = value if isinstance(value, (list, tuple, set, frozenset)) else re.split(r"[,;\s]+", str(value or ""))
    assets = []
    for item in items:
        text = str(item).strip().upper()
        if _ASSET.fullmatch(text) and text not in assets:
            assets.append(text)
    return tuple(assets)


@dataclass(frozen=True)
class RoutingPolicy:
    max_risk_percent: float = 0.5
    total_risk_share: float = 0.8
    min_stop_percent: float = 1.0
    max_stop_percent: float = 10.0
    marketable_gap_percent: float = 1.0
    same_asset_review: bool = True
    #: lien avec CSI, désactivé par défaut (Settings → Signaux → Liens avec CSI).
    csi_high_volatility_review: bool = False
    trusted_chats: frozenset[int] = frozenset()
    base_assets: tuple[str, ...] = CSI_UNIVERSE_BASE_ASSETS
    #: interrupteurs du propriétaire (désactivés par défaut) : toutes les conversations autorisées à la réception
    #: valent groupe de confiance ; tous les actifs sont acceptés en automatique (les autres contrôles restent).
    trust_all_chats: bool = False
    all_assets: bool = False
    max_auto_per_24h: int = 4
    daily_loss_percent: float = 2.0
    loss_streak: int = 3
    breaker_reset_at: float = 0.0
    honor_demo_manual: bool = True
    notify_review: bool = False
    max_age_minutes: float = 5.0
    touch_stop: bool = False
    max_total_risk_percent: float = 5.0

    @property
    def total_risk_threshold(self) -> float:
        return self.total_risk_share * self.max_total_risk_percent

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None, limits: RiskLimits | None = None) -> "RoutingPolicy":
        values = values if isinstance(values, Mapping) else {}
        limits = limits if isinstance(limits, RiskLimits) else RiskLimits()
        hard_risk = max(float(limits.max_risk_per_position_percent), 0.0)
        min_stop = _number(values, "signal_review_min_stop_percent", 1.0, 0.0, 50.0)
        max_stop = _number(values, "signal_review_max_stop_percent", 10.0, 0.1, 100.0)
        if min_stop >= max_stop:
            min_stop, max_stop = 1.0, 10.0
        allowed_chats = parse_chat_ids(values.get("signal_telegram_chats", ""))
        return cls(
            # Un réglage ne peut que resserrer la limite dure par position.
            max_risk_percent=min(_number(values, "signal_review_max_risk_percent", 0.5, 0.01, 100.0), hard_risk),
            total_risk_share=_number(values, "signal_review_total_risk_share", 0.8, 0.05, 1.0),
            min_stop_percent=min_stop,
            max_stop_percent=max_stop,
            marketable_gap_percent=_number(values, "signal_review_marketable_gap_percent", 1.0, 0.0, 10.0),
            same_asset_review=_flag(values, "signal_review_same_asset", True),
            csi_high_volatility_review=_flag(values, "signal_review_csi_high_volatility", False),
            # Groupe de confiance : uniquement parmi les conversations autorisées à la réception.
            trusted_chats=parse_chat_ids(values.get("signal_auto_trusted_chats", ())) & allowed_chats,
            base_assets=(parse_assets(values["signal_auto_base_assets"])
                         if "signal_auto_base_assets" in values else CSI_UNIVERSE_BASE_ASSETS),
            trust_all_chats=_flag(values, "signal_auto_trust_all_chats", False),
            all_assets=_flag(values, "signal_auto_all_assets", False),
            max_auto_per_24h=int(_number(values, "signal_auto_max_per_24h", 4, 0, 100)),
            daily_loss_percent=_number(values, "signal_auto_daily_loss_percent", 2.0, 0.01, 100.0),
            loss_streak=int(_number(values, "signal_auto_loss_streak", 3, 1, 50)),
            breaker_reset_at=_number(values, "signal_auto_breaker_reset_at", 0.0, 0.0, 1e11),
            honor_demo_manual=_flag(values, "signal_route_honor_demo_manual", True),
            notify_review=_flag(values, "signal_review_notify", False),
            max_age_minutes=_number(values, "signal_auto_max_age_minutes", 5, 1, 60),
            touch_stop=_flag(values, "signal_auto_touch_stop", False),
            max_total_risk_percent=float(limits.max_total_risk_percent),
        )

    def widened_by(self, other: "RoutingPolicy") -> list[str]:
        """Changements qui ÉLARGISSENT l'automatique en passant de self à other."""
        widened = []
        if not other.trusted_chats <= self.trusted_chats:
            widened.append("groupes de confiance ajoutés")
        if other.trust_all_chats and not self.trust_all_chats:
            widened.append("toutes les conversations autorisées de confiance")
        if not set(other.base_assets) <= set(self.base_assets):
            widened.append("actifs ajoutés")
        if other.all_assets and not self.all_assets:
            widened.append("tous les actifs acceptés")
        if self.honor_demo_manual and not other.honor_demo_manual:
            widened.append("DEMO_MANUAL n'impose plus la confirmation")
        for name, label, wider in (
            ("max_risk_percent", "risque au stop", lambda old, new: new > old),
            ("total_risk_share", "part du risque total", lambda old, new: new > old),
            ("min_stop_percent", "distance minimale du stop", lambda old, new: new < old),
            ("max_stop_percent", "distance maximale du stop", lambda old, new: new > old),
            ("marketable_gap_percent", "entrée déjà dépassée", lambda old, new: new > old),
            ("max_auto_per_24h", "ordres automatiques sur 24 h", lambda old, new: new > old),
            ("daily_loss_percent", "perte journalière", lambda old, new: new > old),
            ("loss_streak", "pertes consécutives", lambda old, new: new > old),
        ):
            if wider(getattr(self, name), getattr(other, name)):
                widened.append(label)
        if self.same_asset_review and not other.same_asset_review:
            widened.append("revue d'un actif déjà ouvert désactivée")
        if self.csi_high_volatility_review and not other.csi_high_volatility_review:
            widened.append("revue de la volatilité HIGH désactivée")
        return widened


# ==========================================================================
# Nature et confiance déclarée
# ==========================================================================


def signal_kind(row: Mapping[str, Any]) -> str:
    parsed = row.get("parsed") or {}
    if parsed.get("signal_version") == 3:
        return KIND_CSI
    source = row.get("source")
    if source == "telegram":
        return KIND_TELEGRAM
    if source == "api":
        return KIND_JSON_V1
    return KIND_MANUAL


def telegram_chat_id(external_id: Any) -> int | None:
    """Conversation d'un message Telegram (``<sha256 du bot>:<chat>:<message>``), sinon None."""
    match = _TELEGRAM_EXTERNAL_ID.fullmatch(str(external_id or ""))
    return int(match.group(1)) if match else None


def base_asset(symbol: str) -> str:
    symbol = str(symbol or "").upper()
    for quote in ("USDT", "USDC"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol


def unknown_candle_stop(timeframe: Any) -> bool:
    """SL « à la clôture » d'une bougie que le worker ne sait pas surveiller."""
    return bool(timeframe) and not kline_interval(str(timeframe))


def confidence_reasons(row: Mapping[str, Any], policy: RoutingPolicy, *, now: float,
                       run_mode: str | None) -> list[Reason]:
    """Motifs de confiance déclarée ; aucun appel réseau."""
    parsed = row.get("parsed") or {}
    kind = signal_kind(row)
    reasons: list[Reason] = []
    if run_mode not in RUN_MODES:
        return [Reason("C_DEMO_MANUAL", CONFIANCE,
                       "Mode d'exécution inconnu : confirmation manuelle obligatoire")]
    if policy.honor_demo_manual and run_mode == "DEMO_MANUAL":
        return [Reason("C_DEMO_MANUAL", CONFIANCE,
                       "Mode DEMO_MANUAL : chaque signal demande une confirmation manuelle")]
    if kind != KIND_CSI:
        stamp = float(row.get("source_timestamp") or 0)
        oldest = now - policy.max_age_minutes * 60
        if stamp <= 0 or stamp < oldest or stamp > now + 60:
            label = "Message Telegram" if kind == KIND_TELEGRAM else "Signal ML/dépôt"
            return [Reason("C_STALE", CONFIANCE,
                           f"{label} trop ancien ou non daté pour une exécution automatique "
                           f"(fenêtre {policy.max_age_minutes:g} min)",
                           value=round(max(now - stamp, 0) / 60, 1) if stamp > 0 else None,
                           threshold=policy.max_age_minutes)]
    if kind == KIND_CSI:
        status = parsed.get("validation_status") or "absent"
        if status != "DEMO_ELIGIBLE":
            reasons.append(Reason(
                "C_CSI_STATUS", CONFIANCE,
                f"Statut CSI {status} : seul DEMO_ELIGIBLE peut partir automatiquement "
                "(étiquette de la stratégie déclarée par le producteur, pas une mesure du signal)"))
    elif kind == KIND_TELEGRAM:
        chat = telegram_chat_id(row.get("external_id"))
        if chat is None:
            reasons.append(Reason("C_SOURCE_UNDECLARED", CONFIANCE,
                                  "Conversation Telegram illisible : source non déclarée"))
        elif chat not in policy.trusted_chats and not policy.trust_all_chats:
            reasons.append(Reason("C_SOURCE_UNDECLARED", CONFIANCE,
                                  "Groupe Telegram non déclaré de confiance (déclaration du propriétaire, "
                                  "pas une mesure)"))
    else:
        reasons.append(Reason("C_SOURCE_UNDECLARED", CONFIANCE,
                              "Dépôt JSON v1 : aucune source déclarée, confirmation manuelle systématique"))
    if kind != KIND_CSI and not policy.all_assets:
        asset = base_asset(parsed.get("symbol", ""))
        if not policy.base_assets:
            reasons.append(Reason("C_UNIVERSE", CONFIANCE,
                                  "Liste d'actifs validés vide : aucun actif autorisé en automatique"))
        elif asset not in policy.base_assets:
            reasons.append(Reason("C_UNIVERSE", CONFIANCE,
                                  f"Actif {asset or '?'} hors de la liste validée pour l'automatique "
                                  "(univers à valider par le propriétaire)"))
    if unknown_candle_stop(parsed.get("stop_timeframe")) and not policy.touch_stop:
        # Une bougie connue (15m, 1h, 4h…) est surveillée par le worker (candle_stop) ;
        # seule une bougie inconnue reste sans exécution possible hors interprétation au toucher.
        reasons.append(Reason("C_SL_CANDLE", CONFIANCE,
                              f"SL sur clôture de bougie inconnue ({parsed.get('stop_timeframe')}) sans "
                              "autorisation d'interprétation au toucher"))
    return reasons


# ==========================================================================
# Risque (données déjà lues par l'appelant)
# ==========================================================================


def _rate(asset: str, prices: Mapping[str, float]) -> float:
    rate = conversion_rate(asset, "USDT", prices)
    if rate is None or rate <= 0:
        raise RoutingDataError(f"Taux {asset}/USDT indisponible")
    return rate


def _requested_entries(position: Position, entry_ids: Iterable[str]) -> list:
    wanted = set(entry_ids)
    return [entry for entry in position.entries if entry.entry_id in wanted]


def signal_risk_report(position: Position, entry_ids: Iterable[str], *, price: float,
                       balances: Mapping[str, Any], prices: Mapping[str, float],
                       positions: list[Position], limits: RiskLimits):
    """Miroir ligne à ligne de CommandProcessor._submit_position (calcul du risque).

    Retourne (rapport RiskEngine, snapshot, taux devise→USDT, coût, perte au stop).
    Un test de parité compare le texte des refus à celui du worker.
    """
    cost, quantity, loss = 0.0, 0.0, 0.0
    for entry in _requested_entries(position, entry_ids):
        qty = float(entry.binance_qty)
        entry_price = price if entry.order_type.value == "MARKET" else float(entry.resolved_price or 0)
        if not (math.isfinite(qty) and qty > 0 and math.isfinite(entry_price) and entry_price > 0):
            raise RoutingDataError("Quantité ou prix d'entrée invalide")
        cost += qty * entry_price
        quantity += qty
        loss += qty * (entry_price if position.stop_loss.status is SLStatus.NONE
                       else max(entry_price - float(position.stop_loss.resolved_price or 0), 0))
    preview = InvestmentPreview(position.symbol, position.base_asset, position.quote_asset,
                                "QUEUED", quantity, cost, position.stop_loss.resolved_price or 0, loss, ())
    plan, snapshot, quote_rate = investment_risk_context(
        preview, capital=cost, reserve_percent=limits.min_reserve_percent,
        balances=balances, prices=prices, positions=positions,
    )
    report = RiskEngine(limits).evaluate(plan, snapshot, symbol=position.symbol)
    return report, snapshot, quote_rate, cost, loss


def loss_with_costs(entries: list[tuple[float, float]], stop: float | None, *,
                    offset_fraction: float, fee: float = FEE_RATE) -> float:
    """Perte au stop frais compris (hors gap).

    Σ q·(e − s) + f·Σ q·e + Q·s·(1 − (1 − o)(1 − f)) : frais d'achat, décalage du
    stop-limit o sous le stop et frais de vente. Sans stop : tout le notionnel.
    """
    notional = sum(qty * price for qty, price in entries)
    if not stop or stop <= 0:
        return notional * (1 + fee)
    quantity = sum(qty for qty, _ in entries)
    base = sum(qty * max(price - stop, 0.0) for qty, price in entries)
    return base + fee * notional + quantity * stop * (1 - (1 - offset_fraction) * (1 - fee))


def _position_resting_risk(position: Position, prices: Mapping[str, float]) -> float:
    """Risque des entrées encore au repos (non remplies), que le moteur compte à 0."""
    if not position.is_open:
        return 0.0
    stop = position.stop_loss.resolved_price if position.stop_loss.status is not SLStatus.NONE else None
    risk = 0.0
    for entry in position.entries:
        if entry.status not in {EntryStatus.PLANNED, EntryStatus.SUBMITTED, EntryStatus.PARTIALLY_FILLED}:
            continue
        remaining = max(float(entry.binance_qty) - float(entry.executed_qty), 0.0)
        price = float(entry.resolved_price or 0)
        risk += remaining * (max(price - stop, 0.0) if stop else price)
    return risk * _rate(position.quote_asset, prices) if risk > 0 else 0.0


def _payload_position(command: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = command.get("payload") or {}
    position = payload.get("position") if isinstance(payload, Mapping) else None
    return position if isinstance(position, Mapping) else {}


def _queued_risk(command: Mapping[str, Any], prices: Mapping[str, float]) -> float:
    position = _payload_position(command)
    wanted = set((command.get("payload") or {}).get("entry_ids") or [])
    stop = float(((position.get("stop_loss") or {}).get("resolved_price")) or 0)
    risk = 0.0
    for entry in position.get("entries") or []:
        if wanted and entry.get("entry_id") not in wanted:
            continue
        qty, price = float(entry.get("binance_qty") or 0), float(entry.get("resolved_price") or 0)
        risk += qty * (max(price - stop, 0.0) if stop > 0 else price)
    return risk * _rate(str(position.get("quote_asset") or "USDT"), prices) if risk > 0 else 0.0


def assess_risk(*, kind: str, payload: Mapping[str, Any], plan_average_price: float, current_price: float,
                balances: Mapping[str, Any], prices: Mapping[str, float], positions: list[Position],
                active_commands: list[Mapping[str, Any]], limits: RiskLimits, policy: RoutingPolicy,
                raw: str = "") -> tuple[list[Reason], dict[str, Any]]:
    """Critères R1 à R8 ; lève RoutingDataError si le risque n'est pas mesurable."""
    try:
        position = Position.model_validate(payload["position"])
    except Exception as exc:  # noqa: BLE001 - plan illisible = risque non mesurable
        raise RoutingDataError(f"Plan illisible : {exc}") from None
    entry_ids = list(payload.get("entry_ids") or [])
    try:
        report, snapshot, quote_rate, cost, plain_loss = signal_risk_report(
            position, entry_ids, price=current_price, balances=balances, prices=prices,
            positions=positions, limits=limits)
    except RoutingDataError:
        raise
    except ValueError as exc:
        raise RoutingDataError(str(exc)) from None
    total = snapshot.total_capital
    if not (math.isfinite(total) and total > 0):
        raise RoutingDataError("Portefeuille non valorisable")
    entries = [(float(e.binance_qty), float(e.resolved_price or 0))
               for e in _requested_entries(position, entry_ids)]
    stop = position.stop_loss.resolved_price if position.stop_loss.status is not SLStatus.NONE else None
    loss_stop = stop
    if stop and position.stop_loss.trigger is SLTrigger.CANDLE_CLOSE and position.stop_loss.binance_stop_price():
        # SL à la clôture de bougie : la vente peut se faire jusqu'au stop de secours, plus bas ; la perte au stop (R1)
        # se mesure là. Sans secours, le niveau nominal reste la seule référence chiffrable. La distance du stop (R6)
        # reste celle du signal.
        loss_stop = position.stop_loss.binance_stop_price()
    offset = float(position.stop_loss.limit_offset_percent) / 100.0
    risk_with_costs = loss_with_costs(entries, loss_stop, offset_fraction=offset) * quote_rate
    risk_pct = risk_with_costs / total * 100.0
    resting = sum(_position_resting_risk(p, prices) for p in positions if p.is_open)
    queued = sum(_queued_risk(command, prices) for command in active_commands)
    projected = (snapshot.total_risk_quote + resting + queued + risk_with_costs) / total * 100.0
    asset = base_asset(position.symbol)
    same_asset = sum(1 for p in positions if p.is_open and base_asset(p.symbol) == asset)
    same_asset += sum(1 for command in active_commands
                      if base_asset(str(_payload_position(command).get("symbol") or "")) == asset)
    quantity = sum(qty for qty, _ in entries)
    average = (sum(qty * price for qty, price in entries) / quantity) if quantity > 0 else plan_average_price
    effective = min(value for value in (average, plan_average_price, current_price) if value and value > 0)
    stop_distance = (effective - stop) / effective * 100.0 if stop and effective > 0 else 100.0
    lowest_entry = min((price for _, price in entries), default=0.0)
    entry_gap = max(0.0, (lowest_entry - current_price) / lowest_entry * 100.0) if lowest_entry > 0 else 0.0

    reasons: list[Reason] = []
    if risk_pct > policy.max_risk_percent:
        reasons.append(Reason("R1_RISK_AT_STOP", RISQUE,
                              f"Risque au stop frais compris {risk_pct:.2f} % du portefeuille > "
                              f"{policy.max_risk_percent:.2f} % (hors gap)",
                              value=risk_pct, threshold=policy.max_risk_percent))
    if report.refusals:
        reasons.append(Reason("R2_HARD_LIMIT", RISQUE,
                              "Limite dure : le worker refusera à ce budget — " + " ; ".join(report.refusals)))
    if report.warnings:
        reasons.append(Reason("R3_RISK_WARNING", RISQUE,
                              "Avertissement du moteur de risque : " + " ; ".join(report.warnings)))
    if projected > policy.total_risk_threshold:
        reasons.append(Reason("R4_TOTAL_RISK", RISQUE,
                              f"Risque total projeté {projected:.2f} % (entrées au repos et commandes en file "
                              f"comprises) > {policy.total_risk_threshold:.2f} %",
                              value=projected, threshold=policy.total_risk_threshold))
    if policy.same_asset_review and same_asset >= 1:
        reasons.append(Reason("R5_SAME_ASSET", RISQUE,
                              f"Actif {asset} déjà ouvert ou en file ({same_asset})", value=float(same_asset),
                              threshold=1.0))
    if not policy.min_stop_percent <= stop_distance <= policy.max_stop_percent:
        reasons.append(Reason("R6_STOP_DISTANCE", RISQUE,
                              f"Distance du stop {stop_distance:.2f} % hors de la plage "
                              f"[{policy.min_stop_percent:.2f} % ; {policy.max_stop_percent:.2f} %]",
                              value=stop_distance, threshold=policy.min_stop_percent
                              if stop_distance < policy.min_stop_percent else policy.max_stop_percent))
    if kind != KIND_CSI and entry_gap > policy.marketable_gap_percent:
        reasons.append(Reason("R7_ENTRY_MARKETABLE", RISQUE,
                              f"Entrée déjà dépassée : le cours est {entry_gap:.2f} % sous l'entrée la plus basse "
                              f"(> {policy.marketable_gap_percent:.2f} %), l'achat LIMIT partirait au marché",
                              value=entry_gap, threshold=policy.marketable_gap_percent))
    volatility = csi_volatility_regime(raw) if kind == KIND_CSI else ""
    if kind == KIND_CSI and policy.csi_high_volatility_review and volatility == "HIGH":
        reasons.append(Reason("R8_CSI_HIGH_VOLATILITY", RISQUE,
                              "Régime de volatilité CSI HIGH (étiquette descriptive du contexte)"))
    metrics = {
        "risk_pct_with_costs": risk_pct,
        "risk_pct_plain": report.planned_risk_percent,
        "projected_total_risk_pct": projected,
        "exposure_pct": report.planned_exposure_percent,
        "stop_distance_pct": stop_distance,
        "entry_gap_pct": entry_gap,
        "same_asset_count": same_asset,
        "total_capital_usdt": total,
        "cost_usdt": cost * quote_rate,
        "refusals": list(report.refusals),
        "warnings": list(report.warnings),
        "volatility_regime": volatility,
    }
    return reasons, metrics


def csi_volatility_regime(raw: str) -> str:
    """VOLATILITY_REGIME relu dans le texte CSI stocké (ParsedSignal ne le conserve pas)."""
    try:
        return str(read_csi_signal(raw or "").get("volatility_regime") or "")
    except (CsiSignalFormatError, ValueError):
        return ""


# ==========================================================================
# Coupe-circuits de l'automatique
# ==========================================================================


def _utc_midnight(now: float) -> float:
    day = datetime.fromtimestamp(now, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp()


def _closed_at(position: Position) -> float:
    return position.closed_at.timestamp() if position.closed_at else 0.0


def signal_row_id(position: Position) -> str:
    return position.tags[1] if len(position.tags) >= 2 and position.tags[0] == "signal" else ""


def breaker_reasons(*, policy: RoutingPolicy, now: float, auto_commands: list[Mapping[str, Any]],
                    positions: list[Position], prices: Mapping[str, float],
                    total_capital_usdt: float) -> tuple[list[Reason], dict[str, Any]]:
    """Coupe-circuits : ordres automatiques sur 24 h, perte du jour, pertes automatiques consécutives."""
    recent = [c for c in auto_commands
              if float(c.get("created_at") or 0) >= now - 86_400 and c.get("state") not in NO_ORDER_STATES]
    auto_ids = {str(c.get("request_key") or "").removeprefix("signal:") for c in auto_commands}
    midnight = _utc_midnight(now)
    day_result = 0.0
    for position in positions:
        if position.is_open or not signal_row_id(position) or _closed_at(position) < midnight:
            continue
        day_result += float(position.pnl.realized) * _rate(position.quote_asset, prices)
    day_pct = day_result / total_capital_usdt * 100.0 if total_capital_usdt > 0 else 0.0
    streak = 0
    automatic_closed = sorted(
        (p for p in positions if not p.is_open and signal_row_id(p) in auto_ids
         and _closed_at(p) > policy.breaker_reset_at),
        key=_closed_at, reverse=True,
    )
    for position in automatic_closed:
        if float(position.pnl.realized) < 0:
            streak += 1
        else:
            break
    reasons = []
    if len(recent) >= policy.max_auto_per_24h:
        reasons.append(Reason("R9_AUTO_COUNT", RISQUE,
                              f"Coupe-circuit : {len(recent)} ordre(s) automatique(s) sur 24 h "
                              f"(maximum {policy.max_auto_per_24h})",
                              value=float(len(recent)), threshold=float(policy.max_auto_per_24h)))
    if day_pct <= -policy.daily_loss_percent:
        reasons.append(Reason("R9_DAILY_LOSS", RISQUE,
                              f"Coupe-circuit : résultat réalisé des signaux depuis 00:00 UTC {day_pct:.2f} % "
                              f"≤ −{policy.daily_loss_percent:.2f} %",
                              value=day_pct, threshold=-policy.daily_loss_percent))
    if streak >= policy.loss_streak:
        reasons.append(Reason("R9_LOSS_STREAK", RISQUE,
                              f"Coupe-circuit : {streak} perte(s) automatique(s) consécutive(s) "
                              f"(maximum {policy.loss_streak}) — réarmer dans Settings",
                              value=float(streak), threshold=float(policy.loss_streak)))
    return reasons, {"auto_24h": len(recent), "day_result_pct": day_pct, "auto_loss_streak": streak}


# ==========================================================================
# Affichage
# ==========================================================================


def row_state_label(row: Mapping[str, Any], *, now: float) -> str:
    """État lisible d'une ligne de la boîte des signaux."""
    payload = row.get("payload")
    if payload:
        return "Envoyé auto" if payload.get("confirmation_mode") == "AUTO" else "Confirmé"
    parsed = row.get("parsed") or {}
    if parsed.get("errors"):
        return "Non exécutable"
    expires_at = float(row.get("expires_at") or parsed.get("expires_at") or 0)
    if expires_at > 0 and now >= expires_at:
        return "Expiré"
    return {"REVIEW": "À confirmer", "REJECTED": "Refusé auto", "PROCESSING": "En préparation",
            "QUEUED": "Envoyé auto"}.get(row.get("auto_state") or "", "Nouveau")


def is_pending_review(row: Mapping[str, Any], *, now: float) -> bool:
    return row_state_label(row, now=now) == "À confirmer"


def review_notification_text(*, symbol: str, kind: str, reasons: Iterable[Reason],
                             act_before: float | None = None) -> tuple[str, str]:
    """Titre et corps d'une notification « À confirmer » : aucun prix, aucune ligne ENTRY/TP/SL."""
    title = f"Signal à confirmer — {symbol or 'paire inconnue'}"
    lines = [f"Origine : {KIND_LABELS.get(kind, kind)}", "Motifs de revue :"]
    lines += [f"- {CATEGORY_LABELS.get(r.category, r.category)} : {r.message}" for r in reasons]
    if act_before:
        stamp = datetime.fromtimestamp(act_before, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"À confirmer avant {stamp} (après cette heure, le signal n'est plus exécutable).")
    lines.append("Examiner puis confirmer ou ignorer dans la page Signaux.")
    return title, "\n".join(lines)
