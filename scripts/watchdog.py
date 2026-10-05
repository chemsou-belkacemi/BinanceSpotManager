"""Service de surveillance du worker (« bot muet ») : voir binance_spot_manager/watchdog.py.

Lancé par Docker Compose (service `watchdog`), données en lecture seule. Ne passe aucun ordre.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.config import BOT_RUNTIME_FILE, BOT_STOP_FLAG, POSITIONS_DIR, get_settings  # noqa: E402
from binance_spot_manager.models import BotRuntime, Position  # noqa: E402
from binance_spot_manager.notification_engine import NotificationEngine  # noqa: E402
from binance_spot_manager.position_store import read_json  # noqa: E402
from binance_spot_manager.watchdog import CHECK_SECONDS, Watchdog  # noqa: E402

logger = logging.getLogger("bsm.watchdog")


def read_runtime() -> BotRuntime:
    """Lecture seule (aucun dossier créé) : un fichier absent ou illisible vaut « jamais vu »."""
    raw = read_json(BOT_RUNTIME_FILE)
    try:
        return BotRuntime.model_validate(raw) if isinstance(raw, dict) else BotRuntime()
    except Exception:  # noqa: BLE001
        return BotRuntime()


def read_positions() -> list[Position]:
    positions = []
    for path in sorted(POSITIONS_DIR.glob("*.json")):
        raw = read_json(path)
        if isinstance(raw, dict):
            try:
                positions.append(Position.model_validate(raw))
            except Exception:  # noqa: BLE001 - une position illisible n'empêche pas l'alerte
                logger.warning("Position illisible : %s", path.name)
    return positions


def ping(url: str) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10):  # noqa: S310
        pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    engine = NotificationEngine(settings)
    if not engine.active_channels:
        logger.warning("Aucun canal de notification configuré : la surveillance ne peut prévenir personne")
    url = os.environ.get("BSM_HEALTHCHECK_URL", "").strip()
    if url and not url.startswith("https://"):
        logger.error("BSM_HEALTHCHECK_URL doit commencer par https:// : signal externe désactivé")
        url = ""
    watchdog = Watchdog(read_runtime, read_positions, engine.notify, engine, standby_flag=BOT_STOP_FLAG,
                        ping=ping, ping_url=url)
    running = True

    def stop(_signum, _frame):  # pragma: no cover - dépend du signal
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("Surveillance du worker démarrée (signal externe : %s)", "oui" if url else "non")
    while running:
        try:
            watchdog.check()
        except Exception:  # noqa: BLE001 - la surveillance ne s'arrête jamais sur une erreur
            logger.exception("Contrôle interrompu")
        for _ in range(CHECK_SECONDS):
            if not running:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
