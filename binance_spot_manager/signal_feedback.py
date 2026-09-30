"""Retour d'exécution vers CryptoSignalIntelligence (FEEDBACK_FORMAT.md, version 1).

Un événement JSON par ligne, ajouté en fin de fichier
``DATA_DIR/signal_drop/outgoing/execution_events.jsonl`` (UTF-8, flush + fsync),
uniquement pour les signaux du contrat TXT V2. Chaque événement porte un
identifiant déterministe ``BSM-<SIGNAL_ID>-<TYPE>-<n>`` enregistré dans
``DATA_DIR/signal_feedback.sqlite3`` APRÈS l'écriture de la ligne : un arrêt
entre les deux réécrit la même ligne avec le même identifiant, que le
producteur ignore à l'importation ; un événement n'est jamais perdu ni écrit
sous un second identifiant.

Sources des événements :

* RECEIVED : une commande ``signal:<ligne>`` existe (signal accepté, non expiré,
  écart d'entrée contrôlé) ; REJECTED : refus à la réception (dépôt), refus de
  l'exécution automatique (``auto_detail``), commande échouée/expirée/annulée,
  ou signal jamais traité avant EXPIRES_AT ;
* ENTRY_PARTIAL / ENTRY_FILLED / TP_FILLED / STOP_FILLED : quantités REELLEMENT
  remplies, rapportées par INCREMENT (le producteur additionne les quantités) ;
* EXPIRED / CANCELLED : entrées terminées sans aucun achat ; CLOSED : position
  terminée après au moins un achat.

Aucune importation du projet producteur : ses règles de validation sont
reproduites dans :func:`validate_event` et toute ligne non conforme est refusée
avant écriture. Le retour est désactivé en DRY_RUN (aucun ordre réel).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from .config import DATA_DIR
from .models import EntryStatus
from .position_engine import QTY_EPSILON

logger = logging.getLogger("bsm.signal_feedback")

OUTGOING_DIR = DATA_DIR / "signal_drop" / "outgoing"
FEEDBACK_FILE_NAME = "execution_events.jsonl"
REGISTRY_PATH = DATA_DIR / "signal_feedback.sqlite3"
PRODUCER = "BinanceSpotManager"
ENVIRONMENT = "DEMO"
EVENT_TYPES = ("RECEIVED", "REJECTED", "ENTRY_PARTIAL", "ENTRY_FILLED", "TP_FILLED", "STOP_FILLED",
               "CLOSED", "EXPIRED", "CANCELLED")
FILL_EVENTS = frozenset({"ENTRY_PARTIAL", "ENTRY_FILLED", "TP_FILLED", "STOP_FILLED"})
EVENT_FIELDS = ("event_id", "signal_id", "event_type", "occurred_at", "environment", "producer", "symbol",
                "quantity", "price", "quote_quantity", "fee", "fee_asset", "order_id", "target_index", "reason")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")
PRODUCER_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,60}$")
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}(USDT|USDC)$")
ASSET_PATTERN = re.compile(r"^[A-Z0-9]{2,20}$")
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,80}$")
TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
MAX_REASON = 500
MAX_TARGET_INDEX = 4
#: Délai après EXPIRES_AT avant de déclarer refusé un signal jamais traité.
EXPIRY_GRACE_SECONDS = 60


class FeedbackContractError(ValueError):
    """Événement non conforme au contrat : jamais écrit."""


def iso_utc(value) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` depuis un datetime ou un temps Unix (tronqué à la seconde)."""
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        stamp = datetime.fromtimestamp(float(value), tz=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def decimal_text(value) -> str:
    """Nombre en chaîne décimale à point, sans exposant ni NaN/Infinity."""
    try:
        number = Decimal(repr(float(value)))
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise FeedbackContractError(f"nombre invalide : {value!r}") from None
    if not number.is_finite():
        raise FeedbackContractError(f"nombre non fini : {value!r}")
    if abs(number) < Decimal("1e15"):
        # Précision maximale de Binance (8 décimales) : gomme le bruit des flottants.
        number = number.quantize(Decimal("1e-8"))
    text = format(number.normalize(), "f")
    return "0" if text in {"-0", "-0.0"} else text


def _decimal_field(event, key, *, required_positive=False):
    value = event.get(key)
    if value is None:
        if required_positive:
            raise FeedbackContractError(f"{event.get('event_type')} exige {key} > 0 (remplissage réel)")
        return None
    if not isinstance(value, str):
        raise FeedbackContractError(f"{key} : nombre en chaîne décimale requis")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise FeedbackContractError(f"{key} : nombre décimal invalide") from None
    if not number.is_finite() or number < 0:
        raise FeedbackContractError(f"{key} : valeur finie et positive requise")
    if required_positive and number <= 0:
        raise FeedbackContractError(f"{event.get('event_type')} exige {key} > 0 (remplissage réel)")
    return number


def validate_event(event: dict) -> dict:
    """Reproduit les règles du modèle producteur ; lève FeedbackContractError sinon."""
    if not isinstance(event, dict) or set(event) != set(EVENT_FIELDS):
        raise FeedbackContractError("champs de l'événement inattendus ou manquants")
    kind = event["event_type"]
    if kind not in EVENT_TYPES:
        raise FeedbackContractError(f"event_type inconnu : {kind!r}")
    for key, pattern in (("event_id", ID_PATTERN), ("signal_id", ID_PATTERN),
                         ("producer", PRODUCER_PATTERN), ("symbol", SYMBOL_PATTERN)):
        if not isinstance(event[key], str) or not pattern.fullmatch(event[key]):
            raise FeedbackContractError(f"{key} invalide : {event[key]!r}")
    if event["environment"] != ENVIRONMENT:
        raise FeedbackContractError("environment : DEMO uniquement")
    occurred = event["occurred_at"]
    if not isinstance(occurred, str) or not TIME_PATTERN.fullmatch(occurred):
        raise FeedbackContractError("occurred_at : format YYYY-MM-DDTHH:MM:SSZ requis")
    try:
        datetime.strptime(occurred, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise FeedbackContractError("occurred_at : date invalide") from None
    fill = kind in FILL_EVENTS
    _decimal_field(event, "quantity", required_positive=fill)
    _decimal_field(event, "price", required_positive=fill)
    _decimal_field(event, "quote_quantity")
    fee = _decimal_field(event, "fee")
    asset = event["fee_asset"]
    if asset is not None and (not isinstance(asset, str) or not ASSET_PATTERN.fullmatch(asset)):
        raise FeedbackContractError(f"fee_asset invalide : {asset!r}")
    if (fee is None) != (asset is None):
        raise FeedbackContractError("fee et fee_asset vont ensemble")
    order_id = event["order_id"]
    if order_id is not None and (not isinstance(order_id, str) or not ORDER_ID_PATTERN.fullmatch(order_id)):
        raise FeedbackContractError(f"order_id invalide : {order_id!r}")
    index = event["target_index"]
    if index is not None and (isinstance(index, bool) or not isinstance(index, int)
                              or not 1 <= index <= MAX_TARGET_INDEX):
        raise FeedbackContractError("target_index : entier de 1 à 4")
    if kind == "TP_FILLED" and index is None:
        raise FeedbackContractError("TP_FILLED exige target_index (1 à 4)")
    reason = event["reason"]
    if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON):
        raise FeedbackContractError("reason : texte de 500 caractères maximum")
    if kind in {"REJECTED", "CANCELLED"} and not reason:
        raise FeedbackContractError(f"{kind} exige reason")
    return event


def build_event_id(signal_id: str, event_type: str, sequence: int) -> str:
    """``BSM-<SIGNAL_ID>-<TYPE>-<n>`` ; un identifiant trop long remplace le SIGNAL_ID par son empreinte."""
    event_id = f"BSM-{signal_id}-{event_type}-{sequence}"
    if len(event_id) > 160:
        digest = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:24]
        event_id = f"BSM-{digest}-{event_type}-{sequence}"
    return event_id


