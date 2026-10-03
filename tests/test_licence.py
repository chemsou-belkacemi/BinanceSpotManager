"""Licence de location Ed25519 : vérification hors ligne, nouvelles entrées seulement bloquées."""

import base64
import json
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from binance_spot_manager import licence
from binance_spot_manager.licence import LicenceGate, LicenceStatus, sign_licence, verify_licence
from tests.test_commands import processor, proposed, queue_position  # noqa: F401 - fixture
from tests.test_orphan_orders import worker_with_signal_waiting

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()


@pytest.fixture(scope="module")
def owner_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture(scope="module")
def public(owner_key):
    return licence.public_key_b64(owner_key)


def issued(owner_key, fin="2026-12-31", **overrides):
    fields = dict(licence_id="lic-1", client="Client SARL", offre="mensuelle", fin=fin) | overrides
    return sign_licence(owner_key, **fields)


def test_valid_signature(owner_key, public):
    status = verify_licence(issued(owner_key), public, now=NOW)
    assert status.valid and status.client == "Client SARL" and status.offre == "mensuelle"
    assert status.fin == "2026-12-31" and status.days_left == 91 and not status.expiring_soon


@pytest.mark.parametrize("field, value", [("client", "Autre"), ("fin", "2099-12-31"), ("offre", "illimitée"),
                                          ("id", "lic-2")])
def test_tampered_licence_is_refused(owner_key, public, field, value):
    document = issued(owner_key)
    document["licence"][field] = value
    status = verify_licence(document, public, now=NOW)
    assert not status.valid and "Signature" in status.reason


def test_added_field_is_refused(owner_key, public):
    document = issued(owner_key)
    document["licence"]["instances"] = 99
    assert not verify_licence(document, public, now=NOW).valid


def test_other_issuer_is_refused(owner_key):
    other = licence.public_key_b64(Ed25519PrivateKey.generate())
    assert not verify_licence(issued(owner_key), other, now=NOW).valid


def test_expired_licence(owner_key, public):
    document = issued(owner_key, fin="2026-09-30")
    status = verify_licence(document, public, now=NOW)
    assert not status.valid and "expirée" in status.reason
    # Le dernier jour est inclus jusqu'à minuit UTC.
    last_day = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc).timestamp()
    assert verify_licence(document, public, now=last_day).valid


def test_expiring_soon(owner_key, public):
    assert verify_licence(issued(owner_key, fin="2026-10-05"), public, now=NOW).expiring_soon


@pytest.mark.parametrize("key", ["", "   ", "pas-du-base64", base64.b64encode(b"court").decode()])
def test_missing_or_invalid_public_key(owner_key, key):
    status = verify_licence(issued(owner_key), key, now=NOW)
    assert not status.valid and "Clé publique" in status.reason


@pytest.mark.parametrize("document", [None, [], {"licence": "x"}, {"licence": {"client": "a"}},
                                      {"licence": {"id": "1", "client": "a", "offre": "b", "fin": "2026-12-31"}}])
def test_malformed_documents(document, public):
    assert not verify_licence(document, public, now=NOW).valid


def test_signing_requires_complete_fields(owner_key):
    with pytest.raises(ValueError):
        issued(owner_key, client=" ")
    with pytest.raises(ValueError):
        issued(owner_key, fin="31/12/2026")


def test_file_status_and_install(tmp_path, owner_key, public):
    path = tmp_path / "licence.json"
    assert "Aucune licence" in licence.current_status(path=path, public_key_text=public, now=NOW).reason
    forged = issued(owner_key)
    forged["licence"]["fin"] = "2099-01-01"
    refused = licence.install_licence(json.dumps(forged).encode(), path=path, public_key_text=public, now=NOW)
    assert not refused.valid and not path.exists()
    good = licence.install_licence(json.dumps(issued(owner_key)).encode(), path=path, public_key_text=public, now=NOW)
    assert good.valid and licence.current_status(path=path, public_key_text=public, now=NOW).valid
    path.write_text("{abîmé")
    assert not licence.current_status(path=path, public_key_text=public, now=NOW).valid


def test_gate_not_required_by_default(monkeypatch):
    monkeypatch.delenv("BSM_LICENCE_REQUIRED", raising=False)
    gate = LicenceGate(status_loader=lambda: pytest.fail("pas de lecture sans exigence"))
    assert gate.refusal() == ""


