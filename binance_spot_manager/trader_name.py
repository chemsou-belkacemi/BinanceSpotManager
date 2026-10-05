"""Nom du trader (ou de la source) écrit en tête d'un signal texte.

Un message recopié par un relais ne dit pas de quel canal il vient, mais presque tous portent le nom du
trader en tête : « 👑 HAMZAWY 👑 », « Trader/ Suhaib AlMashhadani », « Al-Afify Harmonic Indicator Ultra »,
« ABK SIGNAL ALERT », parfois après une ligne d'événement (« Harmonic Pattern Detected ») ou une formule
(« بسم الله توكلت على الله »). Mesure du 2026-10-05 sur les exports du propriétaire (LEGEND TRADING,
AL-MAHWASHI CRYPTO, IN CRYPTO, fichiers de son robot) : un nom pour 867 signaux lisibles sur 874 ; les 7
autres n'en portent aucun.

Règle : seules comptent les lignes placées avant la première ligne de paire ou d'étiquette (entrée,
objectif, stop). Les formules religieuses, mots-dièses, données « clé : valeur », dates et lignes faites
seulement de mots génériques sont ignorés ; des mots banals seuls (« VIP », « CRYPTO VIP ») ne nomment
personne. Les préfixes (« Trader/ », « Ph. ») et les suffixes génériques (« Harmonic Indicator Ultra »,
« SIGNAL ALERT », « - Bat Pattern Detected ») sont retirés. La DERNIÈRE ligne restante donne le nom :
« INCRYPTO TIME ANALYSIS INDICATOR » puis « YASMINA BOUZID INDICATOR » → « YASMINA BOUZID ». Aucun nom
n'est inventé : sans ligne utilisable, le résultat est « ».

`name_key` donne la forme canonique, la même que CryptoSignalIntelligence (`external/parser.canonical_name`,
comparée par son test tests/test_group_name.py) : majuscules sans accents ni diacritiques, particules collées au
mot suivant (« Suhaib Al-Mashhadani » = « SUHAIB ALMASHHADANI »), aucun mot retiré (« CRYPTO LEGEND » reste
distinct de « LEGEND TRADING »), variantes réunies seulement par la liste déclarée ALIASES. Deux relectures
leak-auditor le 2026-10-05.
"""
from __future__ import annotations

import re
import unicodedata

from .signal_parser import JOINED_PAIR, LABELLED_LINE, SLASH_PAIR

#: Formules religieuses ou de politesse, reconnues par phrase entière : « عبد الرحمن » est un prénom.
FORMULAS = ("بسم الله", "بيم الله", "توكلت على الله", "توكلنا على الله", "الرحمن الرحيم", "الحمد لله",
            "سبحان الله", "شاء الله", "استغفر الله", "صلى الله", "BISMILLAH", "INSHALLAH", "IN SHAA ALLAH")
#: Mots qui décrivent le message, pas son auteur (retirés en tête et en fin de ligne).
GENERIC = frozenset({
    "HARMONIC", "PATTERN", "PATTERNS", "DETECTED", "TRADE", "TRADES", "TIME", "BASED", "TIME-BASED", "CYCLE",
    "ANALYSIS", "INDICATOR", "INDICATORS", "ULTRA", "SIGNAL", "SIGNALS", "ALERT", "ALERTS", "NEW", "SPOT",
    "LONG", "BUY", "ICT", "PREVIEW", "SETUP", "SWING", "SCALP", "SCALPING", "TERM", "SHORT", "MID", "UPDATE",
    "SPECIAL", "TP", "TRACKING", "HOLD",
    "معاينة", "الصفقة", "صفقة", "جديدة", "توصية", "اشارة", "إشارة", "شراء", "سبوت",
})
#: Premier mot d'une donnée (« Type: Spot », « Risk Level - High », « Market = Spot ») : jamais un nom.
METADATA_KEYS = frozenset({"TYPE", "MARKET", "RISK", "LEVEL", "POSITION", "DIRECTION", "SIDE", "TIMEFRAME",
                           "TF", "DURATION", "STRATEGY", "EXCHANGE", "LEVERAGE", "DATE", "TIME", "STATUS",
                           "CATEGORY", "MODE", "ORDER", "PLATFORM", "NOTE", "INFO",
                 "ATTENTION", "WARNING", "REMINDER", "DISCLAIMER"})
