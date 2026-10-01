"""Client CSI : requêtes, jeton, pannes converties en CsiUnavailable, politique de retenue."""
from types import SimpleNamespace

import pytest
import requests

from binance_spot_manager.csi_client import (
    CsiClient,
    CsiOpinion,
    CsiUnavailable,
    GatePolicy,
    source_label,
    source_names,
)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return SimpleNamespace(status_code=status, json=lambda: payload)


def opinion(verdict="DEFAVORABLE", summary="résumé"):
    return CsiOpinion(verdict=verdict, summary=summary, source="g", evaluated_at="2026-09-30T10:00:00+00:00")


def test_evaluate_sends_token_and_builds_an_opinion():
    payload = {
        "verdict": "DEFAVORABLE", "summary_fr": "Défavorable : un veto est déclenché.",
        "evaluated_at": "2026-09-30T10:00:00+00:00", "record_id": "EXT-1",
        "checks": [{"label": "lecture", "ok": True, "detail": ""}, {"label": "distance du stop", "ok": False, "detail": "0,2 ATR"}],
    }
    session = FakeSession([(200, payload), (200, payload | {"verdict": "EN_ATTENTE", "record_id": None})])
    client = CsiClient("http://csi-api:8503/", "secret", session=session)
    result = client.evaluate("PAIR: ETH/USDT\nENTRY 1: 100\nT1: 110\nSL: 95", source="Groupe A", record=False)
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "http://csi-api:8503/evaluate")
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["timeout"] == 30.0                     # évaluation : délai long (vérification Binance côté CSI)
    assert kwargs["json"] == {"text": "PAIR: ETH/USDT\nENTRY 1: 100\nT1: 110\nSL: 95", "source": "Groupe A",
                              "record": False, "user_validated": False}
    assert result.verdict == "DEFAVORABLE" and result.label == "Défavorable" and result.holds_execution
    assert result.record_id == "EXT-1" and result.failed_checks == ("distance du stop : 0,2 ATR",)
    pending = client.evaluate("PAIR: ETH/USDT\nENTRY 1: 100\nT1: 110\nSL: 95", source="Groupe A", user_validated=True)
    assert session.calls[1][2]["json"]["user_validated"] is True
    assert pending.verdict == "EN_ATTENTE" and pending.label == "En attente" and pending.holds_execution
    with pytest.raises(ValueError):
        client.evaluate("   ", source="Groupe A")


def test_failures_become_csi_unavailable_without_leaking_the_token():
    session = FakeSession([
        requests.ConnectionError("http://csi-api:8503/health?token=secret"),
        (401, {"error": "jeton absent ou invalide"}),
        (200, "pas un objet"),
        (200, {"verdict": "PEUT-ÊTRE"}),
    ])
    client = CsiClient("http://csi-api:8503", "secret", session=session)
    with pytest.raises(CsiUnavailable) as down:
        client.health()
    assert "ConnectionError" in str(down.value) and "secret" not in str(down.value)
    with pytest.raises(CsiUnavailable, match="401"):
        client.health()
    with pytest.raises(CsiUnavailable):
        client.strategies()
    with pytest.raises(CsiUnavailable, match="verdict CSI inconnu"):
        client.evaluate("x", source="g")
    assert client.probe()[0] is None                      # plus de réponse préparée : erreur inattendue, jamais levée


def test_from_env_reads_url_and_token():
    client = CsiClient.from_env({"BSM_CSI_API_URL": "http://csi-api:8503/", "CSI_API_TOKEN": "t"})
    assert client.base_url == "http://csi-api:8503" and client.token == "t"
    assert CsiClient.from_env({}).base_url == "http://127.0.0.1:8503"


def test_gate_policy_can_only_hold_never_send():
    default = GatePolicy.from_mapping({})
    assert default.enabled and not default.hold_indetermine and not default.allow_when_unavailable
    assert default.decide(opinion("DEFAVORABLE"))[0] is False
    assert default.decide(opinion("REFUSE"))[0] is False
    assert default.decide(opinion("INDETERMINE"))[0] is True
    assert default.decide(opinion("FAVORABLE"))[0] is True
    allowed, detail = default.decide(None, failure="CSI injoignable")
    assert allowed is False and "retenue" in detail and "CSI injoignable" in detail

    strict = GatePolicy.from_mapping({"signal_csi_hold_indetermine": True})
    assert strict.decide(opinion("INDETERMINE"))[0] is False
    lenient = GatePolicy.from_mapping({"signal_csi_when_unavailable": "allow"})
    assert lenient.decide(None, failure="panne")[0] is True
    disabled = GatePolicy.from_mapping({"signal_csi_gate_enabled": False})
    assert disabled.decide(opinion("REFUSE"))[0] is True


def test_source_names_and_labels():
    names = source_names("-1001234=Suhaib\n 99 = ABK ;")
    assert names == {"-1001234": "Suhaib", "99": "ABK"}
    for bad in ("Suhaib", "abc=Groupe", "-100="):
        with pytest.raises(ValueError):
            source_names(bad)
    preferences = {"signal_csi_source_names": "-1001234=Suhaib"}
    telegram = {"source": "telegram", "external_id": "bothash:-1001234:42"}
    assert source_label(telegram, preferences) == "Suhaib"
    assert source_label({"source": "telegram", "external_id": "bothash:77:1"}, preferences) == "telegram 77"
    assert source_label({"source": "manual", "external_id": ""}, preferences) == "manuel"
    assert source_label(telegram, {"signal_csi_source_names": "invalide"}) == "telegram -1001234"