def test_gate_caches_status_and_refuses_when_invalid():
    statuses = [LicenceStatus(False, "Licence expirée depuis le 2026-09-30"), LicenceStatus(True)]
    clock = [0.0]
    gate = LicenceGate(required=True, status_loader=lambda: statuses.pop(0), clock=lambda: clock[0], ttl=60)
    assert "expirée" in gate.refusal() and "positions ouvertes restent suivies" in gate.refusal()
    clock[0] = 59
    assert gate.refusal()  # résultat gardé : pas de relecture à chaque tour
    clock[0] = 60
    assert gate.refusal() == ""


# --------------------------------------------------------------------------
# Commandes : nouvelles entrées refusées, sorties toujours exécutées
# --------------------------------------------------------------------------


def test_new_entries_are_refused_without_licence(processor):  # noqa: F811
    worker, calls = processor
    worker.entry_gate = lambda: "Licence requise : Licence expirée. Aucune nouvelle entrée"
    position = queue_position(worker)
    assert worker.run_one() == "FAILED"
    assert calls == [] and not worker.positions.exists(position.position_id)
    worker.store.enqueue(worker.scope, "SIMPLE_BUY", {"symbol": "BTCUSDT"}, request_key="buy")
    assert worker.run_one() == "FAILED"
    assert calls == []
    results = [row["result"] for row in worker.store.list_recent(worker.scope)]
    assert all("Licence" in json.dumps(result, ensure_ascii=False) for result in results)


@pytest.mark.parametrize("action", ["CANCEL_ORDER", "MOVE_SL", "CLOSE_LOCAL", "CLOSE_MARKET"])
def test_exits_are_never_blocked_by_licence(processor, monkeypatch, action):  # noqa: F811
    worker, _ = processor
    worker.entry_gate = lambda: pytest.fail("une sortie ne consulte jamais la licence")
    handled = []
    monkeypatch.setattr(worker, "_" + action.lower(), lambda payload: handled.append(payload) or {"ok": True})
    worker.store.enqueue(worker.scope, action, {"position_id": "x"}, request_key=action)
    assert worker.run_one() == "SUCCEEDED"
    assert handled == [{"position_id": "x"}]


def test_valid_licence_lets_entries_through(processor):  # noqa: F811
    worker, calls = processor
    worker.entry_gate = lambda: ""
    queue_position(worker)
    assert worker.run_one() == "SUCCEEDED" and len(calls) == 1


# --------------------------------------------------------------------------
# Worker : aucune mise en file automatique, positions toujours suivies
# --------------------------------------------------------------------------


def test_worker_without_licence_keeps_managing_positions_but_queues_nothing(tmp_path):
    setup = worker_with_signal_waiting(tmp_path, lambda: [])
    refusal = {"value": "Licence requise : Aucune licence installée (licence.json). Aucune nouvelle entrée"}
    gate = LicenceGate(required=True, status_loader=lambda: None)
    gate.refusal = lambda: refusal["value"]
    setup.worker.licence_gate = gate
    setup.worker.auto_signal_executor.entry_gate = gate.refusal

    setup.worker._tick()
    setup.worker._loop = 2
    setup.worker._tick()

    assert setup.commands.list_recent("demo") == []
    assert setup.worker.auto_signal_executor.snapshot()["state"] == "LICENCE"
    assert setup.monitored == [setup.tracked.position_id] * 2  # le suivi continue
    alerts = [e for e in setup.events.tail() if "Licence requise" in e["message"]]
    assert len(alerts) == 1 and alerts[0]["level"] == "CRITICAL"  # un événement, pas un par tour

    refusal["value"] = ""  # licence installée
    setup.worker._loop = 3
    setup.worker._tick()
    assert len(setup.commands.list_recent("demo")) == 1
    assert any("Licence valide" in e["message"] for e in setup.events.tail())


def test_emettre_licence_script_round_trip(tmp_path, monkeypatch):
    from scripts import emettre_licence

    private = tmp_path / "owner" / "licence.pem"
    passphrase = b"phrase de passe solide"
    public = emettre_licence.generate_keypair(private, passphrase)
    assert private.read_bytes().startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
    with pytest.raises(ValueError, match="jamais écrasée"):
        emettre_licence.generate_keypair(private, passphrase)
    document = emettre_licence.issue(private, passphrase, client="Client", offre="annuelle", fin="2027-09-30")
    assert verify_licence(document, public, now=NOW).valid
    with pytest.raises(ValueError):  # mauvaise phrase de passe : clé privée indéchiffrable
        emettre_licence.issue(private, b"mauvaise phrase", client="Client", offre="annuelle", fin="2027-09-30")
    with pytest.raises(ValueError, match="projet"):
        emettre_licence.generate_keypair(emettre_licence.PROJECT_ROOT / "cle.pem", passphrase)