#: Noms faits seulement de ces mots : personne n'est nommé (aucun nom plutôt qu'un nom partagé par des canaux).
BANAL = frozenset({"VIP", "KING", "PRO", "PREMIUM", "FREE", "BINANCE", "SPOT", "CRYPTO", "TRADING", "TRADER",
                   "TRADERS", "SIGNAL", "SIGNALS", "IN", "THE", "BEST", "TOP", "GOLD", "MASTER", "EXPERT",
                   "ELITE", "TEAM", "CHANNEL", "GROUP", "CLUB", "ACADEMY",
                   "GOOD", "MORNING", "EVENING", "NIGHT", "HELLO", "HI", "DEAR", "FRIENDS", "GUYS",
                   "EVERYONE", "ALL",
                   "BTC", "ETH", "BNB", "SOL", "USDT", "USDC", "XRP",
                   "توصيات", "كريبتو", "تداول", "اشارات", "إشارات", "قناة", "مجموعة", "صباح", "مساء", "الخير",
                   "اخواني", "إخواني"})
PREFIX = re.compile(r"^(?:(?:TRADER|ANALYST)\s*[/:]\s*|(?:TRADER|ANALYST|BY|FROM|PH\.?)\s+"
                    r"|(?:المحلل|المتداول)\s*[/:]\s*)", re.IGNORECASE)
SEPARATED = re.compile(r"^(?P<key>[^:=|→]{1,40}?)\s*[:=|→]\s*(?P<value>\S.*)$")
_MONTHS = (r"(?:JAN(?:UARY|VIER)?|FEB(?:RUARY)?|F[EÉ]V(?:RIER)?|MAR(?:CH|S)?|APR(?:IL)?|AVR(?:IL)?|MAY|MAI"
           r"|JUN(?:E)?|JUIN|JUL(?:Y)?|JUIL(?:LET)?|AUG(?:UST)?|AO[UÛ]T|SEP(?:T(?:EMBER|EMBRE)?)?"
           r"|OCT(?:OBER|OBRE)?|NOV(?:EMBER|EMBRE)?|DEC(?:EMBER)?|D[EÉ]C(?:EMBRE)?)")
DATE_OR_TIME = re.compile(
    r"\d{1,4}\s*[/.-]\s*\d{1,2}(?:\s*[/.-]\s*\d{1,4})?|\b\d{1,2}\s*[:hH]\s*\d{2}\b"
    r"|\b\d{1,2}\s*(?:AM|PM)\b|\b(?:UTC|GMT)\b"
    r"|\b(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|LUNDI|MARDI|MERCREDI|JEUDI|VENDREDI|SAMEDI"
    r"|DIMANCHE)\b"
    rf"|\b\d{{1,2}}\s+{_MONTHS}\b|\b{_MONTHS}\s+\d{{1,2}}\b",
    re.IGNORECASE)
#: « AL - MAHWASHI » : la particule reste collée au mot suivant, sauf devant une description (« BEN - New Signal »).
PARTICLE_DASH = re.compile(r"\b(AL|EL|ABD|ABU|ABO|BEN|BIN|IBN)\s*-\s*([^\W_]+)", re.IGNORECASE)
PARTICLES = frozenset({"AL", "EL", "ABD", "ABU", "ABO", "BEN", "BIN", "IBN"})
#: Variantes d'un même nom vues dans les exports du propriétaire ; toute autre fusion se déclare ici.
ALIASES = {
    "ALMAHWASHI CRYPTO TRADING": "ALMAHWASHI CRYPTO",
    "ALMAHWASHI TRADING CRYPTO": "ALMAHWASHI CRYPTO",
    "ALAFIFY TRADING": "ALAFIFY",
}
MAX_LENGTH = 48
MAX_WORDS = 6


def _words(text: str) -> list[str]:
    return [word.upper().strip(".:") for word in text.split()]


#: Mots d'une description placée après « - » ou « : » (« Bat Pattern Detected », « Daily Chart », « Gartley »).
DESCRIPTION = frozenset({"PATTERN", "PATTERNS", "DETECTED", "CHART", "DAILY", "WEEKLY", "GARTLEY", "BAT",
                           "BUTTERFLY", "CRAB", "SHARK", "CYPHER", "ABCD"})


