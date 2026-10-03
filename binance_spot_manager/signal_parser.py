"""Label-based signal reader. Parsing never submits orders or guesses missing prices.

Deux familles de formats coexistent, sans jamais se recouvrir :

* les signaux texte (Telegram, dépôt JSON v1), lus par leurs étiquettes (pair, entry, target, stop,
  platform), quel que soit l'habillage : émojis, filets, numérotation, pourcentages, flèches, barres.
  Les valeurs sont strictes : tout ce qui est ambigu est refusé, jamais deviné (deux prix là où un seul
  est attendu, « or market », virgule des milliers, « 90K », « 90000+ », deux paires, objectifs
  désordonnés, stop au-dessus des entrées…) ;
* le contrat TXT V3 de CryptoSignalIntelligence (``SIGNAL_VERSION=3`` en
  première ligne, ``CLE=VALEUR`` strict), traité par :func:`parse_csi_signal`.

Ce module ne dépend que de la bibliothèque standard : le test de contrat du
producteur le charge par chemin, sans importer le paquet.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

TEMPLATES = {"auto": "Automatique", "structured": "PAIR / ENTRY / T1 (Suhaib, Cleo)",
             "abk": "Coin / Entry Zone / Target (ABK)",
             "numbered": "#PAIRE / Entry1 / TP1 / Stop (Al-Mahwashi)",
             "simple": "Générique (étiquettes Entry / TP / SL)",
             "csi": "TXT V3 CryptoSignalIntelligence (SIGNAL_VERSION=3)"}
NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
UNVERIFIABLE_SOURCE_DATE_WARNING = (
    "Date source non vérifiable : contrôler manuellement la validité du signal."
)

LABELS = {
    "pair": r"PAIR|COIN|SYMBOL|ASSET|TOKEN|PAIRE",
    "platform": r"PLATFORM|PLATEFORME|EXCHANGE",
    "entry": (r"ENTRY(?:\s+(?:ZONE|PRICES?|RANGE|POINTS?|AREA|LEVELS?))?|ENTRIES|ENTR[EÉ]ES?"
              r"|BUY(?:\s+(?:ZONE|RANGE|PRICE|AREA|AROUND|AT|BETWEEN))?|ACHAT"),
    "target": r"TAKE\s*PROFITS?|TARGETS?|TGTS?|TPS?|T|OBJECTIFS?",
    "stop": r"STOP\s*[-_]?\s*LOSS|STOPLOSS|STOP|SL|S\s*/\s*L|INVALIDATION",
}
LABELLED_LINE = re.compile(
    "^(?:" + "|".join(f"(?P<{kind}>{pattern})" for kind, pattern in LABELS.items()) + r")(?![A-Z])"
    r"\s*(?P<index>\d{1,2})?(?![\d.])\s*(?::|=>|->|→|=|-|–|—|@|\))?\s*(?P<value>.*)$"
)
# Several labels on one line ("BTC/USDT Buy: 60000 TP: 62000 SL: 58000"): split before each one.
INLINE_LABEL = re.compile(r"\s(?=(?:" + "|".join(LABELS.values()) + r")(?![A-Z])\s*\d{0,2}\s*[:=])")
QUOTES = r"USDT|USDC|FDUSD|BUSD|USD|BTC|ETH|BNB|EUR|TRY"
SLASH_PAIR = re.compile(rf"(?<![A-Z0-9])([A-Z0-9]{{2,20}})(?:\s*/\s*|[-_])({QUOTES})(?![A-Z0-9])")
JOINED_PAIR = re.compile(r"(?<![A-Z0-9])((?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,20}?)(USDT|USDC)(?![A-Z0-9])")
TIMEFRAME = re.compile(r"(?<![\d.])(\d{1,3})\s*(MINUTES?|MINS?|M|HOURS?|HRS?|H|DAYS?|D|WEEKS?|W)(?![A-Z])")
# Words that change the meaning of a price: never ignored, always refused.
MEANING_CHANGERS = re.compile(r"\b(?:OR|OU|MARKET|CMP|NOW|CURRENT|ABOVE|BREAKOUT|BREAK|RETEST|DCA|UNTIL)\b")
CANDLE_CLOSE = re.compile(r"\b(?:CLOSES?|CLOSED|CLOSING|CANDLE|DAILY|WEEKLY|CL[OÔ]TURE)\b")
SHORT_SIGNAL = re.compile(
    r"\b(?:SHORT|LEVERAGE|FUTURES|PERP|PERPETUAL|MARGIN)\b"
    r"|(?:^|\n)[^A-Z0-9\n]*SELL\b|\b(?:DIRECTION|SIDE|POSITION|TYPE)\s*:?\s*SELL\b|\bSELL\s+(?:LIMIT|NOW|MARKET|ZONE)\b"
)
LIST_INDEX = re.compile(r"^\d{1,2}\s*(?:\)|[.:](?=\s))\s*")


@dataclass
class ParsedSignal:
    template: str = "unknown"
    symbol: str = ""
    direction: str = ""
    exchange: str = ""
    entries: list[float] = field(default_factory=list)
    targets: list[float] = field(default_factory=list)
    stop: float | None = None
    stop_timeframe: str = ""
    published_at: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Champs du contrat TXT CSI ; les valeurs par défaut gardent lisibles les
    # lignes analysées avant leur introduction (formats historiques : version 1).
    signal_version: int = 1
    signal_id: str = ""
    idempotency_key: str = ""
    #: Horodatages en temps Unix UTC (0 : non fournis par le format).
    decision_at: float = 0.0
    valid_from: float = 0.0
    #: Fin d'ACCEPTATION du message (réception, mise en file, envoi de l'entrée).
    expires_at: float = 0.0
    #: Fin de validité de l'ordre d'entrée non rempli.
    entry_expires_at: float = 0.0
    entry_count: int = 0
    rr_reference: str = ""
    max_entry_deviation_bps: float = 0.0
    tp_weights: list[float] = field(default_factory=list)
    exit_policy_id: str = ""
    exit_policy_hash: str = ""
    max_hold_minutes: int | None = None
    news_status: str = ""
    validation_status: str = ""

    def to_dict(self):
        return asdict(self)

    @property
    def is_csi(self) -> bool:
        """Signal du contrat TXT V3 de CryptoSignalIntelligence (seule version acceptée)."""
        return self.signal_version == 3


def normalize(text):
    text = unicodedata.normalize("NFKC", text).upper()
    text = re.sub(r"[0-9]️?⃣", "", text)
    text = text.replace("‎", "").replace("‏", "")
    return text.replace("**", "").replace("️", "")


def without_symbols(text):
    """Decorative emoji (📉, ✅, 🎯…) become spaces: a space never joins two numbers into one."""
    return "".join(" " if unicodedata.category(char) == "So" else char for char in text)


def content_hash(text):
    return hashlib.sha256(" ".join(normalize(text).split()).encode()).hexdigest()


def first_line_is_csi(raw: str) -> bool:
    """Vrai si la première ligne non vide annonce un contrat TXT CSI (toute version)."""
    for line in raw.splitlines():
        if line.strip():
            return line.strip().startswith("SIGNAL_VERSION=")
    return False


def _pairs(text):
    found = {base + quote for base, quote in SLASH_PAIR.findall(text)}
    return found | {base + quote for base, quote in JOINED_PAIR.findall(text)}


def _timeframe(match):
    return (match[1] + match[2]).lower()


# Prix lus dans une valeur : comme NUMBER, plus un entier suivi d'un point sans décimale (« Stop: 223. ») qui vaut
# 223, écriture fréquente des groupes ; « 1.442. », « 223.. » ou « 1.2.3 » restent refusés.
PRICE_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+|\d+\.(?![\d.]))"


def _read_prices(value, kind):
    """(prices, candle timeframe, error) for one value; any doubt is an error, never a guess."""
    if MEANING_CHANGERS.search(value):
        return [], "", "condition ou alternative (or, market, above…) non prise en charge"
    timeframe, notes = "", []
    text = re.sub(r"[(\[{]([^)\]}]*)[)\]}]", lambda m: notes.append(m[1]) or " ", value)
    for note in notes:
        if "%" in note:
            continue
        found = TIMEFRAME.search(note)
        if found:
            timeframe = timeframe or _timeframe(found)
        elif re.search(r"\d", note):
            return [], "", "chiffre inattendu entre parenthèses"
    text = SLASH_PAIR.sub(" ", JOINED_PAIR.sub(" ", text))
    text = re.sub(r"\b(?:USDT|USDC|USD)\b|\$", " ", text)
    text = re.sub(rf"[+-]?\s*{NUMBER}\s*%", " ", text)
    if kind == "stop":
        found = TIMEFRAME.search(text)
        timeframe = timeframe or (_timeframe(found) if found else "")
        text = TIMEFRAME.sub(" ", text)
        close = CANDLE_CLOSE.search(value)
        if close and not timeframe:
            timeframe = {"DAILY": "1d", "WEEKLY": "1w"}.get(close[0], "bougie")
    else:
        timeframe = ""
    if re.search(r"\d\s*,\d|\d\s*\+|\d[A-Z]|^\s*[-−]\s*\.?\d", text):
        return [], "", "prix ambigu (virgule, +, suffixe ou signe négatif)"
    numbers = re.findall(rf"(?<![\d.]){PRICE_NUMBER}(?![\d.])", text)
    if re.search(r"\d", re.sub(rf"(?<![\d.]){PRICE_NUMBER}(?![\d.])", " ", text)):
        return [], "", "prix mal formé"
    return [float(n) for n in numbers], timeframe, ""


def parse_signal(raw: str, template: str = "auto") -> ParsedSignal:
    result = ParsedSignal()
    if not raw.strip() or len(raw) > 20000:
        result.errors.append("Texte vide ou trop long (20 000 caractères maximum).")
        return result
    if first_line_is_csi(raw):
        # Contrat CSI (toute version) : jamais interprété par les modèles texte historiques.
        result = parse_csi_signal(raw)
        if template not in {"auto", "csi"}:
            result.errors.append("Le texte ne correspond pas au modèle sélectionné.")
        return result
    if template == "csi":
        result.errors.append("Le texte ne correspond pas au modèle sélectionné (SIGNAL_VERSION=3 attendu en première ligne).")
        return result
    if re.search(r"^\s*SIGNAL_VERSION=", raw, re.M):
        result.errors.append("Ligne SIGNAL_VERSION hors première ligne : contrat CSI mal formé, jamais lu comme un modèle texte.")
        return result
    # Not in normalize(): content_hash must stay stable for signals already stored.
    text = without_symbols(normalize(raw))
    lines = [re.sub(r"^[^A-Z0-9#]+", "", part.strip())
             for line in text.replace("|", "\n").splitlines() for part in INLINE_LABEL.split(line)]
    clean = "\n".join(lines)
    detected = ("structured" if re.search(r"^PAIR\s*:", clean, re.M) else
                "abk" if re.search(r"^COIN\s*:", clean, re.M) else
                "numbered" if (re.search(r"^#[A-Z0-9]+\s*/\s*(?:USDT|USDC)\s*$", clean, re.M)
                               and re.search(r"^ENTRY\s*1\s*:", clean, re.M)
                               and re.search(r"^TP\s*1\s*:", clean, re.M)
                               and re.search(r"^STOP\s*:", clean, re.M)) else "simple")
    result.template = detected
    if template not in TEMPLATES or template not in {"auto", detected}:
        result.errors.append("Le texte ne correspond pas au modèle sélectionné.")
    if re.search(r"\b(?:NIFTY|BANKNIFTY|INTRADAY)\b", text):
        result.errors.append("Rapport de marché / indices : pas un signal Spot exécutable.")
    # Spot can only buy: BUY is implied unless a short is announced; a short without the word
    # still fails the SL < entries < TP geometry below.
    if SHORT_SIGNAL.search(clean):
        result.direction = "SELL"
        result.errors.append("Short, vente initiale et levier non pris en charge en Spot.")
    else:
        result.direction = "BUY"

    keys = {"entry": [], "target": []}
    stops, pairs, platforms = [], _pairs(clean), []
    section, list_items = None, 0
    for line in lines:
        if not line:
            continue
        labelled = LABELLED_LINE.match(line)
        if labelled:
            kind = next(k for k in LABELS if labelled[k])
            index, value = labelled["index"], labelled["value"].strip()
            section = None
            if kind == "pair":
                named = re.match(r"[#$]?\s*([A-Z0-9]{2,20})(?:\s*/\s*|[-_\s]?)([A-Z]{3,5})?(?![A-Z0-9])", value)
                if named:
                    pairs.add(named[1] + (named[2] or ""))
                continue
            if kind == "platform":
                platforms.append((value.split() or [""])[0])
                continue
            if index is None and not re.search(r"\d", SLASH_PAIR.sub(" ", JOINED_PAIR.sub(" ", value))):
                if MEANING_CHANGERS.search(value):
                    result.errors.append(f"Valeur non prise en charge : « {line.strip()} ».")
                section = kind  # header ("TARGETS:", "ENTRY ZONE:"): bare prices may follow
                continue
            if not value:
                result.errors.append(f"Ligne sans prix : « {line.strip()} ».")
                continue
            source = [(index or "single", value)]
        elif section and re.match(r"\.?\d", LIST_INDEX.sub("", line)):
            list_items += 1
            kind, source = section, [(f"list{list_items}", LIST_INDEX.sub("", line))]
        else:
            section = None
            continue
        for key, value in source:
            prices, timeframe, error = _read_prices(value, kind)
            if not error and not prices:
                error = "aucun prix (pourcentage seul ou texte)"
            if not error and len(prices) > 1 and (kind == "stop" or (kind == "target" and key.isdigit())):
                error = "plusieurs prix pour un seul niveau"
            label = {"entry": "Prix d'entrée", "target": "Objectif", "stop": "Stop loss"}[kind]
            if error:
                result.errors.append(f"{label} ambigu ou non pris en charge ({error}) : « {line.strip()} ».")
                continue
            if kind == "stop":
                stops.extend(prices)
                result.stop_timeframe = timeframe
            else:
                keys[kind].append(key)
                (result.entries if kind == "entry" else result.targets).extend(prices)

    if len(pairs) != 1:
        result.errors.append("Une seule paire explicite est requise ; aucune devise n'est ajoutée automatiquement.")
    else:
        result.symbol = pairs.pop()
        if not re.fullmatch(r"[A-Z0-9]{2,20}(?:USDT|USDC)", result.symbol):
            result.errors.append("Seules les paires Spot USDT/USDC sont prises en charge.")
    result.exchange = platforms[0] if platforms else ""
    if any(platform != "BINANCE" for platform in platforms):
        result.errors.append("Plateforme autre que Binance : aucune conversion automatique.")
    if not platforms:
        result.warnings.append("Plateforme absente : la paire devra être vérifiée sur Binance Demo.")
    indexed_entries, indexed_targets = keys["entry"], keys["target"]
    if len(set(indexed_entries)) != len(indexed_entries) or len(set(indexed_targets)) != len(indexed_targets):
        result.errors.append("Indices d'entrée/objectif répétés : séparer les signaux.")
    for indices in (indexed_entries, indexed_targets):
        if indices and all(i.isdigit() for i in indices) and [int(i) for i in indices] != list(range(1, len(indices) + 1)):
            result.errors.append("Numérotation discontinue ou désordonnée.")
    if len(stops) == 1:
        result.stop = stops[0]
    else:
        result.errors.append("Un seul stop loss explicite est requis.")
    if not 1 <= len(result.entries) <= 20 or not 1 <= len(result.targets) <= 20:
        result.errors.append("Il faut entre 1 et 20 entrées et objectifs explicites.")
    values = result.entries + result.targets + stops
    if any(not math.isfinite(v) or v <= 0 for v in values):
        result.errors.append("Prix nul, négatif ou non fini.")
    if result.direction == "BUY" and result.entries and result.targets and result.stop is not None:
        if result.stop >= min(result.entries) or min(result.targets) <= max(result.entries):
            result.errors.append("Achat incohérent : SL < toutes les entrées < tous les TP requis.")
        if result.targets != sorted(set(result.targets)):
            result.errors.append("Les TP doivent être strictement croissants.")
    if result.stop_timeframe:
        result.warnings.append(f"SL ({result.stop_timeframe}) : clôture de bougie possible, jamais assimilée automatiquement à un stop au toucher.")
    date = re.search(r"\b(20\d\d-\d\d-\d\d)\b", text)
    hour = re.search(r"(\d{1,2}):(\d{2})\s*(?:GMT|UTC)\s*([+-]\d{1,2})?\b", text)
    if date and hour:
        try:
            stamp = datetime.fromisoformat(date[1]).replace(hour=int(hour[1]), minute=int(hour[2]),
                         tzinfo=timezone(timedelta(hours=int(hour[3] or 0))))
            result.published_at = stamp.astimezone(timezone.utc).isoformat()
        except ValueError:
            result.errors.append("Date du signal invalide.")
    else:
        result.warnings.append(UNVERIFIABLE_SOURCE_DATE_WARNING)
    result.errors = list(dict.fromkeys(result.errors))
    return result


# ==========================================================================
# Contrat TXT V3 (CryptoSignalIntelligence, docs/SIGNAL_FORMAT.md)
# ==========================================================================

CSI_VERSION = 3
CSI_ANALYSIS_MARKER = "---ANALYSIS---"
CSI_NONE = "NONE"
CSI_MAX_TP = 4
CSI_MAX_ENTRIES = 2
CSI_RR_QUANTUM = Decimal("0.001")
_CSI_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CSI_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CSI_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
_CSI_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")
_CSI_TOKEN = re.compile(r"^[A-Z0-9_]{1,60}$")
_CSI_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}(USDT|USDC)$")
_CSI_HASH = re.compile(r"^[0-9a-f]{16}$")

#: Clés du contrat et leur type, dans l'ordre canonique du producteur (signals/txt.py).
#: ``?`` : NONE autorisé ; ``decimals`` : liste séparée par des virgules.
CSI_FIELDS: tuple[tuple[str, str], ...] = (
    ("SIGNAL_VERSION", "int"), ("SIGNAL_ID", "str"), ("IDEMPOTENCY_KEY", "str"),
    ("DATA_AS_OF", "time"), ("DECISION_AT", "time"), ("CREATED_AT", "time"), ("VALID_FROM", "time"),
    ("EXPIRES_AT", "time"), ("ENTRY_EXPIRES_AT", "time"),
    ("MARKET_DATA_SOURCE", "str"), ("ENVIRONMENT", "str"), ("MARKET_TYPE", "str"),
    ("SYMBOL", "str"), ("ACTION", "str"), ("STRATEGY", "str"), ("STRATEGY_VERSION", "int"),
    ("TIMEFRAME_SETUP", "str"), ("ENTRY_MODE", "str"), ("ENTRY_COUNT", "int"), ("ENTRY_1", "decimal"),
    ("ENTRY_2", "decimal?"), ("ENTRY_WEIGHTS", "decimals"), ("WEIGHT_BASIS", "str"), ("STOP_LOSS", "decimal"),
    ("TP_COUNT", "int"), ("TP_1", "decimal"), ("TP_2", "decimal?"), ("TP_3", "decimal?"), ("TP_4", "decimal?"),
    ("TP_WEIGHTS", "decimals"), ("EXIT_POLICY_ID", "str"), ("EXIT_POLICY_HASH", "str"),
    ("MAX_HOLD_MINUTES", "int?"), ("RR_REFERENCE", "str"),
    ("RR_TP1_GROSS", "decimal"), ("RR_TP2_GROSS", "decimal?"), ("RR_TP3_GROSS", "decimal?"),
    ("RR_TP4_GROSS", "decimal?"), ("TECHNICAL_SCORE", "decimal?"), ("ML_PROBABILITY", "decimal?"),
    ("MODEL_ID", "str?"), ("ML_TARGET_ID", "str?"), ("ML_HORIZON_MINUTES", "int?"), ("ML_CALIBRATION_ID", "str?"),
    ("TREND_REGIME", "str"), ("VOLATILITY_REGIME", "str"), ("NEWS_STATUS", "str"),
    ("MAX_ENTRY_DEVIATION_BPS", "decimal"), ("VALIDATION_STATUS", "str"), ("INTEGRATION_STATUS", "str"),
    ("STATUS", "str"),
)
CSI_KINDS = dict(CSI_FIELDS)
#: Clés facultatives à la lecture (le producteur les écrit toujours).
CSI_OPTIONAL_KEYS = frozenset({"INTEGRATION_STATUS"})
#: Énumérations fermées du contrat ; toute autre valeur bloque le signal.
CSI_ENUMS = {
    "ENVIRONMENT": {"DEMO"},
    "MARKET_TYPE": {"SPOT"},
    "ACTION": {"BUY"},
    "ENTRY_MODE": {"LIMIT"},
    "WEIGHT_BASIS": {"BASE_QUANTITY", "QUOTE_BUDGET"},
    "RR_REFERENCE": {"ENTRY_1", "WEIGHTED_ENTRY"},
    "TIMEFRAME_SETUP": {"5m", "15m", "1h", "4h"},
    "TREND_REGIME": {"BULL", "BEAR", "RANGE", "UNKNOWN"},
    "VOLATILITY_REGIME": {"LOW", "NORMAL", "HIGH", "UNKNOWN"},
    "NEWS_STATUS": {"OFF", "OBSERVE", "GATE_CLEAR"},
    "VALIDATION_STATUS": {"RESEARCH", "VALIDATED_OOS", "SHADOW", "DEMO_ELIGIBLE", "SCHEMA_EXAMPLE_ONLY"},
    "INTEGRATION_STATUS": {"INTEGRATION_UNVERIFIED", "INTEGRATION_VERIFIED"},
    "STATUS": {"NEW"},
}

# --------------------------------------------------------------------------
# Politiques de sortie (registre partagé avec le backtest du producteur)
# --------------------------------------------------------------------------

#: Copie du registre du producteur (config/exit_policies.json), comparée par les tests.
CSI_POLICY_REGISTRY = Path(__file__).with_name("csi_exit_policies.json")


def _bsm_policy(policy_id: str, stop_rule: str) -> dict:
    """Règles que BinanceSpotManager applique réellement (vérifiées dans son code).

    TP vendu AU MARCHÉ dès que le dernier prix atteint le niveau (automation_engine),
    stop STOP_LOSS_LIMIT à 30 points de base sous le stop (StopLoss.limit_offset_percent),
    vendu au marché si Binance le refuse parce que le prix l'a déjà franchi
    (STOP_LIMIT_MARKET_IF_CROSSED, bot_worker._exit_on_crossed_stop), aucune sortie
    temporelle, stop déplacé après confirmation du fill du TP, parts de TP appliquées
    à la quantité NETTE achetée (frais payés en actif de base déduits), dernier TP =
    restant, tranche sous les minimums reportée sur le TP suivant, entrée annulée à
    ENTRY_EXPIRES_AT ou au premier TP, une seule entrée. Break-even : prix moyen
    d'achat réel, posé après le premier TP effectivement rempli (y compris un report).
    """
    return {
        "policy_id": policy_id, "stop_rule": stop_rule, "time_exit": False,
        "tp_execution": "MARKET_ON_TRIGGER", "stop_order": "STOP_LIMIT_MARKET_IF_CROSSED", "stop_limit_offset_bps": 30,
        "stop_move_applies": "AFTER_TP_FILL_CONFIRMED", "tp_weight_basis": "NET_FILLED_BASE_QUANTITY",
        "remainder_rule": "LAST_TP_SELLS_REMAINDER", "below_minimum_rule": "MERGE_INTO_NEXT_TP",
        "unfilled_entries_rule": "CANCEL_AT_ENTRY_EXPIRY_OR_FIRST_TP", "max_entries": 1, "schema_version": 1,
    }


def exit_policy_hash(rules: dict) -> str:
    """Empreinte du producteur : sha256(JSON canonique, clés triées, sans espaces)[:16]."""
    payload = json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:16]


#: Seules politiques acceptées : celles que BSM exécute réellement, identifiant ET empreinte.
#: Les V1 (stop STOP_LIMIT seul, poids sur la quantité brute, break-even après TP1) ne
#: décrivent pas exactement BSM : elles sont refusées comme toute autre politique.
BSM_EXIT_POLICIES = {
    "BSM_MARKET_TP_FIXED_SL_V2": _bsm_policy("BSM_MARKET_TP_FIXED_SL_V2", "FIXED"),
    "BSM_MARKET_TP_BREAK_EVEN_V2": _bsm_policy("BSM_MARKET_TP_BREAK_EVEN_V2", "BREAK_EVEN_AVG_FILL_AFTER_FIRST_TP"),
}
BSM_EXIT_POLICY_HASHES = {policy_id: exit_policy_hash(rules) for policy_id, rules in BSM_EXIT_POLICIES.items()}


def csi_registry_policy_ids() -> set[str]:
    """Politiques connues du registre copié (diagnostic seulement ; jamais une autorisation)."""
    try:
        return set(json.loads(CSI_POLICY_REGISTRY.read_text(encoding="utf-8"))["policies"])
    except (OSError, ValueError, KeyError, TypeError):
        return set()


class CsiSignalFormatError(ValueError):
    """Texte non conforme au contrat TXT V3 (lecture arrêtée à la première anomalie)."""


def csi_gross_rr(reference: Decimal, stop: Decimal, target: Decimal) -> Decimal:
    """RR brut recalculé comme chez le producteur : (TP - référence) / (référence - STOP), 3 décimales."""
    risk = reference - stop
    if risk <= 0:
        raise CsiSignalFormatError("risque nul ou négatif : STOP_LOSS doit être sous le prix de référence")
    return ((target - reference) / risk).quantize(CSI_RR_QUANTUM, rounding=ROUND_HALF_EVEN)


def csi_reference_price(entries, weights, basis: str, reference: str) -> Decimal:
    """Prix de référence des RR (schema.reference_price du producteur).

    ENTRY_1, ou prix moyen prévu si toutes les entrées étaient remplies :
    moyenne arithmétique pondérée (BASE_QUANTITY) ou harmonique (QUOTE_BUDGET).
    """
    if reference == "ENTRY_1":
        return entries[0]
    if basis == "BASE_QUANTITY":
        return sum((w * p for w, p in zip(weights, entries)), Decimal(0))
    return 1 / sum((w / p for w, p in zip(weights, entries)), Decimal(0))


def _csi_parse_value(raw: str, kind: str, key: str):
    if raw == CSI_NONE:
        if not kind.endswith("?"):
            raise CsiSignalFormatError(f"{key} est obligatoire (NONE interdit)")
        return None
    base = kind.rstrip("?")
    if base == "time":
        if not _CSI_TIME.fullmatch(raw):
            raise CsiSignalFormatError(f"{key} : format YYYY-MM-DDTHH:MM:SSZ requis")
        try:
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            raise CsiSignalFormatError(f"{key} : date invalide") from None
    if base == "int":
        if not re.fullmatch(r"\d+", raw):
            raise CsiSignalFormatError(f"{key} : entier requis")
        return int(raw)
    if base in {"decimal", "decimals"}:
        parts = raw.split(",") if base == "decimals" else [raw]
        values = []
        for part in parts:
            if not _CSI_DECIMAL.fullmatch(part):
                raise CsiSignalFormatError(f"{key} : nombre décimal à point requis (reçu {part!r})")
            values.append(Decimal(part))
        return tuple(values) if base == "decimals" else values[0]
    return raw


def read_csi_signal(text: str) -> dict:
    """Lecture stricte du contrat V3 ; lève CsiSignalFormatError à la première anomalie.

    Retourne les valeurs typées par clé en minuscules. La prose qui suit
    ``---ANALYSIS---`` n'est pas lue : elle ne peut pas modifier le contrat.
    La version est contrôlée dès la première ligne : la V2 (jamais consommée
    en production) et toute version future sont refusées.
    """
    if "\r" in text.replace("\r\n", "\n"):
        raise CsiSignalFormatError("fin de ligne invalide (retour chariot isolé)")
    contract: dict[str, str] = {}
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if line == CSI_ANALYSIS_MARKER:
            break
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        if not separator or not _CSI_KEY.fullmatch(key):
            raise CsiSignalFormatError(f"ligne {number} : format CLE=VALEUR requis")
        if not value or value != value.strip():
            raise CsiSignalFormatError(f"ligne {number} : valeur vide ou entourée d'espaces")
        if not contract:
            if key != "SIGNAL_VERSION":
                raise CsiSignalFormatError("SIGNAL_VERSION doit être la première clé")
            if value == "2":
                raise CsiSignalFormatError("SIGNAL_VERSION=2 : version 2 retirée, contrat V3 requis")
            if value != str(CSI_VERSION):
                raise CsiSignalFormatError(
                    f"version non prise en charge : {value!r} ({CSI_VERSION} attendu)")
        if key in contract:
            raise CsiSignalFormatError(f"clé dupliquée : {key}")
        if key not in CSI_KINDS:
            raise CsiSignalFormatError(f"clé inconnue pour la version {CSI_VERSION} : {key}")
        contract[key] = value
    if not contract:
        raise CsiSignalFormatError("texte vide")
    missing = [key for key, _ in CSI_FIELDS if key not in contract and key not in CSI_OPTIONAL_KEYS]
    if missing:
        raise CsiSignalFormatError("clés manquantes : " + ", ".join(missing))
    values = {key.lower(): _csi_parse_value(raw, CSI_KINDS[key], key) for key, raw in contract.items()}
    values.setdefault("integration_status", "INTEGRATION_UNVERIFIED")
    return values


def _check_exit_policy(values: dict, errors: list[str]) -> None:
    """Point 13 : BSM n'accepte que les politiques qu'il exécute, identifiant ET empreinte."""
    policy_id, given = values["exit_policy_id"], values["exit_policy_hash"]
    rules = BSM_EXIT_POLICIES.get(policy_id)
    if rules is None:
        known = "connue du registre CSI mais " if policy_id in csi_registry_policy_ids() else "inconnue, "
        errors.append(f"EXIT_POLICY_ID {policy_id} : politique {known}non exécutée par BinanceSpotManager "
                      f"(acceptées : {', '.join(sorted(BSM_EXIT_POLICIES))}).")
        return
    expected = BSM_EXIT_POLICY_HASHES[policy_id]
    if given != expected:
        errors.append(f"EXIT_POLICY_HASH attendu {expected} pour {policy_id}, reçu {given} (règles différentes).")
    if values["entry_count"] > rules["max_entries"]:
        errors.append(f"EXIT_POLICY_ID {policy_id} gère au plus {rules['max_entries']} entrée(s) "
                      f"(ENTRY_COUNT={values['entry_count']}).")
    if rules["time_exit"] != (values["max_hold_minutes"] is not None):
        errors.append(f"MAX_HOLD_MINUTES requis si et seulement si EXIT_POLICY_ID {policy_id} "
                      "prévoit une sortie temporelle (aucune ici : NONE requis).")


def parse_csi_signal(raw: str) -> ParsedSignal:
    """Contrat TXT V3 → ParsedSignal ; toute anomalie devient une erreur bloquante.

    Reproduit les règles du modèle producteur (signals/schema.py) : énumérations
    fermées, DEMO et Spot uniquement, achat LIMIT, une ou deux entrées ordonnées,
    poids sommant à 1 (tolérance 1e-9), TP strictement croissants, RR recalculés
    sur le prix de référence, DATA_AS_OF <= DECISION_AT <= CREATED_AT <=
    VALID_FROM < EXPIRES_AT <= ENTRY_EXPIRES_AT, champs ML ensemble ; puis
    n'accepte que les politiques de sortie réellement exécutées par BSM.
    """
    result = ParsedSignal(template="csi", signal_version=CSI_VERSION, direction="BUY")
    try:
        values = read_csi_signal(raw)
    except CsiSignalFormatError as exc:
        result.errors.append(f"Contrat CSI : {exc}.")
        return result
    errors = result.errors
    for key in ("signal_id", "idempotency_key"):
        if not _CSI_ID.fullmatch(values[key]):
            errors.append(f"{key.upper()} : identifiant invalide (1 à 160 caractères [A-Za-z0-9_.:-]).")
    for key in ("market_data_source", "strategy", "exit_policy_id"):
        if not _CSI_TOKEN.fullmatch(values[key]):
            errors.append(f"{key.upper()} : jeton [A-Z0-9_] de 60 caractères maximum requis.")
    if not _CSI_HASH.fullmatch(values["exit_policy_hash"]):
        errors.append("EXIT_POLICY_HASH : 16 caractères hexadécimaux minuscules requis.")
    for key in ("model_id", "ml_target_id", "ml_calibration_id"):
        if values[key] is not None and not _CSI_ID.fullmatch(values[key]):
            errors.append(f"{key.upper()} : identifiant invalide.")
    for key, allowed in CSI_ENUMS.items():
        value = values[key.lower()]
        if value not in allowed:
            errors.append(f"{key}={value} : valeur attendue parmi {', '.join(sorted(allowed))}.")
    if not _CSI_SYMBOL.fullmatch(values["symbol"]):
        errors.append("SYMBOL : seules les paires Spot USDT/USDC sont prises en charge.")
    if values["strategy_version"] < 1:
        errors.append("STRATEGY_VERSION : entier supérieur ou égal à 1 requis.")
    for key in ("max_hold_minutes", "ml_horizon_minutes"):
        if values[key] is not None and values[key] < 1:
            errors.append(f"{key.upper()} : entier supérieur ou égal à 1 requis.")
    if values["validation_status"] == "SCHEMA_EXAMPLE_ONLY":
        # Exemple synthétique de la spécification : conforme, mais jamais exécutable.
        errors.append("VALIDATION_STATUS=SCHEMA_EXAMPLE_ONLY : exemple de schéma, jamais exécuté.")
    if not (values["data_as_of"] <= values["decision_at"] <= values["created_at"] <= values["valid_from"]
            < values["expires_at"] <= values["entry_expires_at"]):
        errors.append("Ordre des dates requis : DATA_AS_OF <= DECISION_AT <= CREATED_AT <= VALID_FROM "
                      "< EXPIRES_AT <= ENTRY_EXPIRES_AT.")

    entry_1, entry_2, stop = values["entry_1"], values["entry_2"], values["stop_loss"]
    declared = [values[f"tp_{index}"] for index in range(1, CSI_MAX_TP + 1)]
    prices = {"ENTRY_1": entry_1, "ENTRY_2": entry_2, "STOP_LOSS": stop}
    prices.update({f"TP_{index}": price for index, price in enumerate(declared, 1)})
    prices_ok = True
    for name, price in prices.items():
        if price is not None and (not price.is_finite() or price <= 0):
            prices_ok = False
            errors.append(f"{name} : prix positif et fini requis.")

    # Entrées
    count = values["entry_count"]
    entries = [price for price in (entry_1, entry_2)[:max(count, 0)] if price is not None]
    entries_ok = 1 <= count <= CSI_MAX_ENTRIES
    if not entries_ok:
        errors.append(f"ENTRY_COUNT : entre 1 et {CSI_MAX_ENTRIES}.")
    elif (entry_2 is None) != (count == 1):
        entries_ok = False
        errors.append("ENTRY_COUNT incohérent avec ENTRY_2 (ENTRY_2=NONE si et seulement si ENTRY_COUNT=1).")
    entry_weights = values["entry_weights"]
    if len(entry_weights) != count or any(weight <= 0 for weight in entry_weights):
        entries_ok = False
        errors.append("ENTRY_WEIGHTS : un poids strictement positif par entrée.")
    elif abs(sum(entry_weights, Decimal(0)) - 1) > Decimal("1e-9"):
        entries_ok = False
        errors.append("ENTRY_WEIGHTS doit sommer à 1.")
    if entry_2 is not None and not stop < entry_2 < entry_1:
        errors.append("Achat incohérent : STOP_LOSS < ENTRY_2 < ENTRY_1 requis.")
    if count == 1 and values["rr_reference"] != "ENTRY_1":
        entries_ok = False
        errors.append("RR_REFERENCE=ENTRY_1 requis avec une seule entrée.")

    # Objectifs
    tp_count = values["tp_count"]
    targets_ok = 1 <= tp_count <= CSI_MAX_TP
    if not targets_ok:
        errors.append(f"TP_COUNT : entre 1 et {CSI_MAX_TP}.")
        targets = [price for price in declared if price is not None]
    else:
        targets = [price for price in declared[:tp_count] if price is not None]
        if len(targets) != tp_count:
            targets_ok = False
            errors.append("TP_COUNT incohérent avec les TP renseignés (TP_n au-delà de TP_COUNT = NONE).")
        if any(price is not None for price in declared[tp_count:]):
            errors.append("TP renseigné au-delà de TP_COUNT.")
    if targets and not stop < entry_1 < targets[0]:
        errors.append("Achat incohérent : STOP_LOSS < ENTRY_1 < TP_1 requis.")
    if any(later <= earlier for earlier, later in zip(targets, targets[1:])):
        errors.append("Les TP doivent être strictement croissants.")
    weights = values["tp_weights"]
    if len(weights) != tp_count or any(weight <= 0 for weight in weights):
        errors.append("TP_WEIGHTS : un poids strictement positif par TP.")
    elif abs(sum(weights, Decimal(0)) - 1) > Decimal("1e-9"):
        errors.append("TP_WEIGHTS doit sommer à 1.")

    _check_exit_policy(values, errors)

    # RR recalculés sur le prix de référence (si les prix permettent le calcul).
    if targets_ok and entries_ok and prices_ok and values["rr_reference"] in CSI_ENUMS["RR_REFERENCE"] \
            and values["weight_basis"] in CSI_ENUMS["WEIGHT_BASIS"]:
        reference = csi_reference_price(entries, entry_weights, values["weight_basis"], values["rr_reference"])
        given = [values[f"rr_tp{index}_gross"] for index in range(1, CSI_MAX_TP + 1)]
        if stop >= reference:
            errors.append("Risque nul ou négatif : STOP_LOSS doit être sous le prix de référence des RR.")
        else:
            for index in range(CSI_MAX_TP):
                value = given[index]
                if index < tp_count:
                    expected = csi_gross_rr(reference, stop, targets[index])
                    if value is None or abs(value - expected) > CSI_RR_QUANTUM / 2:
                        received = CSI_NONE if value is None else value
                        errors.append(f"RR_TP{index + 1}_GROSS attendu {expected}, reçu {received}.")
                elif value is not None:
                    errors.append(f"RR_TP{index + 1}_GROSS doit être NONE.")
    ml = [values[key] for key in ("ml_probability", "model_id", "ml_target_id", "ml_horizon_minutes",
                                  "ml_calibration_id")]
    if any(value is not None for value in ml):
        if any(value is None for value in ml):
            errors.append("ML_PROBABILITY, MODEL_ID, ML_TARGET_ID, ML_HORIZON_MINUTES et ML_CALIBRATION_ID "
                          "vont ensemble (tous renseignés ou tous NONE).")
        elif not 0 <= values["ml_probability"] <= 1:
            errors.append("ML_PROBABILITY dans [0, 1] requis.")
    deviation = values["max_entry_deviation_bps"]
    if not deviation.is_finite() or not 0 <= deviation <= 500:
        errors.append("MAX_ENTRY_DEVIATION_BPS : entre 0 et 500.")

    result.symbol = values["symbol"]
    result.entries = [float(price) for price in entries]
    result.targets = [float(price) for price in targets]
    result.stop = float(stop)
    result.published_at = values["created_at"].isoformat()
    result.signal_id = values["signal_id"]
    result.idempotency_key = values["idempotency_key"]
    result.decision_at = values["decision_at"].timestamp()
    result.valid_from = values["valid_from"].timestamp()
    result.expires_at = values["expires_at"].timestamp()
    result.entry_expires_at = values["entry_expires_at"].timestamp()
    result.entry_count = count
    result.rr_reference = values["rr_reference"]
    result.max_entry_deviation_bps = float(deviation)
    result.tp_weights = [float(weight) for weight in weights]
    result.exit_policy_id = values["exit_policy_id"]
    result.exit_policy_hash = values["exit_policy_hash"]
    result.max_hold_minutes = values["max_hold_minutes"]
    result.news_status = values["news_status"]
    result.validation_status = values["validation_status"]
    return result
