"""Garde-fou « Feu de protection CSI » : lit le feu de protection du marché de CryptoSignalIntelligence (`GET /meteo`).

C'est un OUTIL DE GESTION DU RISQUE, comme la perte maximale du jour : ce n'est PAS une stratégie et AUCUN GAIN
n'est démontré (règle dans docs/METEO_PROTECTION.md de CSI). L'étude en préparation côté CSI (branche
recherche/meteo) teste une règle voisine, pas ce feu : il ne sera mesuré que par son propre journal.
Désactivé par défaut (Settings → Worker & risque).

Effets, seulement quand le garde-fou est activé :
- ROUGE : aucune nouvelle entrée automatique (défaut) ou aucune nouvelle entrée, manuelle ou automatique ;
- ORANGE : taille des signaux automatiques réduite à X % (défaut 50 %, même calcul que le trader perdant) ou rien ;
- CSI injoignable ou feu INCONNU : aucune action (défaut : BSM tourne sur un VPS où CSI n'est pas forcément
  joignable) ou « prudence » (traité comme ORANGE).

Rien n'est vendu ni annulé : les positions ouvertes restent suivies (stops, objectifs, clôtures). Les signaux
retenus restent dans la boîte et ne partent ensuite que s'ils sont encore assez récents, comme pour la protection
en cas de chute de BTC (market_guard.py).

Le worker interroge CSI au plus toutes les CACHE_SECONDS secondes (délais courts) ; une lecture réussie reste valable
STALE_SECONDS secondes (âge vérifié à chaque tour) si CSI ne répond plus, pour ne pas lever un rouge sur une simple
coupure, y compris après un redémarrage du worker. L'état est écrit dans `data/csi_light.json` (bandeau du Dashboard,
dernière lecture réussie, ROUGE en cours sans second « début » après un redémarrage), seulement quand il change ou
au plus toutes les REFRESH_SECONDS secondes quand le garde-fou est activé ; désactivé, rien n'est écrit. Une
écriture impossible (disque plein…) est journalisée une fois et ne fige jamais l'effet du feu.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .signal_sizing import ChannelPolicy

logger = logging.getLogger("bsm.csi_light")

PREFIX = "csi_light_"
STATE_FILE = DATA_DIR / "csi_light.json"
CACHE_SECONDS = 300
STALE_SECONDS = 900
#: (connexion, lecture) pour `requests` : chaque délai vaut séparément ; la résolution DNS n'est PAS bornée (une
#: adresse `BSM_CSI_API_URL` en IP ou un nom résolu localement, comme `csi-api` dans Compose, évite l'attente).
REQUEST_TIMEOUT_SECONDS = (2.0, 3.0)
#: Écriture de l'état : quand il change, et au plus toutes les REFRESH_SECONDS sinon (fraîcheur du bandeau).
REFRESH_SECONDS = 300
MAX_TEXT = 400
TRUE_TEXTS = frozenset({"true", "1", "oui", "yes", "on", "vrai"})
#: Au-delà, l'état écrit par le worker n'est plus affiché (worker arrêté).
DISPLAY_MAX_AGE_SECONDS = 1800

GREEN, ORANGE, RED, UNKNOWN = "VERT", "ORANGE", "ROUGE", "INCONNU"
UNREACHABLE, DISABLED = "INJOIGNABLE", "DESACTIVE"
RED_ACTIONS = {"AUTO": "Aucune nouvelle entrée automatique",
               "ALL": "Aucune nouvelle entrée, manuelle ou automatique"}
ORANGE_ACTIONS = {"REDUCE": "Taille réduite", "NONE": "Aucune action"}
UNAVAILABLE_ACTIONS = {"NONE": "Aucune action", "CAUTION": "Prudence (traiter comme orange)"}
KEPT_BOUNDS = (10.0, 90.0)
DEFAULTS: dict[str, Any] = {"enabled": False, "red_action": "AUTO", "orange_action": "REDUCE", "orange_kept_percent": 50.0,
            "when_unavailable": "NONE"}
NOTE = "Outil de prudence, aucun gain démontré ; étude en cours."


def _enabled(value: Any) -> bool:
    """Interrupteur lu correctement : `bool("false")` vaudrait True ; un texte n'active que s'il dit oui."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in TRUE_TEXTS
    return False


