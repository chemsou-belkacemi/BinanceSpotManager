"""Les tests hors integration ne doivent jamais appeler le reseau : ni Binance, ni Telegram, ni le courriel.

Le .env local est charge par la configuration : sans ce blocage, un test qui declenche une notification (connexion
a l'interface, alerte du worker...) enverrait un VRAI message au proprietaire (constate le 2026-10-05 : trois
« Connexion a l'interface : client » venus des tests de connexion).
"""

import smtplib
import urllib.request

import pytest
import requests


@pytest.fixture(autouse=True)
def block_network_outside_integration(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return

    def forbidden(*args, **kwargs):
        raise RuntimeError("Reseau interdit dans un test hors integration")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    # Telegram (notifications, signal de vie externe) et courriel : urllib et smtplib, hors requests.
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
