"""Notification Engine — abstraction multi-canaux (sections 64 et 65).

Canaux implementes : Telegram, Email.
Canaux reserves : SMS, WhatsApp (interfaces presentes, envoi non implemente).

Le moteur ne leve jamais d'exception vers l'appelant : une notification qui
echoue ne doit jamais empecher le bot de gerer une position.
"""

from __future__ import annotations

import json
import logging
import queue
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable, Optional

from .config import Settings, get_settings
from .models import EventType, Position, TakeProfit

logger = logging.getLogger("bsm.notifications")


# ==========================================================================
# Evenements notifiables
# ==========================================================================

NOTIFIABLE_EVENTS: dict[str, str] = {
    # Désactivé par défaut (Settings → Signaux) ; le texte ne contient aucun prix.
    "SIGNAL_REVIEW": "Signal a confirmer",
    "ENTRY_CREATED": "Entry creee",
    "ENTRY_FILLED": "Entry remplie",
    "ENTRY_PARTIAL": "Entry partielle",
    "TP_TRIGGERED": "TP atteint",
    "TP_EXECUTED": "TP execute",
    "SL_MOVED": "SL deplace",
    "SL_EXECUTED": "SL execute",
    "POSITION_FINISHED": "Position terminee",
    "BINANCE_ERROR": "Erreur Binance",
    "WORKER_OFFLINE": "Worker offline",
    "INSUFFICIENT_CAPITAL": "Capital insuffisant",
    "DESYNC_DETECTED": "Desynchronisation",
}

#: Evenements qui se repetent a chaque cycle tant que le probleme persiste.
#: Les evenements de trading (TP, SL, fin de position) ne sont jamais limites.
THROTTLED_EVENTS: frozenset[str] = frozenset({"BINANCE_ERROR", "DESYNC_DETECTED"})

#: Un message identique n'est renvoye qu'apres ce delai.
REPEAT_COOLDOWN_SECONDS = 900.0

#: Ecart minimal entre deux messages differents d'un meme evenement/position.
MIN_INTERVAL_SECONDS = 60.0


@dataclass
class Notification:
    event: str
    title: str
    body: str
    level: str = "INFO"
    position_id: str = ""
    symbol: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        header = f"{self.title}"
        lines = [header, "", self.body]
        return "\n".join(lines)


# ==========================================================================
# Canaux
# ==========================================================================


class NotificationChannel(ABC):
    name = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    def send(self, notification: Notification) -> bool:
        ...

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} configured={self.is_configured}>"