def clean_reading(reading: Any) -> dict | None:
    """Ce qui est gardé d'une réponse de `GET /meteo` : couleur, explication et heure de calcul, en textes seulement
    (un NaN ou un objet inattendu ne peut ni casser l'écriture JSON ni passer pour une couleur). None si ce n'est
    pas un objet JSON."""
    if not isinstance(reading, dict):
        return None
    out = {}
    for key in ("color", "explanation", "computed_at"):
        value = reading.get(key)
        out[key] = value[:MAX_TEXT] if isinstance(value, str) else ""
    return out


@dataclass(frozen=True)
class LightPolicy:
    """Réglages du garde-fou (data/settings.json, clés `csi_light_*`)."""

    enabled: bool = False
    red_action: str = "AUTO"
    orange_action: str = "REDUCE"
    kept_percent: float = 50.0
    when_unavailable: str = "NONE"

    @classmethod
    def from_mapping(cls, preferences: Any) -> LightPolicy:
        prefs = preferences if isinstance(preferences, dict) else {}

        def choice(key: str, allowed: dict) -> str:
            value = str(prefs.get(PREFIX + key, DEFAULTS[key])).upper()
            return value if value in allowed else str(DEFAULTS[key])

        try:
            kept = float(prefs.get(PREFIX + "orange_kept_percent", DEFAULTS["orange_kept_percent"]))
        except (TypeError, ValueError):
            kept = float(DEFAULTS["orange_kept_percent"])
        if math.isnan(kept):
            kept = float(DEFAULTS["orange_kept_percent"])
        return cls(enabled=_enabled(prefs.get(PREFIX + "enabled", False)), red_action=choice("red_action", RED_ACTIONS),
                   orange_action=choice("orange_action", ORANGE_ACTIONS),
                   kept_percent=min(max(kept, KEPT_BOUNDS[0]), KEPT_BOUNDS[1]),
                   when_unavailable=choice("when_unavailable", UNAVAILABLE_ACTIONS))

    def reduced(self, budget: float) -> float:
        """Même calcul que la réduction du trader perdant (ChannelPolicy.reduced)."""
        return ChannelPolicy(action="REDUCE", kept_percent=self.kept_percent).reduced(budget)


@dataclass(frozen=True)
class LightEffect:
    """Ce que le feu fait maintenant : `color` est celle de CSI, INJOIGNABLE ou DESACTIVE."""

    color: str = DISABLED
    block_auto: bool = False
    block_manual: bool = False
    kept_percent: float | None = None
    detail: str = ""

    @property
    def active(self) -> bool:
        return self.block_auto or self.block_manual or self.kept_percent is not None


def effect_of(policy: LightPolicy, reading: dict | None, failure: str = "") -> LightEffect:
    """Effet du feu (fonction pure). `reading` = réponse de `GET /meteo`, None si CSI est injoignable (`failure`)."""
    if not policy.enabled:
        return LightEffect(DISABLED, detail="feu CSI désactivé dans Settings")
    color = str((reading or {}).get("color") or "").upper() if reading is not None else UNREACHABLE
    explanation = str((reading or {}).get("explanation") or "").strip()
    if color not in (GREEN, ORANGE, RED, UNKNOWN, UNREACHABLE):
        color, explanation = UNKNOWN, f"couleur illisible ({color or 'absente'})"
    if color == RED:
        manual = policy.red_action == "ALL"
        what = "aucune nouvelle entrée, manuelle ou automatique" if manual else "aucune nouvelle entrée automatique"
        return LightEffect(RED, block_auto=True, block_manual=manual,
                           detail=f"Feu CSI ROUGE : {what} ; positions ouvertes suivies. {explanation}".strip())
    caution = color in (UNKNOWN, UNREACHABLE) and policy.when_unavailable == "CAUTION"
    if color == ORANGE or caution:
        why = (explanation if color == ORANGE else
               f"feu CSI {'injoignable' if color == UNREACHABLE else 'INCONNU'} ({failure or explanation or 'sans détail'}), "
               "traité comme orange par prudence")
        if policy.orange_action == "REDUCE":
            return LightEffect(color, kept_percent=policy.kept_percent,
                               detail=f"Feu CSI {color} : taille des signaux automatiques réduite à "
                                      f"{policy.kept_percent:g} %. {why}".strip())
        return LightEffect(color, detail=f"Feu CSI {color} : aucune action (réglage). {why}".strip())
    if color in (UNKNOWN, UNREACHABLE):
        why = failure or explanation or "sans détail"
        return LightEffect(color, detail=f"Feu CSI {'injoignable' if color == UNREACHABLE else 'INCONNU'} ({why}) : "
                                         "aucune action (réglage par défaut).")
    return LightEffect(GREEN, detail=f"Feu CSI VERT : aucune restriction. {explanation}".strip())


