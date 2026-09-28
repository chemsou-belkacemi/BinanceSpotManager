"""Les tests hors integration ne doivent jamais appeler le reseau Binance."""

import pytest
import requests


@pytest.fixture(autouse=True)
def block_network_outside_integration(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return

    def forbidden(*args, **kwargs):
        raise RuntimeError("Reseau interdit dans un test hors integration")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