def _single_commission(commissions):
    """(asset, montant) si toutes les commissions portent sur un seul actif, sinon None."""
    assets = {c.asset for c in commissions if c.amount > 0}
    if len(assets) != 1:
        return None
    asset = assets.pop()
    return asset, sum(c.amount for c in commissions if c.asset == asset)


class FeedbackRegistry:
    """SQLite : signaux suivis, identifiants d'événements écrits, cumuls déjà rapportés."""

    def __init__(self, path=REGISTRY_PATH):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY, row_id TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '', position_id TEXT NOT NULL DEFAULT '',
                final INTEGER NOT NULL DEFAULT 0, registered REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, event_type TEXT NOT NULL,
                dedupe_key TEXT NOT NULL, sequence INTEGER NOT NULL, written REAL NOT NULL,
                UNIQUE(signal_id, event_type, dedupe_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS reported (
                signal_id TEXT NOT NULL, item_key TEXT NOT NULL,
                quantity REAL NOT NULL, quote REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0, fee_asset TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(signal_id, item_key))""")
            db.commit()
            yield db
        finally:
            db.close()

    def register(self, signal_id, row_id="", symbol="", *, now):
        with self.connect() as db, db:
            db.execute("INSERT OR IGNORE INTO signals (signal_id, row_id, symbol, registered) VALUES (?, ?, ?, ?)",
                       (signal_id, row_id, symbol, float(now)))
            if row_id:
                db.execute("UPDATE signals SET row_id=? WHERE signal_id=? AND row_id=''", (row_id, signal_id))
            if symbol:
                db.execute("UPDATE signals SET symbol=? WHERE signal_id=? AND symbol=''", (symbol, signal_id))

    def known_ids(self):
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT signal_id FROM signals")}

    def pending(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM signals WHERE final=0 ORDER BY registered, signal_id")]

    def set_position(self, signal_id, position_id):
        with self.connect() as db, db:
            db.execute("UPDATE signals SET position_id=? WHERE signal_id=? AND position_id=''",
                       (position_id, signal_id))

    def mark_final(self, signal_id):
        with self.connect() as db, db:
            db.execute("UPDATE signals SET final=1 WHERE signal_id=?", (signal_id,))

    def event_exists(self, signal_id, event_type, dedupe_key):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM events WHERE signal_id=? AND event_type=? AND dedupe_key=?",
                              (signal_id, event_type, dedupe_key)).fetchone() is not None

    def next_sequence(self, signal_id, event_type):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM events WHERE signal_id=? AND event_type=?",
                              (signal_id, event_type)).fetchone()[0] + 1

    def record_event(self, event_id, signal_id, event_type, dedupe_key, sequence, *, now):
        with self.connect() as db, db:
            db.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?)",
                       (event_id, signal_id, event_type, dedupe_key, int(sequence), float(now)))

    def events_total(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def reported(self, signal_id, item_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM reported WHERE signal_id=? AND item_key=?",
                             (signal_id, item_key)).fetchone()
            return dict(row) if row else {"quantity": 0.0, "quote": 0.0, "fee": 0.0, "fee_asset": ""}

    def set_reported(self, signal_id, item_key, *, quantity, quote, fee, fee_asset):
        with self.connect() as db, db:
            db.execute("""INSERT INTO reported VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id, item_key) DO UPDATE SET
                quantity=excluded.quantity, quote=excluded.quote, fee=excluded.fee, fee_asset=excluded.fee_asset""",
                       (signal_id, item_key, float(quantity), float(quote), float(fee), fee_asset or ""))


class SignalFeedbackWriter:
    """Écrit le fichier de retour ; appelé par le dépôt (réception) et par le worker (``sync``)."""

    def __init__(self, scope, inbox, commands, *, positions=None, directory=OUTGOING_DIR,
                 registry_path=REGISTRY_PATH, clock=time.time, enabled=True):
        self.scope = scope
        self.inbox = inbox
        self.commands = commands
        self.positions = positions
        self.directory = Path(directory)
        self.path = self.directory / FEEDBACK_FILE_NAME
        self.registry = FeedbackRegistry(registry_path)
        self.clock = clock
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._diagnostics = {
            "state": "ACTIVE" if self.enabled else "DISABLED",
            "events_total": 0,
            "pending_signals": 0,
            "last_event_at": None,
            "last_error": "",
            "path": str(self.path),
        }

    def snapshot(self):
        with self._lock:
            return dict(self._diagnostics)

    def _update(self, **values):
        with self._lock:
            self._diagnostics.update(values)

    # ------------------------------------------------------------------
    # Entrées : réception d'un dépôt
    # ------------------------------------------------------------------

    def register(self, signal_id, row_id, symbol) -> None:
        """Signal V2 accepté à la réception : suivi jusqu'à son état final."""
        if not self.enabled:
            return
        try:
            self.registry.register(signal_id, row_id, symbol, now=self.clock())
        except sqlite3.Error as exc:
            logger.error("Retour d'exécution : enregistrement de %s impossible (%s)", signal_id, exc)
            self._update(state="ERROR", last_error=str(exc))

    def record_rejection(self, signal_id, symbol, reason, *, occurred_at=None) -> bool:
        """REJECTED à la réception (expiré, mal formé, doublon) ; jamais d'exception."""
        if not self.enabled:
            return False
        if not (signal_id and ID_PATTERN.fullmatch(signal_id) and symbol and SYMBOL_PATTERN.fullmatch(symbol)):
            logger.warning("Retour d'exécution : refus non rapportable (identifiant ou paire invalide)")
            return False
        try:
            self.registry.register(signal_id, "", symbol, now=self.clock())
            written = self._emit(signal_id, "REJECTED", "reception", symbol=symbol,
                                 occurred_at=occurred_at if occurred_at is not None else self.clock(),
                                 reason=reason)
            self.registry.mark_final(signal_id)
            return written
        except Exception as exc:  # noqa: BLE001 - le dépôt continue sans le retour
            logger.exception("Retour d'exécution : refus de %s non écrit", signal_id)
            self._update(state="ERROR", last_error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Cycle du worker
    # ------------------------------------------------------------------

    def sync(self, positions, *, now=None) -> int:
        """Rapproche les signaux suivis des commandes et positions ; retourne le nombre de lignes écrites."""
        if not self.enabled:
            return 0
        now = self.clock() if now is None else float(now)
        try:
            return self._sync(list(positions or []), now)
        except Exception as exc:  # noqa: BLE001 - le retour ne bloque jamais les TP/SL
            logger.exception("Retour d'exécution : synchronisation interrompue")
            self._update(state="ERROR", last_error=str(exc))
            return 0

    def _sync(self, positions, now):
        known = self.registry.known_ids()
        for key in self.inbox.signal_keys(self.scope):
            if key["signal_id"] not in known:
                self.registry.register(key["signal_id"], key["row_id"], "", now=now)
        by_tag = {}
        for position in positions:
            if len(position.tags) >= 2 and position.tags[0] == "signal":
                by_tag[position.tags[1]] = position
        written = 0
        pending = self.registry.pending()
        for record in pending:
            written += self._process_signal(record, by_tag, now)
        self._update(state="ACTIVE", last_error="", pending_signals=len(self.registry.pending()),
                     events_total=self.registry.events_total())
        return written

    def _process_signal(self, record, by_tag, now) -> int:
        signal_id = record["signal_id"]
        row = self.inbox.get(self.scope, record["row_id"]) if record["row_id"] else None
        if row is None:
            return 0
        parsed = row["parsed"]
        symbol = record["symbol"] or parsed.get("symbol") or ""
        expires_at = float(parsed.get("expires_at") or 0)
        command = self.commands.get_by_request_key(self.scope, f"signal:{row['id']}")
        position = by_tag.get(row["id"])
        position_id = record["position_id"]
        if command and not position_id:
            position_id = str((command["payload"].get("position") or {}).get("position_id") or "")
            if position_id:
                self.registry.set_position(signal_id, position_id)
        if position is None and position_id and self.positions is not None:
            position = self.positions.load(position_id)
        written = 0
        final = False
        if command is None:
            if row["auto_state"] == "REJECTED":
                written += self._emit(signal_id, "REJECTED", "auto", symbol=symbol, occurred_at=now,
                                      reason=row["auto_detail"] or "Signal automatique refusé")
                final = True
            elif (row["payload"] is None and row["auto_state"] in {"", "PROCESSING"}
                  and expires_at > 0 and now >= expires_at + EXPIRY_GRACE_SECONDS):
                written += self._emit(signal_id, "REJECTED", "unprocessed", symbol=symbol, occurred_at=now,
                                      reason="Signal expiré avant tout traitement "
                                             "(exécution automatique inactive ou non autorisée)")
                final = True
        else:
            written += self._emit(signal_id, "RECEIVED", "command", symbol=symbol,
                                  occurred_at=command["created_at"])
            state = command["state"]
            if state in {"FAILED", "EXPIRED", "CANCELED"}:
                reason = {
                    "FAILED": (command.get("result") or {}).get("message") or "Commande refusée par le worker",
                    "EXPIRED": "Commande expirée avant traitement par le worker",
                    "CANCELED": "Commande annulée avant exécution",
                }[state]
                written += self._emit(signal_id, "REJECTED", f"command:{command['id']}", symbol=symbol,
                                      occurred_at=command["updated_at"], reason=reason)
                final = position is None
            elif state == "UNCERTAIN" and position is None and expires_at > 0 \
                    and now >= expires_at + EXPIRY_GRACE_SECONDS:
                # La position est sauvegardée avant tout envoi : sans position, aucun ordre n'est parti.
                written += self._emit(signal_id, "REJECTED", f"command:{command['id']}", symbol=symbol,
                                      occurred_at=command["updated_at"],
                                      reason="Commande incertaine sans position créée : aucun ordre envoyé")
                final = True
        if position is not None:
            count, terminal = self._report_position(signal_id, position, symbol or position.symbol, now)
            written += count
            final = final or terminal
        if final:
            self.registry.mark_final(signal_id)
        return written

    def _report_position(self, signal_id, position, symbol, now):
        written = 0
        for entry in position.sorted_entries:
            kind = "ENTRY_FILLED" if entry.status is EntryStatus.FILLED else "ENTRY_PARTIAL"
            occurred = entry.filled_at if (entry.status is EntryStatus.FILLED and entry.filled_at) else now
            written += self._report_fill(
                signal_id, symbol, f"entry:{entry.entry_id}", kind,
                quantity=entry.executed_qty, quote=entry.quote_spent, average=entry.average_fill_price,
                commissions=entry.commissions, order_id=entry.order_id or entry.client_order_id,
                occurred_at=occurred,
            )
        for tp in position.sorted_tps:
            if not 1 <= tp.sequence_number <= MAX_TARGET_INDEX:
                continue
            written += self._report_fill(
                signal_id, symbol, f"tp:{tp.tp_id}", "TP_FILLED",
                quantity=tp.executed_qty, quote=tp.quote_received, average=tp.average_fill_price,
                commissions=tp.commissions, order_id=tp.order_id or tp.client_order_id,
                occurred_at=tp.executed_at or now, target_index=tp.sequence_number,
            )
        stop = position.stop_loss
        written += self._report_fill(
            signal_id, symbol, "sl", "STOP_FILLED",
            quantity=stop.executed_qty, quote=stop.quote_received, average=stop.average_fill_price,
            commissions=stop.commissions, order_id=stop.order_id or stop.client_order_id,
            occurred_at=stop.executed_at or now,
        )
        for sale in position.manual_exits:
            # Vente au marché décidée par le worker (stop franchi) ou par l'utilisateur :
            # rapportée comme sortie protectrice, le motif précise la nature de la vente.
            written += self._report_fill(
                signal_id, symbol, f"exit:{sale.client_order_id}", "STOP_FILLED",
                quantity=sale.executed_qty, quote=sale.quote_received, average=sale.average_fill_price,
                commissions=sale.commissions, order_id=sale.order_id or sale.client_order_id,
                occurred_at=position.closed_at or now,
                reason=f"Vente au marché hors stop Binance ({sale.close_reason.value})",
            )
        bought = any(entry.executed_qty > QTY_EPSILON for entry in position.entries)
        closed_at = position.closed_at or now
        if not bought and position.entries and all(entry.is_terminal for entry in position.entries):
            if any(entry.status is EntryStatus.EXPIRED for entry in position.entries):
                written += self._emit(signal_id, "EXPIRED", "entries", symbol=symbol, occurred_at=closed_at)
            else:
                details = "; ".join(e.last_error for e in position.entries if e.last_error)
                reason = "Entrée annulée ou refusée avant tout remplissage" + (f" : {details}" if details else "")
                written += self._emit(signal_id, "CANCELLED", "entries", symbol=symbol, occurred_at=closed_at,
                                      reason=reason)
            return written, True
        if not position.is_open:
            if bought:
                written += self._emit(signal_id, "CLOSED", "closed", symbol=symbol, occurred_at=closed_at)
            else:
                motive = position.close_reason.value if position.close_reason else "motif inconnu"
                written += self._emit(signal_id, "CANCELLED", "entries", symbol=symbol, occurred_at=closed_at,
                                      reason=f"Position fermée sans achat ({motive})")
            return written, True
        return written, False

    def _report_fill(self, signal_id, symbol, item_key, event_type, *, quantity, quote, average,
                     commissions, order_id, occurred_at, target_index=None, reason=None) -> int:
        """Rapporte l'INCREMENT de quantité depuis le dernier cumul déjà écrit."""
        quantity = float(quantity or 0)
        if quantity <= QTY_EPSILON:
            return 0
        last = self.registry.reported(signal_id, item_key)
        delta_qty = quantity - float(last["quantity"])
        if delta_qty <= QTY_EPSILON:
            return 0
        quote = float(quote or 0)
        delta_quote = quote - float(last["quote"])
        if delta_quote <= 0:
            delta_quote = delta_qty * float(average or 0)
        if delta_quote <= 0:
            logger.warning("Retour d'exécution : remplissage sans prix pour %s (%s)", signal_id, item_key)
            return 0
        fee = fee_asset = None
        single = _single_commission(commissions or [])
        if single is not None:
            asset, total = single
            previous = float(last["fee"]) if last["fee_asset"] == asset else 0.0
            if total - previous > 0:
                fee, fee_asset = total - previous, asset
        # Clé = cumul atteint : une reprise après arrêt retrouve la même ligne, jamais un
        # second incrément, et réaligne le cumul déjà rapporté.
        dedupe_key = f"{item_key}:{decimal_text(quantity)}"
        cumulative = dict(quantity=quantity, quote=max(quote, float(last["quote"]) + delta_quote),
                          fee=single[1] if single is not None else 0.0,
                          fee_asset=single[0] if single is not None else "")
        if self.registry.event_exists(signal_id, event_type, dedupe_key):
            self.registry.set_reported(signal_id, item_key, **cumulative)
            return 0
        written = self._emit(
            signal_id, event_type, dedupe_key, symbol=symbol, occurred_at=occurred_at,
            quantity=delta_qty, price=delta_quote / delta_qty, quote_quantity=delta_quote,
            fee=fee, fee_asset=fee_asset, order_id=order_id, target_index=target_index, reason=reason,
        )
        if written:
            self.registry.set_reported(signal_id, item_key, **cumulative)
        return written

    # ------------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------------

    def _emit(self, signal_id, event_type, dedupe_key, *, symbol, occurred_at, quantity=None, price=None,
              quote_quantity=None, fee=None, fee_asset=None, order_id=None, target_index=None,
              reason=None) -> int:
        """Écrit une ligne si (signal, type, clé) est inédit ; retourne 1 si écrite, 0 sinon."""
        if self.registry.event_exists(signal_id, event_type, dedupe_key):
            return 0
        sequence = self.registry.next_sequence(signal_id, event_type)
        event = {
            "event_id": build_event_id(signal_id, event_type, sequence),
            "signal_id": signal_id,
            "event_type": event_type,
            "occurred_at": iso_utc(occurred_at),
            "environment": ENVIRONMENT,
            "producer": PRODUCER,
            "symbol": symbol,
            "quantity": None if quantity is None else decimal_text(quantity),
            "price": None if price is None else decimal_text(price),
            "quote_quantity": None if quote_quantity is None else decimal_text(quote_quantity),
            "fee": None if fee is None else decimal_text(fee),
            "fee_asset": fee_asset,
            "order_id": None if order_id in (None, "") else str(order_id),
            "target_index": target_index,
            "reason": None if reason is None else str(reason)[:MAX_REASON],
        }
        try:
            validate_event(event)
        except FeedbackContractError as exc:
            logger.error("Retour d'exécution : événement %s non conforme, ignoré (%s)", event["event_id"], exc)
            self._update(last_error=f"{event['event_id']} : {exc}")
            return 0
        self._append_line(event)
        # Enregistré APRÈS l'écriture : une reprise réécrit au pire la même ligne, même identifiant.
        self.registry.record_event(event["event_id"], signal_id, event_type, dedupe_key, sequence,
                                   now=self.clock())
        self._update(last_event_at=self.clock())
        return 1

    def _append_line(self, event: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
