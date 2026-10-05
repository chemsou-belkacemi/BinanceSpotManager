"""Protection en cas de chute du marche.

Si BTC baisse d'au moins `drop_percent` % sur les `window_hours` dernieres heures (du plus haut des
clotures 15 minutes de la fenetre au dernier prix), les nouvelles entrees AUTOMATIQUES sont suspendues
`pause_hours` heures : les signaux recus restent dans la boite (comme pendant une suspension) et ne
partent ensuite que s'ils sont encore assez recents. En option (`tighten_stops`), les stops des
positions ouvertes en gain sont remontes a leur seuil de rentabilite (frais compris) au declenchement.

Rien ne vend : la protection ne fait que retenir les nouvelles entrees et, si demande, remonter des
stops par le chemin habituel du worker. Desactivee dans Settings pendant une pause, elle est levee
aussitot. Apres une pause, la baisse n'est mesuree qu'a partir du declenchement precedent : une meme
chute ne relance pas la pause, une nouvelle baisse de `drop_percent` % la relance.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .config import DATA_DIR

logger = logging.getLogger("bsm.market_guard")

SYMBOL = "BTCUSDT"
INTERVAL = "15m"
CHECK_EVERY_SECONDS = 300
PREFIX = "market_guard_"
GUARD_FILE = DATA_DIR / "market_guard.json"
DEFAULTS = {"enabled": True, "drop_percent": 3.0, "window_hours": 4.0, "pause_hours": 6.0, "tighten_stops": False}
BOUNDS = {"drop_percent": (0.5, 50.0), "window_hours": (1.0, 48.0), "pause_hours": (0.5, 72.0)}


@dataclass(frozen=True)
class GuardSettings:
    enabled: bool = True
    drop_percent: float = 3.0
    window_hours: float = 4.0
    pause_hours: float = 6.0
    tighten_stops: bool = False

    @classmethod
    def from_preferences(cls, preferences: Any) -> GuardSettings:
        prefs = preferences if isinstance(preferences, dict) else {}
        values: dict[str, Any] = {}
        for key, default in DEFAULTS.items():
            raw = prefs.get(PREFIX + key, default)
            if isinstance(default, bool):
                values[key] = bool(raw)
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                number = float(default)
            low, high = BOUNDS[key]
            values[key] = min(max(number, low), high) if number == number else float(default)
        return cls(**values)


def drop_over_window(klines: list[list[Any]], *, now_ms: int, window_hours: float,
                     last_price: Optional[float] = None,
                     not_before_ms: int = 0) -> Optional[tuple[float, float, float]]:
    """(baisse en %, plus haut des clotures de la fenetre, dernier prix) ; None si les bougies manquent.

    Seules les bougies cloturees comptent pour le plus haut, et aucune avant `not_before_ms` ; le
    dernier prix est `last_price` s'il est fourni, sinon la derniere cloture."""
    start_ms = max(now_ms - int(window_hours * 3_600_000), int(not_before_ms))
    closes = []
    for kline in klines:
        try:
            close_time, close = int(kline[6]), float(kline[4])
        except (TypeError, ValueError, IndexError):
            raise ValueError("bougie illisible") from None
        if close_time <= now_ms and close_time >= start_ms and close > 0:
            closes.append(close)
    if not closes:
        return None
    peak = max(closes)
    last = float(last_price) if last_price and last_price > 0 else closes[-1]
    return max(0.0, (peak - last) / peak * 100.0), peak, last


def active_status(path: Path = GUARD_FILE, now: Optional[float] = None) -> Optional[tuple[str, float]]:
    """(raison, fin de la pause en secondes) si la protection est active, sinon None (lecture seule,
    pour l'interface)."""
    from .position_store import read_json

    state = read_json(Path(path))
    if not isinstance(state, dict):
        return None
    until = float(state.get("active_until") or 0)
    if until <= (time.time() if now is None else now):
        return None
    return str(state.get("reason") or "Protection marché active"), until


class MarketGuard:
    """Etat persistant (fichier JSON) et controle periodique de la protection marche."""

    def __init__(self, klines: Callable[..., list], price: Callable[[str], float],
                 preferences: Callable[[], Any], *, path: Path = GUARD_FILE,
                 clock: Callable[[], float] = time.time,
                 read: Optional[Callable] = None, write: Optional[Callable] = None) -> None:
        from .position_store import atomic_write_json, read_json
        self.path = Path(path)
        self.klines = klines
        self.price = price
        self.preferences = preferences
        self.clock = clock
        self._read = read or read_json
        self._write = write or atomic_write_json
        self._next_check = 0.0

    def state(self) -> dict:
        raw = self._read(self.path)
        return raw if isinstance(raw, dict) else {}

    def active_reason(self, now: Optional[float] = None) -> str:
        """Raison de la suspension si la protection est active, sinon ""."""
        now = self.clock() if now is None else now
        state = self.state()
        until = float(state.get("active_until") or 0)
        if until > now:
            return str(state.get("reason") or "Protection marché active")
        return ""

    def check(self, now: Optional[float] = None) -> list[tuple[str, str]]:
        """Un controle (mesure de BTC au plus toutes les CHECK_EVERY_SECONDS) ; rend les evenements a
        annoncer : ("STARTED", detail), ("ENDED", detail)."""
        now = self.clock() if now is None else now
        settings = GuardSettings.from_preferences(self.preferences())
        state = self.state()
        events: list[tuple[str, str]] = []
        until = float(state.get("active_until") or 0)
        last_trigger = float(state.get("triggered_at") or state.get("last_triggered_at") or 0)
        if until and (until <= now or not settings.enabled):
            why = "fin de la pause" if until <= now else "protection désactivée dans Settings"
            events.append(("ENDED", f"Protection marché levée ({why}) : entrées automatiques à nouveau permises "
                                    f"(déclenchée par {state.get('reason') or 'une chute de BTC'})."))
            self._write(self.path, {"active_until": 0, "reason": "", "ended_at": now,
                                    "last_triggered_at": last_trigger})
            until = 0
        if not settings.enabled or until > now or now < self._next_check:
            return events
        self._next_check = now + CHECK_EVERY_SECONDS
        now_ms = int(now * 1000)
        try:
            bars = self.klines(SYMBOL, INTERVAL, limit=int(settings.window_hours * 4) + 2)
            last_price = self.price(SYMBOL)
            measured = drop_over_window(bars, now_ms=now_ms, window_hours=settings.window_hours,
                                        last_price=last_price, not_before_ms=int(last_trigger * 1000))
        except Exception as exc:  # noqa: BLE001 - une donnee manquante ne declenche rien
            logger.warning("Protection marché : contrôle impossible (%s)", exc)
            return events
        if measured is None:
            return events
        drop, peak, last = measured
        if drop < settings.drop_percent:
            return events
        until = now + settings.pause_hours * 3600
        reason = f"BTC −{drop:.1f} % en {settings.window_hours:g} h ({peak:,.0f} → {last:,.0f})".replace(",", " ")
        self._write(self.path, {"active_until": until, "reason": reason, "triggered_at": now,
                                "drop_percent": round(drop, 3), "tighten_stops": settings.tighten_stops})
        until_text = time.strftime("%d/%m %H:%M UTC", time.gmtime(until))
        events.append(("STARTED", f"{reason} : nouvelles entrées automatiques suspendues jusqu'au {until_text}"
                                  + (" ; stops des positions en gain remontés au seuil de rentabilité"
                                     if settings.tighten_stops else "") + "."))
        return events