def active_status(path: Path = STATE_FILE, now: float | None = None) -> dict | None:
    """État écrit par le worker si le feu bloque ou réduit maintenant (lecture seule, pour le Dashboard), sinon None.
    Un état plus vieux que DISPLAY_MAX_AGE_SECONDS (worker arrêté) n'est pas affiché."""
    from .position_store import read_json

    state = read_json(Path(path))
    if not isinstance(state, dict) or not state.get("active"):
        return None
    try:
        checked = float(state.get("checked_at") or 0)
    except (TypeError, ValueError):
        return None
    if (time.time() if now is None else now) - checked > DISPLAY_MAX_AGE_SECONDS:
        return None
    return state


class CsiLightGuard:
    """Lecture du feu (cache), effet courant, état persistant et événements de début et de fin d'un ROUGE."""

    def __init__(self, client: Any, preferences: Callable[[], Any], *, path: Path = STATE_FILE,
                 clock: Callable[[], float] = time.time,
                 read: Callable | None = None, write: Callable | None = None) -> None:
        from .position_store import atomic_write_json, read_json
        self.client = client
        self.preferences = preferences
        self.path = Path(path)
        self.clock = clock
        self._read = read or read_json
        self._write = write or atomic_write_json
        self._reading: dict | None = None
        self._read_at = 0.0
        self._next_fetch = 0.0
        self._failure = ""
        self._effect = LightEffect()
        self._state: dict | None = None           # état connu (fichier relu une fois au premier contrôle)
        self._written_at = 0.0
        self._write_failed = False

    def state(self) -> dict:
        """État enregistré (lu au premier appel, puis tenu en mémoire : une écriture ratée ne fait rien oublier)."""
        if self._state is None:
            try:
                raw = self._read(self.path)
            except Exception:  # noqa: BLE001 - fichier illisible : on repart d'un état vide
                raw = None
            self._state = raw if isinstance(raw, dict) else {}
            self._restore(self._state)
        return self._state

    def _restore(self, state: dict) -> None:
        """Après un redémarrage : la dernière lecture réussie reprend avec son heure (même délai de grâce)."""
        reading = clean_reading(state.get("last_reading"))
        try:
            read_at = float(state.get("read_at") or 0)
        except (TypeError, ValueError):
            read_at = 0.0
        if reading is not None and math.isfinite(read_at) and read_at > 0:
            self._reading, self._read_at = reading, read_at

    def _fetch(self, now: float) -> None:
        if now < self._next_fetch:
            return
        self._next_fetch = now + CACHE_SECONDS
        try:
            if self.client is None or not hasattr(self.client, "meteo"):
                raise RuntimeError("aucun client CSI configuré")
            reading = clean_reading(self.client.meteo(timeout=REQUEST_TIMEOUT_SECONDS))
            if reading is None:
                raise TypeError("réponse inattendue de CSI")
        except Exception as exc:  # noqa: BLE001 - CSI ne doit jamais arrêter le worker
            self._failure = (str(exc) or exc.__class__.__name__)[:MAX_TEXT]
            logger.info("Feu CSI indisponible : %s", self._failure)
            return
        self._reading, self._read_at, self._failure = reading, now, ""

    def check(self, now: float | None = None) -> list[tuple[str, str]]:
        """Un contrôle (CSI interrogé au plus toutes les CACHE_SECONDS) ; rend les événements à annoncer :
        ("STARTED", détail) au début d'un ROUGE qui bloque, ("ENDED", détail) à sa fin. Ne lève jamais pour une
        écriture impossible."""
        now = self.clock() if now is None else now
        previous = self.state()
        policy = LightPolicy.from_mapping(self.preferences())
        if policy.enabled:
            self._fetch(now)
            if self._reading is not None and not (0 <= now - self._read_at <= STALE_SECONDS):
                self._reading = None                    # lecture trop ancienne (vérifié à chaque tour) : injoignable
                self._failure = self._failure or "dernière lecture de CSI trop ancienne"
        else:
            self._next_fetch = 0.0                      # réactivé : lecture immédiate
        self._effect = effect_of(policy, self._reading if policy.enabled else None, self._failure)
        was_red = bool(previous.get("red_active"))
        is_red = self._effect.block_auto
        events: list[tuple[str, str]] = []
        if is_red and not was_red:
            events.append(("STARTED", self._effect.detail))
        elif was_red and not is_red:
            now_text = {DISABLED: "garde-fou désactivé dans Settings", UNREACHABLE: "CSI injoignable",
                        UNKNOWN: "feu INCONNU"}.get(self._effect.color, f"feu {self._effect.color}")
            events.append(("ENDED", (f"Feu CSI n'est plus rouge ({now_text}) : nouvelles entrées à nouveau permises "
                                     "par ce garde-fou.")))
        red_since = previous.get("red_since") if was_red else now
        content = {"enabled": policy.enabled, "color": self._effect.color, "active": self._effect.active,
                   "red_active": is_red, "block_auto": self._effect.block_auto,
                   "block_manual": self._effect.block_manual, "kept_percent": self._effect.kept_percent,
                   "detail": self._effect.detail, "failure": self._failure,
                   "csi_computed_at": (self._reading or {}).get("computed_at", ""),
                   "red_since": red_since if is_red else None,
                   "last_reading": self._reading, "read_at": self._read_at if self._reading is not None else 0.0}
        already_off = (not policy.enabled and previous.get("enabled") in (None, False)
                       and not previous.get("active") and not previous.get("red_active"))
        unchanged = already_off or {k: previous.get(k) for k in content} == content
        due = policy.enabled and now - self._written_at >= REFRESH_SECONDS
        if not unchanged or due:
            self._save(content | {"checked_at": now}, now)
        return events

    def _save(self, state: dict, now: float) -> None:
        self._state = state                           # en mémoire d'abord : l'effet et les événements suivent toujours
        try:
            self._write(self.path, state)
        except Exception as exc:  # noqa: BLE001 - disque plein, valeur illisible… : jamais bloquant
            if not self._write_failed:
                logger.warning("Feu CSI : état non enregistré dans %s (%s) ; l'effet reste appliqué",
                               self.path.name, exc.__class__.__name__)
            self._write_failed = True
            return
        self._written_at, self._write_failed = now, False

    def effect(self) -> LightEffect:
        """Effet du dernier contrôle (aucun appel réseau)."""
        return self._effect

    def auto_refusal(self) -> str:
        return self._effect.detail if self._effect.block_auto else ""

    def manual_refusal(self) -> str:
        """Raison de refuser une nouvelle entrée manuelle (ROUGE réglé sur « manuelle ou automatique »)."""
        return self._effect.detail if self._effect.block_manual else ""

    def status_line(self) -> str:
        """Ligne de /statut (Telegram)."""
        effect = self._effect
        if effect.color == DISABLED:
            return "Feu CSI : désactivé"
        return f"Feu CSI : {effect.color} — {effect.detail}"
