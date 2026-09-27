"""Prix publics Spot Testnet via un seul WebSocket, avec cache borne dans le temps.

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
MAX_PRICE_AGE_SECONDS = 5.0
IDLE_TIMEOUT_SECONDS = 60.0
MAX_WATCHED_SYMBOLS = 100


class DemoMarketPriceStream:
    """Un flux par process ; reconnexion automatique et aucune valeur perimee."""

    def __init__(
        self, settings: Settings, *, connect: Optional[Callable] = None,
    ) -> None:
        self.enabled = settings.is_demo and settings.base_url == "https://testnet.binance.vision"
        if connect is None and self.enabled:
            try:
                from websockets.sync.client import connect as websocket_connect
            except ImportError:
                logger.warning("websockets absent : prix REST conserves")
                self.enabled = False
            else:
                connect = websocket_connect
        self._connect = connect
        self._lock = threading.Lock()
        self._watched: set[str] = set()
        self._latest: dict[str, tuple[float, float]] = {}
        self._thread: Optional[threading.Thread] = None
        self._last_access = time.monotonic()

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
        backoff = 1.0
        while True:
            with self._lock:
                if time.monotonic() - self._last_access > IDLE_TIMEOUT_SECONDS:
                    return
            try:
                assert self._connect is not None
                with self._connect(
                    TESTNET_STREAM_URL, open_timeout=5,
                    ping_interval=20, ping_timeout=20,
                ) as connection:
                    backoff = 1.0
                    subscribed: set[str] = set()
                    while True:
                        with self._lock:
                            if time.monotonic() - self._last_access > IDLE_TIMEOUT_SECONDS:
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
                logger.warning("Flux prix Demo indisponible : %s", exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