def _generic(word: str) -> bool:
    """Mot générique, y compris composé (« MID-TERM », « TIME-BASED »)."""
    word = word.upper().strip(".:")
    return word in GENERIC or ("-" in word and all(part in GENERIC for part in word.split("-") if part))


def _without_description(text: str) -> str:
    """« NOM - Bat Pattern Detected » → « NOM » ; « AL-MAHWASHI CRYPTO - VIP » reste entier (VIP distingue)."""
    left, separator, right = text.partition(" - ")
    words = _words(right)
    if separator and words and (all(_generic(word) for word in words) or DESCRIPTION & set(words)):
        return left.strip()
    return text


def _starts_the_signal(line: str) -> bool:
    """Ligne de paire ou d'étiquette (entrée, objectif, stop) : la fin de l'en-tête."""
    text = unicodedata.normalize("NFKC", line).upper().replace("*", "")
    text = "".join(" " if unicodedata.category(ch) in ("So", "Sk") else ch for ch in text)
    text = text.strip().lstrip("#").strip()
    return bool(LABELLED_LINE.match(text) or SLASH_PAIR.search(text) or JOINED_PAIR.search(text))


def name_from_line(line: str) -> str:
    """Nom porté par une ligne d'en-tête, ou « » (formule, mot-dièse, donnée, date, ligne d'événement ou
    générique, phrase, mots banals)."""
    plain = "".join(ch for ch in unicodedata.normalize("NFKC", line)
                    if unicodedata.category(ch) != "Mn" and ch != "\u0640")       # diacritiques, tatouil
    bare = "".join(" " if unicodedata.category(ch) in ("So", "Sk") else ch for ch in plain)
    head = re.sub(r"^\d{1,2}[.)]\s*", "", bare.strip().lstrip("*•·▪▫-–—_( "))
    if head.startswith(("#", "$")) or re.search(r"t\.me/|https?:|www\.", bare, re.IGNORECASE):
        return ""
    text = "".join(ch if unicodedata.category(ch)[0] in "LNZ" or ch in "/&'.-:=|→" else " "
                   for ch in plain.replace("*", " ").replace("_", " "))
    text = " ".join(text.split()).strip(" -/.:&'=|→")
    if not text or any(formula in text.upper() for formula in FORMULAS) or DATE_OR_TIME.search(text):
        return ""
    text = PREFIX.sub("", text).strip(" -/.:")
    if _words(text) and _words(text)[0] in METADATA_KEYS:
        return ""
    separated = SEPARATED.match(text)
    if separated:
        # « LEGEND TRADING: NEW SIGNAL » → « LEGEND TRADING » ; toute autre « clé : valeur » est une donnée.
        value = _words(separated.group("value"))
        if not (all(_generic(word) for word in value) or DESCRIPTION & set(value)):
            return ""
        text = separated.group("key").strip()
    text = PARTICLE_DASH.sub(lambda match: match.group(0) if match.group(2).upper() in GENERIC
                             else f"{match.group(1)}-{match.group(2)}", text)
    words = _without_description(text).strip(" -/.:").split()
    while words and _generic(words[-1]):
        words.pop()
    while words and _generic(words[0]):
        words.pop(0)
    name = " ".join(words).strip(" -/.:")
    if not name or len(name) > MAX_LENGTH or len(words) > MAX_WORDS:
        return ""
    if all(word in BANAL for word in re.findall(r"[^\W_]+", name.upper())):
        return ""
    return name


def trader_of(text: str) -> str:
    """Nom du trader écrit en tête du signal ; « » s'il n'y en a pas."""
    names = []
    for line in (text or "").splitlines():
        if _starts_the_signal(line):
            break
        name = name_from_line(line)
        if name:
            names.append(name)
    return names[-1] if names else ""


def name_key(name: str) -> str:
    """Forme canonique (clé de regroupement) : majuscules sans accents ni diacritiques, particules collées au mot
    suivant, puis variantes déclarées (ALIASES). Identique à CryptoSignalIntelligence `canonical_name`."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch) and ch != "\u0640").upper()
    words: list[str] = []
    for word in re.findall(r"[^\W_]+", text):
        if words and words[-1] in PARTICLES:
            words[-1] += word
        else:
            words.append(word)
    canonical = " ".join(words)[:60]
    return ALIASES.get(canonical, canonical)
