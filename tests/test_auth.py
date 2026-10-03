"""Connexion : scrypt, TOTP RFC 6238, blocage après 5 échecs, expiration de session."""

import json
import stat
import sys
import urllib.parse

import pytest

from binance_spot_manager import auth
from binance_spot_manager.auth import (
    AccountStore, AuthResult, current_user, hash_password, open_session, totp, verify_password,
    verify_totp,
)
from binance_spot_manager.key_vault import KeyVault

PASSWORD = "correct horse battery"
RFC_SEED = b"12345678901234567890"
NOW = 1_800_000_000.0


# --------------------------------------------------------------------------
# Mots de passe
# --------------------------------------------------------------------------


def test_default_scrypt_parameters_are_robust_and_salted():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first.startswith("scrypt$17$8$1$")
    assert first != second  # sel aléatoire
    assert PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert not verify_password(PASSWORD + "!", first)


@pytest.mark.parametrize("encoded", ["", "scrypt$17$8$1$x$y", "bcrypt$1$2$3$4$5", "scrypt$40$8$1$AAAA$AAAA"])
def test_malformed_hash_never_verifies(encoded):
    assert not verify_password(PASSWORD, encoded)


def test_short_password_is_refused():
    with pytest.raises(ValueError, match="12"):
        hash_password("court")


# --------------------------------------------------------------------------
# TOTP : vecteurs de la RFC 6238 (annexe B, SHA-1, 8 chiffres)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("timestamp, expected", [
    (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
    (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130"),
])
def test_rfc6238_sha1_vectors(timestamp, expected):
    assert totp(RFC_SEED, timestamp, digits=8) == expected


def test_window_accepts_one_step_each_side_only():
    secret = b"x" * 20
    for offset in (-30, 0, 30):
        assert verify_totp(secret, totp(secret, NOW + offset), NOW) is not None
    for offset in (-60, 60, 90):
        assert verify_totp(secret, totp(secret, NOW + offset), NOW) is None


def test_replayed_or_older_code_is_refused():
    secret = b"y" * 20
    step = verify_totp(secret, totp(secret, NOW), NOW)
    assert verify_totp(secret, totp(secret, NOW), NOW, last_step=step) is None
    assert verify_totp(secret, totp(secret, NOW - 30), NOW, last_step=step) is None
    assert verify_totp(secret, totp(secret, NOW + 30), NOW + 30, last_step=step) == step + 1


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", None])
def test_malformed_codes_are_refused(code):
    assert verify_totp(b"z" * 20, code, NOW) is None


def test_otpauth_uri():
    uri = auth.otpauth_uri(RFC_SEED, "client.1")
    parsed = urllib.parse.urlparse(uri)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "otpauth" and parsed.netloc == "totp"
    assert query["secret"] == [auth.totp_secret_base32(RFC_SEED)]
    assert query["issuer"] == ["BinanceSpotManager"] and query["digits"] == ["6"]


# --------------------------------------------------------------------------
# Comptes
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    data = tmp_path / "data"
    vault = KeyVault(data / "key_vault.json", master_key_file=tmp_path / "keys" / "master.key")
    return AccountStore(data / "accounts.json", vault, log2_n=14)


@pytest.fixture
def account(store):
    return store.create("client", PASSWORD)


def login(store, account, *, now=NOW, password=PASSWORD, code=None):
    return store.authenticate("client", password, code or totp(account.totp_secret, now), now=now)


def test_successful_login_and_files_without_secret(store, account):
    result = login(store, account)
    assert result.ok and result.username == "client" and result.session_version == 1
    raw = store.path.read_text()
    assert PASSWORD not in raw and account.totp_base32 not in raw
    assert account.totp_base32 not in store.vault.path.read_text()
    assert account.totp_base32 not in repr(account)
    if sys.platform != "win32":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_wrong_password_wrong_code_and_unknown_user_share_one_message(store, account):
    messages = {
        login(store, account, password="mauvais mot de passe").message,
        login(store, account, code="000000" if totp(account.totp_secret, NOW) != "000000" else "111111").message,
        store.authenticate("inconnu", PASSWORD, "123456", now=NOW).message,
    }
    assert messages == {auth.GENERIC_FAILURE}


def test_code_cannot_be_reused(store, account):
    code = totp(account.totp_secret, NOW)
    assert login(store, account, code=code).ok
    assert not login(store, account, now=NOW + 5, code=code).ok


def test_five_failures_lock_the_account_for_fifteen_minutes(store, account):
    for attempt in range(4):
        assert login(store, account, now=NOW + attempt, password="mauvais mot de passe").message == auth.GENERIC_FAILURE
    blocked = login(store, account, now=NOW + 4, password="mauvais mot de passe")
    assert not blocked.ok and "bloqué" in blocked.message
    # Même le bon mot de passe et le bon code sont refusés pendant le blocage.
    during = login(store, account, now=NOW + 14 * 60)
    assert not during.ok and "bloqué" in during.message
    assert login(store, account, now=NOW + 15 * 60 + 5).ok


