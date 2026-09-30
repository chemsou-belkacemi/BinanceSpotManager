"""Explicit text templates. Parsing never submits orders or guesses missing prices.

Deux familles de formats coexistent, sans jamais se recouvrir :

* les modèles texte historiques (Telegram, dépôt JSON v1) ;
* le contrat TXT V2 de CryptoSignalIntelligence (``SIGNAL_VERSION=2`` en
  première ligne, ``CLE=VALEUR`` strict), traité par :func:`parse_signal_v2`.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
import hashlib
import math
import re
import unicodedata

TEMPLATES = {"auto": "Automatique", "structured": "PAIR / ENTRY / T1 (Suhaib, Cleo)",
             "abk": "Coin / Entry Zone / Target (ABK)",
             "numbered": "#PAIRE / Entry1 / TP1 / Stop (Al-Mahwashi)",
             "simple": "BUY / Entry Price / TP",
             "v2": "TXT V2 CryptoSignalIntelligence (SIGNAL_VERSION=2)"}
NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"


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
    # Champs du contrat TXT V2 ; les valeurs par défaut gardent lisibles les
    # lignes analysées avant leur introduction (formats historiques : version 1).
    signal_version: int = 1
    signal_id: str = ""
    idempotency_key: str = ""
    #: Fenêtre de validité en temps Unix UTC (0 : non fournie par le format).
    valid_from: float = 0.0
    expires_at: float = 0.0
    max_entry_deviation_bps: float = 0.0
    tp_weights: list[float] = field(default_factory=list)
    exit_policy_id: str = ""

    def to_dict(self):
        return asdict(self)

    @property
    def is_v2(self) -> bool:
        return self.signal_version == 2


def normalize(text):
    text = unicodedata.normalize("NFKC", text).upper()
    text = re.sub(r"[0-9]\ufe0f?\u20e3", "", text)
    text = text.replace("\u200e", "").replace("\u200f", "")
    return text.replace("**", "").replace("\ufe0f", "")


def content_hash(text):
    return hashlib.sha256(" ".join(normalize(text).split()).encode()).hexdigest()


def first_line_is_v2(raw: str) -> bool:
    """Vrai si la première ligne non vide annonce le contrat TXT V2."""
    for line in raw.splitlines():
        if line.strip():
            return line.strip().startswith("SIGNAL_VERSION=")
    return False


def parse_signal(raw: str, template: str = "auto") -> ParsedSignal:
    result = ParsedSignal()
    if not raw.strip() or len(raw) > 20000:
        result.errors.append("Texte vide ou trop long (20 000 caractères maximum).")
        return result
    if first_line_is_v2(raw):
        # Contrat V2 : jamais interprété par les modèles texte historiques.
        result = parse_signal_v2(raw)
        if template not in {"auto", "v2"}:
            result.errors.append("Le texte ne correspond pas au modèle sélectionné.")
        return result
    if template == "v2":
        result.errors.append("Le texte ne correspond pas au modèle sélectionné (SIGNAL_VERSION=2 attendu en première ligne).")
        return result
    if re.search(r"^\s*SIGNAL_VERSION=", raw, re.M):
        result.errors.append("Ligne SIGNAL_VERSION hors première ligne : contrat V2 mal formé, jamais lu comme un modèle texte.")
        return result
    text = normalize(raw)
    lines = [re.sub(r"^[^A-Z0-9#]+", "", line.strip()) for line in text.splitlines()]
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
    if re.search(r"\b(?:SHORT|SELL|LEVERAGE|FUTURES)\b", text):
        result.direction = "SELL"
        result.errors.append("Short, vente initiale et levier non pris en charge en Spot.")
    elif re.search(r"\b(?:BUY|LONG)\b", text) or detected in {"structured", "abk", "numbered"}:
        result.direction = "BUY"
    else:
        result.errors.append("Direction d'achat non identifiable.")
    pairs = re.findall(r"(?:^|\n)(?:PAIR|COIN)\s*:\s*([A-Z0-9]+\s*/\s*[A-Z0-9]+|[A-Z0-9]+)", clean)
    if not pairs:
        pairs = re.findall(r"\b([A-Z0-9]{2,20}/(?:USDT|USDC|USD))\b|#([A-Z0-9]+USDT)\b|(?:BUY|SELL)\s+(?:LIMIT\s+)?([A-Z0-9]+USD[T]?)\b", clean)
        pairs = [next(x for x in match if x) for match in pairs]
    if len(pairs) != 1:
        result.errors.append("Une seule paire explicite est requise ; aucune devise n'est ajoutée automatiquement.")
    else:
        result.symbol = re.sub(r"[\s/]", "", pairs[0])
        if not re.fullmatch(r"[A-Z0-9]{2,20}(?:USDT|USDC)", result.symbol):
            result.errors.append("Seules les paires Spot USDT/USDC sont prises en charge.")
    platforms = re.findall(r"^PLATFORM\s*:\s*(\w+)", clean, re.M)
    result.exchange = platforms[0] if platforms else ""
    if any(platform != "BINANCE" for platform in platforms):
        result.errors.append("Plateforme autre que Binance : aucune conversion automatique.")
    if not platforms:
        result.warnings.append("Plateforme absente : la paire devra être vérifiée sur Binance Demo.")

    # Strict whole numeric prefix; percentages, comma ambiguity and extra price tokens fail closed.
    indexed_entries, indexed_targets = [], []
    stops = []
    for line in lines:
        entry = re.match(r"ENTRY(?:\s+(ZONE|PRICE)|\s*(\d+))?\s*:\s*(.*)$", line)
        target = re.match(r"(?:T(?:P)?\s*(\d+)|TARGET\s*(\d*)|TP)\s*(?::|→|-|\s)\s*(.*)$", line)
        stop = re.match(r"(?:SL|STOP(?:\s*LOSS)?)\s*(?::|-|\s)\s*(.*)$", line)
        if re.match(r"ENTRY\s*\d", line) and not entry:
            result.errors.append("Ligne d'entrée numérotée non reconnue : modèle à compléter.")
        if re.match(r"(?:TP?\s*\d|TARGET\s*\d)", line) and not target:
            result.errors.append("Ligne d'objectif numéroté non reconnue : modèle à compléter.")
        if entry and entry[3].strip():
            value = entry[3].strip()
            match = re.fullmatch(rf"({NUMBER})(?:\s*[–—-]\s*({NUMBER}))?\s*", value)
            if not match:
                result.errors.append("Prix d'entrée ambigu ou non pris en charge.")
            else:
                indexed_entries.append(entry[2] or entry[1] or "single")
                result.entries.extend(float(v) for v in match.groups() if v)
        if target:
            indexed_targets.append(target[1] or target[2] or "single")
            match = re.fullmatch(rf"({NUMBER})\s*(?:[\[(][^\]\n)]*[%][\])])?\s*", target[3])
            if match:
                result.targets.append(float(match[1]))
            else:
                result.errors.append("Objectif ambigu (plage, +, pourcentage seul ou texte inattendu).")
        if stop:
            match = re.fullmatch(rf"({NUMBER})\s*(?:\((\d+\s*(?:H|MIN|M))\))?\s*(?:[\[(][^\]\n)]*%[\])])?\s*", stop[1])
            if match:
                stops.append(float(match[1]))
                result.stop_timeframe = (match[2] or "").lower()
            else:
                result.errors.append("Stop loss ambigu ou non pris en charge.")
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
    hour = re.search(r"(\d{1,2}):(\d{2})\s*GMT\s*([+-]\d{1,2})\b", text)
    if date and hour:
        try:
            stamp = datetime.fromisoformat(date[1]).replace(hour=int(hour[1]), minute=int(hour[2]),
                         tzinfo=timezone(timedelta(hours=int(hour[3]))))
            result.published_at = stamp.astimezone(timezone.utc).isoformat()
        except ValueError:
            result.errors.append("Date du signal invalide.")
    else:
        result.warnings.append("Date source non vérifiable : contrôler manuellement la validité du signal.")
    return result


# ==========================================================================
# Contrat TXT V2 (CryptoSignalIntelligence, docs/SIGNAL_FORMAT_V2.md)
# ==========================================================================

V2_VERSION = 2
V2_ANALYSIS_MARKER = "---ANALYSIS---"
V2_NONE = "NONE"
V2_MAX_TP = 4
V2_RR_QUANTUM = Decimal("0.001")
_V2_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
_V2_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_V2_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
_V2_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")
_V2_TOKEN = re.compile(r"^[A-Z0-9_]{1,60}$")
_V2_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}(USDT|USDC)$")

#: Clés du contrat et leur type, dans l'ordre canonique du producteur.
#: ``?`` : NONE autorisé ; ``decimals`` : liste séparée par des virgules.
V2_FIELDS: tuple[tuple[str, str], ...] = (
    ("SIGNAL_VERSION", "int"), ("SIGNAL_ID", "str"), ("IDEMPOTENCY_KEY", "str"),
    ("CREATED_AT", "time"), ("DATA_AS_OF", "time"), ("VALID_FROM", "time"), ("EXPIRES_AT", "time"),
    ("MARKET_DATA_SOURCE", "str"), ("INTENDED_EXECUTION_ENVIRONMENT", "str"), ("MARKET_TYPE", "str"),
    ("SYMBOL", "str"), ("ACTION", "str"), ("STRATEGY", "str"), ("STRATEGY_VERSION", "int"),
    ("TIMEFRAME_SETUP", "str"), ("ENTRY_MODE", "str"), ("ENTRY_1", "decimal"), ("ENTRY_2", "decimal?"),
    ("ENTRY_WEIGHTS", "decimals"), ("WEIGHT_BASIS", "str"), ("STOP_LOSS", "decimal"),
    ("TP_COUNT", "int"), ("TP_1", "decimal"), ("TP_2", "decimal?"), ("TP_3", "decimal?"), ("TP_4", "decimal?"),
    ("TP_WEIGHTS", "decimals"), ("EXIT_POLICY_ID", "str"),
    ("RR_TP1_GROSS", "decimal"), ("RR_TP2_GROSS", "decimal?"), ("RR_TP3_GROSS", "decimal?"),
    ("RR_TP4_GROSS", "decimal?"), ("TECHNICAL_SCORE", "decimal?"), ("ML_PROBABILITY", "decimal?"),
    ("MODEL_ID", "str?"), ("TREND_REGIME", "str"), ("VOLATILITY_REGIME", "str"),
    ("MAX_ENTRY_DEVIATION_BPS", "decimal"), ("VALIDATION_STATUS", "str"), ("INTEGRATION_STATUS", "str"),
    ("STATUS", "str"),
)
V2_KINDS = dict(V2_FIELDS)
#: Clés facultatives à la lecture (le producteur les écrit toujours).
V2_OPTIONAL_KEYS = frozenset({"INTEGRATION_STATUS"})
#: Énumérations fermées du contrat ; toute autre valeur bloque le signal.
V2_ENUMS = {
    "INTENDED_EXECUTION_ENVIRONMENT": {"DEMO"},
    "MARKET_TYPE": {"SPOT"},
    "ACTION": {"BUY"},
    "ENTRY_MODE": {"LIMIT"},
    "WEIGHT_BASIS": {"BASE_QUANTITY", "QUOTE_BUDGET"},
    "TIMEFRAME_SETUP": {"5m", "15m", "1h", "4h"},
    "TREND_REGIME": {"BULL", "BEAR", "RANGE", "UNKNOWN"},
    "VOLATILITY_REGIME": {"LOW", "NORMAL", "HIGH", "UNKNOWN"},
    "VALIDATION_STATUS": {"RESEARCH", "VALIDATED_OOS", "SHADOW", "DEMO_ELIGIBLE", "SCHEMA_EXAMPLE_ONLY"},
    "INTEGRATION_STATUS": {"INTEGRATION_UNVERIFIED", "INTEGRATION_VERIFIED"},
    "STATUS": {"NEW"},
}


class SignalV2FormatError(ValueError):
    """Texte non conforme au contrat TXT V2 (lecture arrêtée à la première anomalie)."""


def v2_gross_rr(entry: Decimal, stop: Decimal, target: Decimal) -> Decimal:
    """RR brut recalculé comme chez le producteur : (TP - ENTRY_1) / (ENTRY_1 - STOP), 3 décimales."""
    risk = entry - stop
    if risk <= 0:
        raise SignalV2FormatError("risque nul ou négatif : STOP_LOSS doit être sous ENTRY_1")
    return ((target - entry) / risk).quantize(V2_RR_QUANTUM, rounding=ROUND_HALF_EVEN)


def _v2_parse_value(raw: str, kind: str, key: str):
    if raw == V2_NONE:
        if not kind.endswith("?"):
            raise SignalV2FormatError(f"{key} est obligatoire (NONE interdit)")
        return None
    base = kind.rstrip("?")
    if base == "time":
        if not _V2_TIME.fullmatch(raw):
            raise SignalV2FormatError(f"{key} : format YYYY-MM-DDTHH:MM:SSZ requis")
        try:
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            raise SignalV2FormatError(f"{key} : date invalide") from None
    if base == "int":
        if not re.fullmatch(r"\d+", raw):
            raise SignalV2FormatError(f"{key} : entier requis")
        return int(raw)
    if base in {"decimal", "decimals"}:
        parts = raw.split(",") if base == "decimals" else [raw]
        values = []
        for part in parts:
            if not _V2_DECIMAL.fullmatch(part):
                raise SignalV2FormatError(f"{key} : nombre décimal à point requis (reçu {part!r})")
            values.append(Decimal(part))
        return tuple(values) if base == "decimals" else values[0]
    return raw


def read_signal_v2(text: str) -> dict:
    """Lecture stricte du contrat ; lève SignalV2FormatError à la première anomalie.

    Retourne les valeurs typées (Decimal, datetime UTC, int, str ou None) par
    clé en minuscules. La prose qui suit ``---ANALYSIS---`` n'est pas lue : elle
    ne peut pas modifier le contrat. La cohérence métier (prix, poids, RR,
    dates) est contrôlée ensuite par :func:`parse_signal_v2`.
    """
    if "\r" in text.replace("\r\n", "\n"):
        raise SignalV2FormatError("fin de ligne invalide (retour chariot isolé)")
    contract: dict[str, str] = {}
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if line == V2_ANALYSIS_MARKER:
            break
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        if not separator or not _V2_KEY.fullmatch(key):
            raise SignalV2FormatError(f"ligne {number} : format CLE=VALEUR requis")
        if not value or value != value.strip():
            raise SignalV2FormatError(f"ligne {number} : valeur vide ou entourée d'espaces")
        if not contract and key != "SIGNAL_VERSION":
            raise SignalV2FormatError("SIGNAL_VERSION doit être la première clé")
        if key in contract:
            raise SignalV2FormatError(f"clé dupliquée : {key}")
        if key not in V2_KINDS:
            raise SignalV2FormatError(f"clé inconnue pour la version {V2_VERSION} : {key}")
        contract[key] = value
    if contract.get("SIGNAL_VERSION") != str(V2_VERSION):
        raise SignalV2FormatError(
            f"version non prise en charge : {contract.get('SIGNAL_VERSION')!r} ({V2_VERSION} attendu)"
        )
    missing = [key for key, _ in V2_FIELDS if key not in contract and key not in V2_OPTIONAL_KEYS]
    if missing:
        raise SignalV2FormatError("clés manquantes : " + ", ".join(missing))
    values = {key.lower(): _v2_parse_value(raw, V2_KINDS[key], key) for key, raw in contract.items()}
    values.setdefault("integration_status", "INTEGRATION_UNVERIFIED")
    return values


def parse_signal_v2(raw: str) -> ParsedSignal:
    """Contrat TXT V2 → ParsedSignal ; toute anomalie devient une erreur bloquante.

    Reproduit les règles du modèle producteur : énumérations fermées, DEMO et
    Spot uniquement, achat LIMIT à une seule entrée, paire USDT/USDC,
    STOP_LOSS < ENTRY_1 < TP_1 < … strictement croissants, TP_COUNT cohérent,
    poids strictement positifs sommant à 1 (tolérance 1e-9), RR recalculés,
    DATA_AS_OF <= CREATED_AT <= VALID_FROM < EXPIRES_AT.
    """
    result = ParsedSignal(template="v2", signal_version=V2_VERSION, direction="BUY")
    try:
        values = read_signal_v2(raw)
    except SignalV2FormatError as exc:
        result.errors.append(f"Contrat V2 : {exc}.")
        return result
    errors = result.errors
    for key in ("signal_id", "idempotency_key"):
        if not _V2_ID.fullmatch(values[key]):
            errors.append(f"{key.upper()} : identifiant invalide (1 à 160 caractères [A-Za-z0-9_.:-]).")
    for key in ("market_data_source", "strategy", "exit_policy_id"):
        if not _V2_TOKEN.fullmatch(values[key]):
            errors.append(f"{key.upper()} : jeton [A-Z0-9_] de 60 caractères maximum requis.")
    if values["model_id"] is not None and not _V2_ID.fullmatch(values["model_id"]):
        errors.append("MODEL_ID : identifiant invalide.")
    for key, allowed in V2_ENUMS.items():
        value = values[key.lower()]
        if value not in allowed:
            errors.append(f"{key}={value} : valeur attendue parmi {', '.join(sorted(allowed))}.")
    if not _V2_SYMBOL.fullmatch(values["symbol"]):
        errors.append("SYMBOL : seules les paires Spot USDT/USDC sont prises en charge.")
    if values["strategy_version"] < 1:
        errors.append("STRATEGY_VERSION : entier supérieur ou égal à 1 requis.")
    if values["validation_status"] == "SCHEMA_EXAMPLE_ONLY":
        # Exemple synthétique de la spécification : conforme, mais jamais exécutable.
        errors.append("VALIDATION_STATUS=SCHEMA_EXAMPLE_ONLY : exemple de schéma, jamais exécuté.")
    if not values["data_as_of"] <= values["created_at"] <= values["valid_from"] < values["expires_at"]:
        errors.append("Ordre des dates requis : DATA_AS_OF <= CREATED_AT <= VALID_FROM < EXPIRES_AT.")

    entry, stop = values["entry_1"], values["stop_loss"]
    declared = [values[f"tp_{index}"] for index in range(1, V2_MAX_TP + 1)]
    prices = {"ENTRY_1": entry, "ENTRY_2": values["entry_2"], "STOP_LOSS": stop}
    prices.update({f"TP_{index}": price for index, price in enumerate(declared, 1)})
    for name, price in prices.items():
        if price is not None and (not price.is_finite() or price <= 0):
            errors.append(f"{name} : prix positif et fini requis.")
    if values["entry_2"] is not None:
        errors.append("ENTRY_2 non prise en charge par ce moteur : NONE requis.")
    if len(values["entry_weights"]) != 1 or values["entry_weights"][0] != 1:
        errors.append("ENTRY_WEIGHTS doit valoir 1.0 pour une entrée unique.")
    count = values["tp_count"]
    consistent = 1 <= count <= V2_MAX_TP
    if not consistent:
        errors.append(f"TP_COUNT : entre 1 et {V2_MAX_TP}.")
        targets = [price for price in declared if price is not None]
    else:
        targets = [price for price in declared[:count] if price is not None]
        if len(targets) != count:
            consistent = False
            errors.append("TP_COUNT incohérent avec les TP renseignés (TP_n au-delà de TP_COUNT = NONE).")
        if any(price is not None for price in declared[count:]):
            errors.append("TP renseigné au-delà de TP_COUNT.")
    if targets and not stop < entry < targets[0]:
        errors.append("Achat incohérent : STOP_LOSS < ENTRY_1 < TP_1 requis.")
    if any(later <= earlier for earlier, later in zip(targets, targets[1:])):
        errors.append("Les TP doivent être strictement croissants.")
    weights = values["tp_weights"]
    if len(weights) != count or any(weight <= 0 for weight in weights):
        errors.append("TP_WEIGHTS : un poids strictement positif par TP.")
    elif abs(sum(weights, Decimal(0)) - 1) > Decimal("1e-9"):
        errors.append("TP_WEIGHTS doit sommer à 1.")
    if consistent and stop < entry:
        given = [values[f"rr_tp{index}_gross"] for index in range(1, V2_MAX_TP + 1)]
        for index in range(V2_MAX_TP):
            value = given[index]
            if index < count:
                expected = v2_gross_rr(entry, stop, targets[index])
                if value is None or abs(value - expected) > V2_RR_QUANTUM / 2:
                    received = V2_NONE if value is None else value
                    errors.append(f"RR_TP{index + 1}_GROSS attendu {expected}, reçu {received}.")
            elif value is not None:
                errors.append(f"RR_TP{index + 1}_GROSS doit être NONE.")
    if values["ml_probability"] is not None and not (0 <= values["ml_probability"] <= 1 and values["model_id"]):
        errors.append("ML_PROBABILITY dans [0, 1] et MODEL_ID requis ensemble.")
    deviation = values["max_entry_deviation_bps"]
    if not deviation.is_finite() or not 0 <= deviation <= 500:
        errors.append("MAX_ENTRY_DEVIATION_BPS : entre 0 et 500.")

    result.symbol = values["symbol"]
    result.entries = [float(entry)]
    result.targets = [float(price) for price in targets]
    result.stop = float(stop)
    result.published_at = values["created_at"].isoformat()
    result.signal_id = values["signal_id"]
    result.idempotency_key = values["idempotency_key"]
    result.valid_from = values["valid_from"].timestamp()
    result.expires_at = values["expires_at"].timestamp()
    result.max_entry_deviation_bps = float(deviation)
    result.tp_weights = [float(weight) for weight in weights]
    result.exit_policy_id = values["exit_policy_id"]
    return result
