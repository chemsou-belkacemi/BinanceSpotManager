"""Label-based signal reader. Parsing never submits orders or guesses missing prices.

Any provider layout is read through its labels (pair, entry, target, stop, platform), whatever
surrounds them: emoji, separator lines, numbering, percentages, arrows, pipes. Values are strict:
anything ambiguous is refused rather than guessed — two prices where one is expected, "or market",
a thousands comma, "90K", "90000+", two pairs, unordered targets, a stop above the entries...
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
import unicodedata

TEMPLATES = {"auto": "Automatique", "structured": "PAIR / ENTRY / T1 (Suhaib, Cleo)",
             "abk": "Coin / Entry Zone / Target (ABK)",
             "numbered": "#PAIRE / Entry1 / TP1 / Stop (Al-Mahwashi)",
             "simple": "Générique (étiquettes Entry / TP / SL)"}
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

    def to_dict(self):
        return asdict(self)


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
