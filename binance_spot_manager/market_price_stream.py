"""Prix publics Spot Demo/Testnet via un WebSocket, avec cache borne dans le temps.

Ce module ne transmet jamais d'ordre et ne se connecte jamais au Spot Live.
Le consommateur doit utiliser REST lorsque ``prices`` ne fournit pas un symbole.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Callable, Optional

from .config import Settings

logger = logging.getLogger("bsm.market_stream")

TESTNET_STREAM_URL = "wss://stream.testnet.binance.vision/ws"
DEMO_STREAM_URL = "wss://demo-stream.binance.com/ws"
DEMO_STREAM_URLS = {
    "https://testnet.binance.vision": TESTNET_STREAM_URL,
    "https://demo-api.binance.com": DEMO_STREAM_URL,
}
MAX_PRICE_AGE_SECONDS = 5.0
IDLE_TIMEOUT_SECONDS = 60.0
MAX_WATCHED_SYMBOLS = 100


class DemoMarketPriceStream:
    """Un flux par process ; reconnexion automatique et aucune valeur perimee."""

    def __init__(
        self, settings: Settings, *, connect: Optional[Callable] = None,
    ) -> None:
        self._stream_url = DEMO_STREAM_URLS.get(settings.base_url) if settings.is_demo else None
        self.enabled = self._stream_url is not None
        self._disabled_reason = "" if self.enabled else "Hote non compatible avec le flux Demo"
        if connect is None and self.enabled:
            try:
                from websockets.sync.client import connect as websocket_connect
            except ImportError:
                logger.warning("websockets absent : prix REST conserves")
                self.enabled = False
                self._disabled_reason = "Dependance websockets absente : fallback REST"
            else:
                connect = websocket_connect
        self._connect = connect
        self._lock = threading.Lock()
        self._watched: set[str] = set()
        self._latest: dict[str, tuple[float, float]] = {}
        self._thread: Optional[threading.Thread] = None
        self._last_access = time.monotonic()
        self._state = "IDLE" if self.enabled else "DISABLED"
        self._last_error = ""
        self._reconnects = 0

    def snapshot(self) -> dict:
        """Diagnostic sans connexion, souscription ni prolongation du flux."""
        now = time.monotonic()
        with self._lock:
            return {
                "captured_at": time.time(),
                "state": self._state,
                "disabled_reason": self._disabled_reason,
                "last_error": self._last_error,
                "reconnects": self._reconnects,
                "symbols": [
                    {
                        "symbol": symbol,
                        "age_seconds": now - self._latest[symbol][1] if symbol in self._latest else None,
                        "fresh": symbol in self._latest and now - self._latest[symbol][1] <= MAX_PRICE_AGE_SECONDS,
                    }
                    for symbol in sorted(self._watched)
                ],
            }

    def prices(self, symbols: list[str]) -> dict[str, float]:
        """Souscrit aux symboles et ne retourne que les prix recents."""
        if not self.enabled:
            return {}
        wanted = {symbol.upper() for symbol in symbols if symbol}
        if not wanted:
            return {}
        now = time.monotonic()
        with self._lock:
            room = MAX_WATCHED_SYMBOLS - len(self._watched)
            if room > 0:
                self._watched.update(sorted(wanted - self._watched)[:room])
            self._last_access = now
            fresh = {
                symbol: price for symbol in wanted
                if (item := self._latest.get(symbol)) is not None
                and now - item[1] <= MAX_PRICE_AGE_SECONDS
                for price in [item[0]]
            }
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="bsm-demo-prices", daemon=True
                )
                self._thread.start()
        return fresh

    def _accept(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and "data" in payload:
                payload = payload["data"]
            if not isinstance(payload, dict) or payload.get("e") != "24hrMiniTicker":
                return
            symbol = payload.get("s")
            price = float(payload.get("c", 0))
            if not isinstance(symbol, str) or not math.isfinite(price) or price <= 0:
                return
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        with self._lock:
            if symbol in self._watched:
                self._latest[symbol] = (price, time.monotonic())

    def _run(self) -> None:
        if not self.enabled:
            return
        backoff = 1.0
        while True:
            with self._lock:
                if time.monotonic() - self._last_access > IDLE_TIMEOUT_SECONDS:
                    self._state = "IDLE"
                    return
                self._state = "CONNECTING"
            try:
                assert self._connect is not None
                with self._connect(
                    self._stream_url, open_timeout=5,
                    ping_interval=20, ping_timeout=20,
                ) as connection:
                    with self._lock:
                        self._state = "CONNECTED"
                    backoff = 1.0
                    subscribed: set[str] = set()
                    while True:
                        with self._lock:
                            if time.monotonic() - self._last_access > IDLE_TIMEOUT_SECONDS:
                                self._state = "IDLE"
                                return
                            pending = self._watched - subscribed
                        if pending:
                            connection.send(json.dumps({
                                "method": "SUBSCRIBE",
                                "params": [f"{s.lower()}@miniTicker" for s in sorted(pending)],
                                "id": 1,
                            }))
                            subscribed.update(pending)
                        try:
                            raw = connection.recv(timeout=1)
                        except TimeoutError:
                            continue
                        self._accept(raw)
            except Exception as exc:  # noqa: BLE001 - le fallback REST reste disponible
                with self._lock:
                    self._state = "RECONNECTING"
                    self._last_error = str(exc)[:300]
                    self._reconnects += 1
                logger.warning("Flux prix Demo indisponible : %s", exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
