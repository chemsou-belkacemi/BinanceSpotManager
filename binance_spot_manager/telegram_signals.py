"""Opt-in Telegram inbox import, with one durable long-polling reader."""
import hashlib
import logging
import re
import threading
import time

import requests

from .signal_inbox import CsiChannelRefused, SignalInbox


logger = logging.getLogger("bsm.telegram")
POLL_TIMEOUT_SECONDS = 20
BACKOFF_SECONDS = (2, 5, 10, 30, 60)


def origin_label(message) -> str:
    """Nom du canal (ou de l'auteur) d'origine d'un message Telegram, pour le suivi par canal.

    Message transféré : canal, groupe ou personne d'origine (forward_origin, ou anciens champs
    forward_from_*). Sinon : la conversation elle-même (un canal ou un groupe autorisé). Un texte copié
    par un relais sans transfert n'a que le nom de la conversation du relais.
    """
    origin = message.get("forward_origin") or {}
    kind = origin.get("type")
    name = ""
    if kind == "channel":
        chat = origin.get("chat") or {}
        name = chat.get("title") or chat.get("username") or ""
    elif kind == "chat":
        chat = origin.get("sender_chat") or {}
        name = chat.get("title") or chat.get("username") or ""
    elif kind == "user":
        user = origin.get("sender_user") or {}
        name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part) or user.get("username") or ""
    elif kind == "hidden_user":
        name = origin.get("sender_user_name") or ""
    if not name:
        legacy = message.get("forward_from_chat") or {}
        name = legacy.get("title") or legacy.get("username") or message.get("forward_sender_name") or ""
    if not name and message.get("forward_from"):
        user = message["forward_from"]
        name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part) or user.get("username") or ""
    if not name:
        chat = message.get("chat") or {}
        name = chat.get("title") or chat.get("username") or ""
    return " ".join(str(name).split())[:80]


def chat_allowlist(text):
    values = [part for part in re.split(r"[,;\s]+", text.strip()) if part]
    if not values or any(not re.fullmatch(r"-?\d+", value) for value in values):
        raise ValueError("Indiquer au moins un identifiant numérique de conversation Telegram.")
    return {int(value) for value in values}


def import_telegram(
    token, allowed_chats, inbox, scope, *, session=None, poll_timeout=0,
    request_timeout=None,
):
    if not token or not allowed_chats:
        raise ValueError("Token et conversations autorisées requis ; réception désactivée.")
    poll_timeout = max(0, min(int(poll_timeout), 50))
    request_timeout = request_timeout or (3, max(5, poll_timeout + 5))
    bot = hashlib.sha256(token.encode()).hexdigest()
    client = session or requests.Session()
    try:
        response = client.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": inbox.offset(bot), "timeout": poll_timeout, "limit": 100,
                    "allowed_updates": '["message","channel_post","edited_message","edited_channel_post"]'},
            timeout=request_timeout,
        )
        data = response.json()
        if response.status_code != 200 or not data.get("ok"):
            # Do not expose request URLs / tokens through exception messages.
            raise ValueError("Réception Telegram indisponible : vérifier le token, le webhook et les autres lecteurs getUpdates.")
        updates = data.get("result")
        if not isinstance(updates, list):
            raise ValueError("Réponse Telegram invalide")
    except requests.RequestException:
        raise ValueError("Connexion Telegram impossible (détails sensibles masqués).") from None
    except (TypeError, KeyError):
        raise ValueError("Réponse Telegram invalide") from None
    finally:
        if session is None:
            client.close()
    received = []
    for update in sorted(updates, key=lambda item: item["update_id"]):
        message = next((update[k] for k in ("message", "channel_post", "edited_message", "edited_channel_post") if k in update), {})
        chat = message.get("chat", {}).get("id")
        raw = message.get("text") or message.get("caption")
        if chat in allowed_chats and raw:
            origin = message.get("forward_origin") or {}
            source_timestamp = origin.get("date") or message.get("date") or 0
            try:
                received.append(inbox.receive(scope, raw, source="telegram",
                    external_id=f"{bot}:{chat}:{message['message_id']}",
                    edited="edited_message" in update or "edited_channel_post" in update,
                    source_timestamp=source_timestamp, origin=origin_label(message)))
            except CsiChannelRefused:
                # Un signal CSI ne passe que par le dépôt TXT : message ignoré, offset avancé.
                logger.warning("Message Telegram %s ignoré : signal CSI hors dépôt TXT", message["message_id"])
        # Persist only AFTER saving the message. A retry remains idempotent.
        inbox.advance(bot, int(update["update_id"]) + 1)
    return received