def test_success_resets_failure_counter(store, account):
    for attempt in range(4):
        login(store, account, now=NOW + attempt, password="mauvais mot de passe")
    assert login(store, account, now=NOW + 10).ok
    for attempt in range(4):
        assert not login(store, account, now=NOW + 40 + attempt, password="mauvais mot de passe").ok
    assert login(store, account, now=NOW + 90).ok


def test_duplicate_account_requires_replace_and_replace_closes_sessions(store, account):
    with pytest.raises(ValueError, match="existe déjà"):
        store.create("client", PASSWORD)
    state = {}
    open_session(state, login(store, account), now=NOW)
    replaced = store.create("client", PASSWORD + "2", replace=True)
    assert current_user(state, store, now=NOW + 1, idle_seconds=600) is None
    assert login(store, replaced, now=NOW + 60, password=PASSWORD + "2").ok


@pytest.mark.parametrize("name", ["", "a", "x" * 33, "nom avec espace", "../etc", "é"])
def test_invalid_usernames(store, name):
    with pytest.raises(ValueError):
        store.create(name, PASSWORD)


def test_delete_account_removes_totp_secret(store, account):
    assert store.delete("client")
    assert store.vault.get("totp:client") is None
    assert not store.has_accounts()


def test_unreadable_accounts_file_keeps_login_required(store, account, monkeypatch):
    store.path.write_text("{pas du json")
    monkeypatch.delenv("BSM_AUTH_REQUIRED", raising=False)
    assert auth.auth_required(store)
    with pytest.raises(RuntimeError):
        login(store, account)


# --------------------------------------------------------------------------
# Politique et sessions
# --------------------------------------------------------------------------


def test_auth_required_defaults_to_existing_accounts(store, monkeypatch):
    monkeypatch.delenv("BSM_AUTH_REQUIRED", raising=False)
    assert not auth.auth_required(store)  # installation actuelle sans compte : inchangée
    store.create("client", PASSWORD)
    assert auth.auth_required(store)
    monkeypatch.setenv("BSM_AUTH_REQUIRED", "false")
    assert not auth.auth_required(store)
    monkeypatch.setenv("BSM_AUTH_REQUIRED", "true")
    store.delete("client")
    assert auth.auth_required(store)


def test_session_expires_after_inactivity(store, account):
    state = {}
    open_session(state, login(store, account), now=NOW)
    assert current_user(state, store, now=NOW + 500, idle_seconds=600) == "client"
    # L'activité prolonge la session...
    assert current_user(state, store, now=NOW + 1000, idle_seconds=600) == "client"
    # ...un rafraîchissement automatique non.
    assert current_user(state, store, now=NOW + 1500, idle_seconds=600, touch=False) == "client"
    assert current_user(state, store, now=NOW + 1601, idle_seconds=600) is None
    assert auth.SESSION_KEY not in state


def test_session_of_deleted_account_is_closed(store, account):
    state = {}
    open_session(state, login(store, account), now=NOW)
    store.delete("client")
    assert current_user(state, store, now=NOW + 1, idle_seconds=600) is None


def test_failed_result_never_opens_a_session():
    with pytest.raises(ValueError):
        open_session({}, AuthResult(False, "non"))


def test_idle_timeout_bounds(monkeypatch):
    monkeypatch.setenv("BSM_SESSION_IDLE_MINUTES", "1")
    assert auth.idle_timeout_seconds() == 5 * 60
    monkeypatch.setenv("BSM_SESSION_IDLE_MINUTES", "abc")
    assert auth.idle_timeout_seconds() == 30 * 60


def test_creer_compte_script_prints_secret_once_and_checks_code(store, capsys):
    from scripts import creer_compte

    codes = []

    def code_prompt(_):
        account = json.loads(store.path.read_text())["accounts"]["nouveau"]
        assert account["failures"] == 0
        secret = store.vault.get("totp:nouveau")
        codes.append(totp(secret, NOW))
        return codes[-1]

    assert creer_compte.main(["nouveau"], store=store, password_prompt=lambda: PASSWORD,
                             code_prompt=code_prompt, clock=lambda: NOW) == 0
    out = capsys.readouterr().out
    secret = auth.totp_secret_base32(store.vault.get("totp:nouveau"))
    assert out.count(secret) == 2  # secret + URI, une seule fois
    assert "otpauth://totp/" in out
    # Le code de vérification n'a pas été consommé : il reste utilisable pour la connexion.
    assert store.authenticate("nouveau", PASSWORD, codes[0], now=NOW).ok


def test_creer_compte_script_without_a_terminal_skips_the_optional_code(store):
    """`docker compose run -T` : pas de terminal, la question facultative reçoit une fin de fichier."""
    from scripts import creer_compte

    def no_terminal(_):
        raise EOFError

    assert creer_compte.main(["sansterminal"], store=store, password_prompt=lambda: PASSWORD,
                             code_prompt=no_terminal, clock=lambda: NOW) == 0
    assert "sansterminal" in json.loads(store.path.read_text())["accounts"]
