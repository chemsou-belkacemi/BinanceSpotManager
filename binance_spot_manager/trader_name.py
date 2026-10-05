"""Nom du trader (ou de la source) écrit en tête d'un signal texte.

Un message recopié par un relais ne dit pas de quel canal il vient, mais presque tous portent le nom du
trader en tête : « 👑 HAMZAWY 👑 », « Trader/ Suhaib AlMashhadani », « Al-Afify Harmonic Indicator Ultra »,
« ABK SIGNAL ALERT », parfois après une ligne d'événement (« Harmonic Pattern Detected ») ou une formule
(« بسم الله توكلت على الله »). Mesure du 2026-10-05 sur les exports du propriétaire (LEGEND TRADING,
AL-MAHWASHI CRYPTO, IN CRYPTO, fichiers de son robot) : un nom pour 867 signaux lisibles sur 874 ; les 7
autres n'en portent aucun.

Règle : seules comptent les lignes placées avant la première ligne de paire ou d'étiquette (entrée,
objectif, stop). Les formules religieuses et les lignes faites seulement de mots génériques sont
ignorées. Les préfixes (« Trader/ », « Ph. ») et les suffixes génériques (« Harmonic Indicator Ultra »,
« SIGNAL ALERT », « - Bat Pattern Detected ») sont retirés. La DERNIÈRE ligne restante donne le nom :
« INCRYPTO TIME ANALYSIS INDICATOR » puis « YASMINA BOUZID INDICATOR » → « YASMINA BOUZID ». Aucun nom
n'est inventé : sans ligne utilisable, le résultat est « ».

`name_key` regroupe les variantes d'écriture (« Suhaib Al-Mashhadani », « SUHAIB ALMASHHADANI ») en
ignorant les mots « TRADING », « CRYPTO » et « TRADER » ; « VIP » reste distinctif.
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
    "SPECIAL", "TP", "TRACKING",
    "معاينة", "الصفقة", "صفقة", "جديدة", "توصية", "اشارة", "إشارة", "شراء", "سبوت",
})
#: Mots ignorés pour regrouper les variantes d'un même nom.
KEY_NOISE = frozenset({"TRADING", "CRYPTO", "TRADER"})
PREFIX = re.compile(r"^(?:(?:TRADER|ANALYST)\s*[/:]\s*|(?:TRADER|ANALYST|BY|FROM|PH\.?)\s+)", re.IGNORECASE)
#: Ligne « clé : valeur » (« Type: Spot », « Market: Spot ») : une donnée, pas un nom, sauf Trader:/Analyst:.
METADATA = re.compile(r"^(?!(?:TRADER|ANALYST)\b)[^\W\d_][^:]{0,24}:\s*\S", re.IGNORECASE)
#: Date ou heure (« 05/10/2026 », « 2026-10-05 », « 14:00 »).
DATE_OR_TIME = re.compile(r"\d{1,4}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{1,4}|\b\d{1,2}:\d{2}\b")
#: « AL - MAHWASHI » : la particule reste collée au mot suivant (pas une coupure « nom - description »).
PARTICLE_DASH = re.compile(r"\b(AL|EL|ABD|ABU|ABO|BEN|BIN|IBN)\s*-\s*", re.IGNORECASE)
MAX_LENGTH = 48
MAX_WORDS = 6


def _clean(line: str) -> str:
    """Lettres, chiffres et quelques signes ; pictogrammes, gras et filets remplacés par des espaces."""
    text = unicodedata.normalize("NFKC", line).replace("*", " ").replace("_", " ")
    text = "".join(ch if unicodedata.category(ch)[0] in "LNZ" or ch in "/&'.-:" else " " for ch in text)
    return " ".join(text.split()).strip(" -/.:&'")


def _starts_the_signal(line: str) -> bool:
    """Ligne de paire ou d'étiquette (entrée, objectif, stop) : la fin de l'en-tête."""
    text = unicodedata.normalize("NFKC", line).upper().replace("*", "")
    text = "".join(" " if unicodedata.category(ch) in ("So", "Sk") else ch for ch in text)
    text = text.strip().lstrip("#").strip()
    return bool(LABELLED_LINE.match(text) or SLASH_PAIR.search(text) or JOINED_PAIR.search(text))


def name_from_line(line: str) -> str:
    """Nom porté par une ligne d'en-tête, ou « » (formule, mot-dièse, donnée « clé : valeur », date,
    ligne générique, phrase trop longue)."""
    bare = "".join(" " if unicodedata.category(ch) in ("So", "Sk") else ch
                   for ch in unicodedata.normalize("NFKC", line)).strip()
    if bare.startswith("#"):
        return ""
    text = _clean(line)
    if (not text or any(formula in text.upper() for formula in FORMULAS) or METADATA.match(text)
            or DATE_OR_TIME.search(text)):
        return ""
    text = PARTICLE_DASH.sub(lambda match: match.group(1) + "-", text)
    text = PREFIX.sub("", text.split(" - ")[0].strip()).strip(" -/.:")
    words = text.split()
    while words and words[-1].upper().strip(".:") in GENERIC:
        words.pop()
    while words and words[0].upper().strip(".:") in GENERIC:
        words.pop(0)
    name = " ".join(words).strip(" -/.:")
    if not name or len(name) > MAX_LENGTH or len(words) > MAX_WORDS:
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
    """Clé de regroupement : majuscules sans accents, sans espaces ni signes, sans TRADING/CRYPTO/TRADER."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).upper()
    words = re.findall(r"[^\W_]+", text)
    kept = [word for word in words if word not in KEY_NOISE] or words
    return "".join(kept)
