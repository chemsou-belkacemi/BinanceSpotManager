"""Explicit text templates. Parsing never submits orders or guesses missing prices."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
import unicodedata

TEMPLATES = {"auto": "Automatique", "structured": "PAIR / ENTRY / T1 (Suhaib, Cleo)",
             "abk": "Coin / Entry Zone / Target (ABK)",
             "numbered": "#PAIRE / Entry1 / TP1 / Stop (Al-Mahwashi)",
             "simple": "BUY / Entry Price / TP"}
NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
UNVERIFIABLE_SOURCE_DATE_WARNING = (
    "Date source non vérifiable : contrôler manuellement la validité du signal."
)


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

    def to_dict(self):
        return asdict(self)


def normalize(text):
    text = unicodedata.normalize("NFKC", text).upper()
    text = re.sub(r"[0-9]\ufe0f?\u20e3", "", text)
    text = text.replace("\u200e", "").replace("\u200f", "")
    return text.replace("**", "").replace("\ufe0f", "")


def content_hash(text):
    return hashlib.sha256(" ".join(normalize(text).split()).encode()).hexdigest()


def parse_signal(raw: str, template: str = "auto") -> ParsedSignal:
    result = ParsedSignal()
    if not raw.strip() or len(raw) > 20000:
        result.errors.append("Texte vide ou trop long (20 000 caractères maximum).")
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
        result.warnings.append(UNVERIFIABLE_SOURCE_DATE_WARNING)
    return result
