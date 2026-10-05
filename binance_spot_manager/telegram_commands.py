"""Commandes Telegram du propriétaire : /pause, /reprise, /statut, /aide.

Acceptées seulement dans la conversation PRIVÉE avec le bot et depuis le compte du propriétaire (identifiant
Telegram réglé dans Settings → Notifications ; par défaut, la conversation des notifications si c'est une
conversation privée, dont l'identifiant est celui du propriétaire). Tout autre message est ignoré, sans réponse.

- /pause : plus aucune nouvelle entrée, manuelle ou automatique, jusqu'à /reprise (état gardé dans
  `data/pause_manuelle.json`, un redémarrage ne lève rien). Les positions ouvertes restent suivies : rien n'est
  vendu ni annulé.
- /reprise : lève la pause manuelle (pas la perte maximale du jour, ni la protection marché, ni la licence).
- /statut : état du worker, positions ouvertes, latent, blocages en cours.

Le même lecteur getUpdates que les signaux lit les commandes (Telegram n'accepte qu'un lecteur par bot).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .config import DATA_DIR

logger = logging.getLogger("bsm.telegram_commands")

PAUSE_FILE = DATA_DIR / "pause_manuelle.json"
OWNER_KEY = "telegram_owner_id"
ENABLED_KEY = "telegram_commands_enabled"
HELP = ("Commandes : /pause (plus aucune nouvelle entrée), /reprise (lever la pause), /statut (état du bot). "
        "Les positions ouvertes restent toujours suivies.")


class ManualPause:
    """Pause manuelle persistante des nouvelles entrées."""

    def __init__(self, path: Path = PAUSE_FILE, *, clock: Callable[[], float] = time.time,
                 read: Optional[Callable] = None, write: Optional[Callable] = None) -> None:
        from .position_store import atomic_write_json, read_json
        self.path = Path(path)
        self.clock = clock
        self._read = read or read_json
        self._write = write or atomic_write_json

    def refusal(self) -> str:
        state = self._read(self.path)
        if isinstance(state, dict) and state.get("paused"):
            try:
                since = time.strftime("%d/%m %H:%M UTC", time.gmtime(float(state.get("since") or 0)))
            except (TypeError, ValueError, OverflowError):
                since = "?"
            return (f"Pause manuelle depuis le {since} ({state.get('by') or 'Telegram'}) : aucune nouvelle entrée ; "
                    "/reprise pour reprendre. Les positions ouvertes restent suivies")
        return ""

    def set(self, paused: bool, *, by: str) -> None:
        self._write(self.path, {"paused": bool(paused), "since": self.clock(), "by": by})


def owner_id(preferences: Any, default_chat: Any) -> Optional[int]:
    """Identifiant Telegram du propriétaire : réglage explicite, sinon la conversation des notifications si elle
    est privée (identifiant positif) ; None si les commandes sont désactivées ou l'identifiant inconnu."""
    prefs = preferences if isinstance(preferences, dict) else {}
    if not prefs.get(ENABLED_KEY, True):
        return None
    for value in (prefs.get(OWNER_KEY), default_chat):
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


class TelegramCommands:
    def __init__(self, preferences: Callable[[], Any], default_chat: Any, pause: ManualPause,
                 status: Callable[[], str]) -> None:
        self.preferences, self.default_chat, self.pause, self.status = preferences, default_chat, pause, status

    def owner(self) -> Optional[int]:
        return owner_id(self.preferences(), self.default_chat)

    def handle(self, message: dict, *, edited: bool = False) -> Optional[str]:
        """Réponse à envoyer si le message est une commande du propriétaire, sinon None (ignoré)."""
        owner = self.owner()
        text = str(message.get("text") or "").strip()
        chat = message.get("chat") or {}
        sender = (message.get("from") or {}).get("id")
        if (edited or owner is None or not text.startswith("/") or chat.get("type") != "private"
                or chat.get("id") != owner or sender != owner):
            return None
        command = text.split()[0].split("@")[0].lower()
        if command == "/pause":
            self.pause.set(True, by="Telegram /pause")
            logger.warning("Pause manuelle demandée par Telegram")
            return ("Pause activée : plus aucune nouvelle entrée (manuelle ou automatique). Les positions ouvertes "
                    "restent suivies. /reprise pour reprendre.")
        if command in {"/reprise", "/resume"}:
            self.pause.set(False, by="Telegram /reprise")
            logger.warning("Pause manuelle levée par Telegram")
            return "Pause levée : les nouvelles entrées sont à nouveau permises (sauf autre blocage, voir /statut)."
        if command in {"/statut", "/status"}:
            try:
                return self.status()
            except Exception:  # noqa: BLE001 - une réponse plutôt qu'un silence
                logger.exception("Statut indisponible")
                return "Statut indisponible pour l'instant (voir les journaux du worker)."
        return HELP
