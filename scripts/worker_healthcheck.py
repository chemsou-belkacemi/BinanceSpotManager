"""Healthcheck Docker du worker : code 0 si son heartbeat est recent.

Un worker en veille (arret demande depuis le Dashboard) reste sain : il
continue d'ecrire son heartbeat. Aucune requete Binance n'est envoyee.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_spot_manager.bot_process_manager import BotProcessManager  # noqa: E402


def main() -> int:
    status = BotProcessManager().status()
    if status.pid_alive:
        return 0
    print(f"Heartbeat absent ou trop ancien ({status.heartbeat_age}s, etat {status.state})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