class TelegramChannel(NotificationChannel):
    """Envoi via l'API Bot Telegram (aucune dependance externe)."""

    name = "telegram"

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def send(self, notification: Notification) -> bool:
        if not self.is_configured:
            return False

        url = (
            f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        )
        payload = urllib.parse.urlencode(
            {
                "chat_id": self.settings.telegram_chat_id,
                "text": notification.as_text(),
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")

        try:
            with urllib.request.urlopen(url, data=payload, timeout=10) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            logger.error("Notification Telegram en echec : %s", exc)
            return False


class EmailChannel(NotificationChannel):
    """Envoi SMTP classique."""

    name = "email"

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.smtp_host
            and self.settings.smtp_from
            and self.settings.smtp_to
        )

    def send(self, notification: Notification) -> bool:
        if not self.is_configured:
            return False

        message = EmailMessage()
        message["Subject"] = f"[BinanceSpotManager] {notification.title}"
        message["From"] = self.settings.smtp_from
        message["To"] = self.settings.smtp_to
        message.set_content(notification.as_text())

        try:
            with smtplib.SMTP(
                self.settings.smtp_host, self.settings.smtp_port, timeout=10
            ) as smtp:
                # Certificat et nom d'hote verifies : sans contexte, starttls() n'en verifie aucun.
                smtp.starttls(context=ssl.create_default_context())
                if self.settings.smtp_user:
                    smtp.login(self.settings.smtp_user, self.settings.smtp_password)
                smtp.send_message(message)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("Notification Email en echec : %s", exc)
            return False


class SmsChannel(NotificationChannel):
    """Reserve — envoi non implemente en V2, volontairement."""

    name = "sms"

    @property
    def is_configured(self) -> bool:
        return False

    def send(self, notification: Notification) -> bool:
        logger.info("Canal SMS non implemente (V2) — notification ignoree")
        return False


class WhatsAppChannel(NotificationChannel):
    """Reserve — envoi non implemente en V2, volontairement."""

    name = "whatsapp"

    @property
    def is_configured(self) -> bool:
        return False

    def send(self, notification: Notification) -> bool:
        logger.info("Canal WhatsApp non implemente (V2) — notification ignoree")
        return False


# ==========================================================================
# Moteur
# ==========================================================================


@dataclass
class _ThrottleState:
    body: str
    sent_at: float
    suppressed: int = 0


class NotificationEngine:
    """Dispatche une notification vers les canaux configures.

    `background=True` (worker) : l'envoi reseau passe par un thread dedie pour
    ne jamais retarder la surveillance TP/SL. Les erreurs et desynchronisations
    repetees sont limitees ; un message supprime est compte, jamais perdu en
    silence : le compteur accompagne le message suivant.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        channels: Optional[list[NotificationChannel]] = None,
        *,
        background: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or get_settings()
        self.channels = channels or [
            TelegramChannel(self.settings),
            EmailChannel(self.settings),
            SmsChannel(self.settings),
            WhatsAppChannel(self.settings),
        ]
        self._clock = clock
        self._throttle: dict[tuple[str, str], _ThrottleState] = {}
        self._queue: Optional[queue.Queue] = None
        if background:
            self._queue = queue.Queue(maxsize=100)
            threading.Thread(
                target=self._drain, name="bsm-notifications", daemon=True
            ).start()

    @property
    def active_channels(self) -> list[str]:
        return [c.name for c in self.channels if c.is_configured]

    def notify(self, notification: Notification) -> dict[str, bool]:
        """Envoie a tous les canaux configures. Retourne {canal: succes}.

        En mode background, retourne {} : l'envoi est differe. Une notification
        limitee retourne egalement {}.
        """
        if not self.active_channels:
            return {}
        notification = self._apply_throttle(notification)
        if notification is None:
            return {}
        if self._queue is None:
            return self._send_now(notification)
        try:
            self._queue.put_nowait(notification)
        except queue.Full:
            logger.error("File de notifications pleine : %s ignoree", notification.event)
        return {}

    def close(self, timeout: float = 5.0) -> None:
        """Laisse partir les notifications en attente, sans bloquer au-dela du delai."""
        if self._queue is None:
            return
        deadline = self._clock() + timeout
        while self._queue.unfinished_tasks and self._clock() < deadline:
            time.sleep(0.05)

    def _drain(self) -> None:
        assert self._queue is not None
        while True:
            notification = self._queue.get()
            try:
                self._send_now(notification)
            finally:
                self._queue.task_done()

    def _apply_throttle(self, notification: Notification) -> Optional[Notification]:
        if notification.event not in THROTTLED_EVENTS:
            return notification
        key = (notification.event, notification.position_id)
        now = self._clock()
        state = self._throttle.get(key)
        if state is not None:
            elapsed = now - state.sent_at
            if elapsed < MIN_INTERVAL_SECONDS or (
                notification.body == state.body and elapsed < REPEAT_COOLDOWN_SECONDS
            ):
                state.suppressed += 1
                return None
        suppressed = state.suppressed if state is not None else 0
        self._throttle[key] = _ThrottleState(body=notification.body, sent_at=now)
        if suppressed:
            notification = Notification(
                event=notification.event,
                title=notification.title,
                body=f"{notification.body}\n\n({suppressed} message(s) similaire(s) non envoye(s))",
                level=notification.level,
                position_id=notification.position_id,
                symbol=notification.symbol,
                data=notification.data,
            )
        return notification

    def _send_now(self, notification: Notification) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for channel in self.channels:
            if not channel.is_configured:
                continue
            try:
                results[channel.name] = channel.send(notification)
            except Exception as exc:  # noqa: BLE001 — jamais bloquant
                logger.error("Canal %s en exception : %s", channel.name, exc)
                results[channel.name] = False
        return results

    # -- fabriques de messages ------------------------------------------

    def entry_filled(self, position: Position, entry) -> Notification:
        return Notification(
            event="ENTRY_FILLED",
            title=f"Entry {entry.sequence_number} remplie — {position.symbol}",
            body=(
                f"Prix moyen : {entry.average_fill_price}\n"
                f"Quantite : {entry.executed_qty}\n"
                f"Montant : {entry.quote_spent:.2f} {position.quote_asset}\n"
                f"Prix moyen position : {position.metrics.average_price:.2f}\n"
                f"Quantite nette : {position.metrics.net_qty}\n"
                f"Prochain TP : {position.next_tp.target_price if position.next_tp else '-'}\n"
                f"SL : {position.stop_loss.resolved_price}"
            ),
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def tp_executed(self, position: Position, tp: TakeProfit) -> Notification:
        """Exemple de notification conforme a la section 66."""
        remaining_percent = 100.0
        target_qty = position.metrics.total_bought_qty or 0.0
        if target_qty > 0:
            remaining_percent = position.metrics.net_qty / target_qty * 100.0

        sl = position.stop_loss
        sl_label = ""
        if sl.status.name == "ACTIVE" and sl.resolved_price:
            sl_label = f"\nNouveau SL : {sl.resolved_price}"
            if position.metrics.break_even_with_fees and abs(
                sl.resolved_price - position.metrics.break_even_with_fees
            ) / max(position.metrics.break_even_with_fees, 1e-9) < 0.001:
                sl_label += "\nBreak-even + frais (estimation)"

        # Les frais payes dans un troisieme actif (BNB) ne sont pas deduits de ce gain : le dire.
        third_asset_fees = any(
            fee.amount > 0 and fee.asset.upper() not in {position.base_asset.upper(), position.quote_asset.upper()}
            for fee in tp.commissions
        )
        return Notification(
            event="TP_EXECUTED",
            title=f"TP{tp.sequence_number} atteint — {position.symbol}",
            body=(
                f"Prix : {tp.average_fill_price or tp.target_price}\n"
                f"Vendu : {tp.sell_percent:.0f} %\n"
                f"Gain realise : {tp.gain_realized:+.2f} {position.quote_asset}"
                f"{' (hors frais BNB)' if third_asset_fees else ''}"
                f"{sl_label}\n"
                f"Position restante : {remaining_percent:.0f} %"
            ),
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def sl_moved(self, position: Position, old_price: float, new_price: float) -> Notification:
        return Notification(
            event="SL_MOVED",
            title=f"SL deplace — {position.symbol}",
            body=(
                f"Ancien SL : {old_price}\n"
                f"Nouveau SL : {new_price}\n"
                f"Quantite protegee : {position.stop_loss.quantity}"
            ),
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def sl_executed(self, position: Position) -> Notification:
        sl = position.stop_loss
        return Notification(
            event="SL_EXECUTED",
            title=f"SL exécuté — {position.symbol}",
            body=(
                f"Prix moyen : {sl.average_fill_price}\n"
                f"Quantité vendue : {sl.executed_qty}\n"
                f"Position : {position.status.value}"
            ),
            level="WARNING",
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def candle_stop_exit(
        self, position: Position, stop_price: float, interval: str, close_price: float, outcome: str
    ) -> Notification:
        return Notification(
            event="SL_EXECUTED",
            title=f"SL à la clôture {interval} — sortie au marché — {position.symbol}",
            body=(
                f"Bougie {interval} clôturée au SL ou dessous\n"
                f"Stop : {stop_price}\n"
                f"Clôture : {close_price}\n"
                f"Résultat : {outcome}"
            ),
            level="WARNING",
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def stop_crossed_exit(
        self, position: Position, stop_price: float, market_price: float, outcome: str
    ) -> Notification:
        return Notification(
            event="SL_EXECUTED",
            title=f"Stop franchi — sortie au marché — {position.symbol}",
            body=(
                f"SL refusé par Binance : prix déjà sous le stop\n"
                f"Stop : {stop_price}\n"
                f"Prix au contrôle : {market_price}\n"
                f"Résultat : {outcome}"
            ),
            level="WARNING",
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def stop_crossed_paused(
        self, position: Position, stop_price: float, reason: str
    ) -> Notification:
        return Notification(
            event="BINANCE_ERROR",
            title=f"Stop franchi — position en pause — {position.symbol}",
            body=(
                f"Stop : {stop_price}\n"
                f"{reason}\n"
                f"Position sans protection : vérifier sur Binance Demo."
            ),
            level="CRITICAL",
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def position_finished(
        self, position: Position, *, fee_rates: Optional[dict[str, float]] = None
    ) -> Notification:
        """Fin de position, gagnante OU perdante, avec le meme PnL que la page History : les frais
        payes en BNB sont valorises au cours fourni (`fee_rates`) sur une copie de la position."""
        from .fee_valuation import external_fee_assets
        from .position_engine import recompute_position

        shown = position
        if fee_rates:
            shown = position.model_copy(deep=True)
            recompute_position(shown, fee_rates=fee_rates)
        third = sorted(external_fee_assets(position))
        missing = [asset for asset in third if not fee_rates or asset not in fee_rates]
        fees_note = ""
        if third and not missing:
            fees_note = f" (dont {', '.join(third)} au cours actuel)"
        elif missing:
            fees_note = f" (hors frais {', '.join(missing)} : cours indisponible)"
        result = shown.pnl.realized
        label = "Gain" if result > 0 else "Perte" if result < 0 else "Resultat nul"
        return Notification(
            event="POSITION_FINISHED",
            title=f"Position terminee — {position.symbol} — {label} {result:+.2f} {position.quote_asset}",
            body=(
                f"Raison : {position.close_reason.value if position.close_reason else '-'}\n"
                f"PnL realise, frais compris : {result:+.2f} {position.quote_asset}\n"
                f"Frais payes : {shown.pnl.fees_paid:.2f} {position.quote_asset}{fees_note}\n"
                f"TP atteints : {len(position.hit_tps)}/{len(position.take_profits)}"
            ),
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def desync(self, position: Position, messages: list[str]) -> Notification:
        return Notification(
            event="DESYNC_DETECTED",
            title=f"Desynchronisation — {position.symbol}",
            body="\n".join(f"- {m}" for m in messages),
            level="WARNING",
            position_id=position.position_id,
            symbol=position.symbol,
        )

    def error(self, message: str, *, context: str = "") -> Notification:
        return Notification(
            event="BINANCE_ERROR",
            title="Erreur BinanceSpotManager",
            body=f"{context}\n{message}" if context else message,
            level="ERROR",
        )

    def worker_offline(self, last_heartbeat: Optional[str]) -> Notification:
        return Notification(
            event="WORKER_OFFLINE",
            title="Worker offline",
            body=f"Dernier heartbeat : {last_heartbeat or 'jamais'}",
            level="CRITICAL",
        )

    # -- envoi filtre par preferences de position -----------------------

    def notify_position_event(
        self, position: Position, notification: Notification
    ) -> dict[str, bool]:
        """Respecte les preferences par position avant d'envoyer."""
        prefs = position.notifications
        allowed = {
            "ENTRY_FILLED": prefs.on_entry_filled,
            "ENTRY_PARTIAL": prefs.on_entry_filled,
            "TP_EXECUTED": prefs.on_tp_executed,
            "SL_MOVED": prefs.on_sl_moved,
            "SL_EXECUTED": prefs.on_sl_executed,
            "POSITION_FINISHED": prefs.on_position_finished,
            "BINANCE_ERROR": prefs.on_error,
        }.get(notification.event, True)

        if not allowed:
            return {}
        return self.notify(notification)


def notification_preview(notification: Notification) -> str:
    """Rendu texte d'une notification, utilise par l'UI de test."""
    return notification.as_text()


def channel_summary(engine: NotificationEngine) -> str:
    active = engine.active_channels
    if not active:
        return "Aucun canal configure (.env)"
    return "Canaux actifs : " + ", ".join(active)


def serialize_for_log(notification: Notification) -> str:
    return json.dumps(
        {
            "event": notification.event,
            "title": notification.title,
            "position_id": notification.position_id,
            "symbol": notification.symbol,
        },
        ensure_ascii=False,
    )
