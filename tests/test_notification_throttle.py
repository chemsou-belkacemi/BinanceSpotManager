"""Limitation des notifications repetees et envoi hors boucle du worker."""

from __future__ import annotations

import threading

import pytest

from binance_spot_manager.config import Settings
from binance_spot_manager.notification_engine import (
    MIN_INTERVAL_SECONDS,
    REPEAT_COOLDOWN_SECONDS,
    Notification,
    NotificationChannel,
    NotificationEngine,
)

pytestmark = pytest.mark.unit


class RecordingChannel(NotificationChannel):
    name = "recording"

    def __init__(self, settings, gate: threading.Event | None = None) -> None:
        super().__init__(settings)
        self.sent: list[Notification] = []
        self.gate = gate

    @property
    def is_configured(self) -> bool:
        return True

    def send(self, notification: Notification) -> bool:
        if self.gate is not None:
            self.gate.wait(5)
        self.sent.append(notification)
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup():
    channel = RecordingChannel(Settings())
    clock = Clock()
    return NotificationEngine(Settings(), [channel], clock=clock), channel, clock


def test_identical_error_is_sent_once_per_cooldown(setup):
    engine, channel, clock = setup
    for _ in range(30):
        engine.notify(engine.error("SL non recree", context="worker"))
        clock.now += 1

    assert len(channel.sent) == 1

    clock.now += REPEAT_COOLDOWN_SECONDS
    engine.notify(engine.error("SL non recree", context="worker"))

    assert len(channel.sent) == 2
    assert "29 message(s) similaire(s) non envoye(s)" in channel.sent[1].body


def test_new_error_waits_for_min_interval(setup):
    engine, channel, clock = setup
    engine.notify(engine.error("premiere erreur"))
    clock.now += MIN_INTERVAL_SECONDS / 2
    engine.notify(engine.error("autre erreur"))
    assert len(channel.sent) == 1

    clock.now += MIN_INTERVAL_SECONDS
    engine.notify(engine.error("autre erreur"))
    assert [n.body.splitlines()[0] for n in channel.sent] == ["premiere erreur", "autre erreur"]


def test_desync_is_limited_per_position(setup):
    engine, channel, _ = setup

    def desync(position_id: str) -> Notification:
        return Notification(event="DESYNC_DETECTED", title="Desync", body="ecart",
                            position_id=position_id)

    for _ in range(5):
        engine.notify(desync("A"))
        engine.notify(desync("B"))

    assert sorted(n.position_id for n in channel.sent) == ["A", "B"]


def test_trading_events_are_never_throttled(setup):
    engine, channel, _ = setup
    for _ in range(3):
        engine.notify(Notification(event="TP_EXECUTED", title="TP1", body="identique"))

    assert len(channel.sent) == 3


def test_background_send_does_not_block_caller():
    gate = threading.Event()
    channel = RecordingChannel(Settings(), gate=gate)
    engine = NotificationEngine(Settings(), [channel], background=True)

    assert engine.notify(Notification(event="TP_EXECUTED", title="TP1", body="x")) == {}
    assert channel.sent == []  # le canal est bloque, l'appelant ne l'est pas

    gate.set()
    engine.close(timeout=5)
    assert len(channel.sent) == 1
