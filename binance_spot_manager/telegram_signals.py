"""Opt-in inbox import. No trading, webhook changes, or outgoing Telegram messages."""
import hashlib
import re

import requests


def chat_allowlist(text):
    values = [part for part in re.split(r"[,;\s]+", text.strip()) if part]
    if not values or any(not re.fullmatch(r"-?\d+", value) for value in values):
        raise ValueError("Indiquer au moins un identifiant numérique de conversation Telegram.")
    return {int(value) for value in values}


def import_telegram(token, allowed_chats, inbox, scope, *, session=None):
    if not token or not allowed_chats:
        raise ValueError("Token et conversations autorisées requis ; réception désactivée.")
    bot = hashlib.sha256(token.encode()).hexdigest()
    client = session or requests.Session()
    try:
        response = client.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": inbox.offset(bot), "timeout": 0, "limit": 100,
                    "allowed_updates": '["message","channel_post","edited_message","edited_channel_post"]'},
            timeout=(3, 5),
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
            received.append(inbox.receive(scope, raw, source="telegram",
                external_id=f"{bot}:{chat}:{message['message_id']}",
                edited="edited_message" in update or "edited_channel_post" in update))
        # Persist only AFTER saving the message. A retry remains idempotent.
        inbox.advance(bot, int(update["update_id"]) + 1)
    return received