class TelegramSignalPoller:
    """Unique background getUpdates reader; it only writes to SignalInbox."""

    def __init__(
        self, token, scope, preferences_loader, *, inbox=None, session=None,
        poll_timeout=POLL_TIMEOUT_SECONDS, pause_requested=None, clock=time.time,
    ):
        self.token = token
        self.scope = scope
        self.preferences_loader = preferences_loader
        self.inbox = inbox or SignalInbox()
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.poll_timeout = max(1, min(int(poll_timeout), 50))
        self.pause_requested = pause_requested or (lambda: False)
        self.clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._diagnostics = {
            "state": "STOPPED",
            "running": False,
            "last_poll_at": None,
            "last_received_at": None,
            "last_batch_count": 0,
            "received_total": 0,
            "failures": 0,
            "last_error": "",
            "poll_timeout_seconds": self.poll_timeout,
        }

    def _update(self, **values):
        with self._lock:
            self._diagnostics.update(values)

    def snapshot(self):
        with self._lock:
            return dict(self._diagnostics)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="bsm-telegram-getupdates", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout=None):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout or self.poll_timeout + 5)
        if self._owns_session:
            self.session.close()
        self._update(state="STOPPED", running=False)

    def poll_once(self):
        preferences = self.preferences_loader()
        preferences = preferences if isinstance(preferences, dict) else {}
        enabled = bool(preferences.get("signal_telegram_enabled", False))
        automatic = bool(preferences.get("signal_telegram_auto_enabled", False))
        if not enabled:
            self._update(state="DISABLED", last_error="")
            return []
        if not automatic:
            self._update(state="MANUAL", last_error="")
            return []
        if not self.token:
            raise ValueError("Token Telegram absent de la configuration actuelle.")
        chats = chat_allowlist(preferences.get("signal_telegram_chats", ""))
        self._update(state="POLLING", running=True)
        items = import_telegram(
            self.token, chats, self.inbox, self.scope, session=self.session,
            poll_timeout=self.poll_timeout,
            request_timeout=(3, self.poll_timeout + 5),
        )
        now = self.clock()
        update = {
            "state": "CONNECTED",
            "running": True,
            "last_poll_at": now,
            "last_batch_count": len(items),
            "failures": 0,
            "last_error": "",
        }
        if items:
            update["last_received_at"] = now
            update["received_total"] = self.snapshot()["received_total"] + len(items)
        self._update(**update)
        return items

    def _run(self):
        self._update(running=True)
        failures = 0
        while not self._stop.is_set():
            if self.pause_requested():
                self._update(state="PAUSED", running=True)
                self._stop.wait(1)
                continue
            try:
                items = self.poll_once()
                failures = 0
                if items:
                    logger.info("%s message(s) Telegram autorisé(s) importé(s)", len(items))
                if self.snapshot()["state"] in {"DISABLED", "MANUAL"}:
                    self._stop.wait(2)
            except ValueError as exc:
                failures += 1
                delay = BACKOFF_SECONDS[min(failures - 1, len(BACKOFF_SECONDS) - 1)]
                self._update(
                    state="ERROR", running=True, failures=failures,
                    last_error=str(exc),
                )
                logger.warning("Réception Telegram en échec : %s ; reprise dans %ss", exc, delay)
                self._stop.wait(delay)
            except Exception:  # noqa: BLE001 - le thread ne doit jamais tuer le worker
                failures += 1
                delay = BACKOFF_SECONDS[min(failures - 1, len(BACKOFF_SECONDS) - 1)]
                self._update(
                    state="ERROR", running=True, failures=failures,
                    last_error="Erreur Telegram inattendue (détails sensibles masqués).",
                )
                logger.exception("Réception Telegram inattendue ; reprise dans %ss", delay)
                self._stop.wait(delay)
        self._update(state="STOPPED", running=False)
