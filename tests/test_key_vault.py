"""Coffre des clés : chiffrement AES-256-GCM sur place, clé maîtresse séparée des données."""

import json
import logging
import os
import stat
import sys

import pytest

from binance_spot_manager import config, key_vault
from binance_spot_manager.backup_manager import allowed_member, build_backup
from binance_spot_manager.config import load_settings
from binance_spot_manager.key_vault import (
    KeyVault, TamperedVault, VaultError, WrongMasterKey, load_master_key,
)

API_KEY = "A" * 60 + "WXYZ"
API_SECRET = "s3cr3t" * 10 + "Q9"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="droits POSIX")


@pytest.fixture
def paths(tmp_path):
    data, keys = tmp_path / "data", tmp_path / "keys"
    data.mkdir()
    return data / "key_vault.json", keys / "master.key"


@pytest.fixture
def vault(paths):
    return KeyVault(paths[0], master_key_file=paths[1])


def test_round_trip_and_status_without_secret(vault, paths):
    status = vault.save_binance_keys(API_KEY, API_SECRET)
    assert status.defined and status.hint == "…WXYZ" and status.updated_at
    assert vault.load_binance_keys() == (API_KEY, API_SECRET)
    raw = paths[0].read_text()
    assert API_KEY not in raw and API_SECRET not in raw and API_KEY[:20] not in raw
    assert paths[1].exists()
    assert API_SECRET not in repr(vault) and API_SECRET not in repr(status)


def test_each_write_uses_a_fresh_nonce(vault, paths):
    vault.save_binance_keys(API_KEY, API_SECRET)
    first = json.loads(paths[0].read_text())["entries"]["binance"]
    vault.save_binance_keys(API_KEY, API_SECRET)
    second = json.loads(paths[0].read_text())["entries"]["binance"]
    assert first["nonce"] != second["nonce"] and first["ciphertext"] != second["ciphertext"]


def test_delete(vault):
    vault.save_binance_keys(API_KEY, API_SECRET)
    assert vault.delete_binance_keys()
    assert vault.load_binance_keys() is None
    assert not vault.binance_status().defined
    assert not vault.delete_binance_keys()


@pytest.mark.parametrize("field", ["ciphertext", "nonce", "hint", "updated_at"])
def test_tampered_entry_is_refused(vault, paths, field):
    vault.save_binance_keys(API_KEY, API_SECRET)
    document = json.loads(paths[0].read_text())
    entry = document["entries"]["binance"]
    if field in {"ciphertext", "nonce"}:
        raw = bytearray(key_vault._unb64(entry[field]))
        raw[0] ^= 1
        entry[field] = key_vault._b64(bytes(raw))
    else:
        entry[field] = entry[field] + "x"
    paths[0].write_text(json.dumps(document))
    with pytest.raises(TamperedVault):
        vault.load_binance_keys()


def test_entry_moved_under_another_name_is_refused(vault, paths):
    vault.put("autre", b"valeur")
    document = json.loads(paths[0].read_text())
    document["entries"]["binance"] = document["entries"]["autre"]
    paths[0].write_text(json.dumps(document))
    with pytest.raises(TamperedVault):
        vault.get("binance")


def test_truncated_vault_is_refused(vault, paths):
    vault.save_binance_keys(API_KEY, API_SECRET)
    paths[0].write_text(paths[0].read_text()[:40])
    with pytest.raises(TamperedVault):
        vault.load_binance_keys()


def test_wrong_master_key_is_refused(vault, paths, tmp_path):
    vault.save_binance_keys(API_KEY, API_SECRET)
    other_key = tmp_path / "other" / "master.key"
    load_master_key(other_key, create=True, data_dir=paths[0].parent)  # une autre clé, valide
    other = KeyVault(paths[0], master_key_file=other_key)
    with pytest.raises(WrongMasterKey):
        other.load_binance_keys()
    with pytest.raises(WrongMasterKey):
        other.save_binance_keys(API_KEY, API_SECRET)
    assert vault.load_binance_keys() == (API_KEY, API_SECRET)


def test_missing_master_key_is_not_recreated_on_read(vault, paths):
    vault.save_binance_keys(API_KEY, API_SECRET)
    paths[1].unlink()
    with pytest.raises(VaultError, match="absente"):
        vault.load_binance_keys()
    assert not paths[1].exists()


@posix_only
def test_master_key_and_vault_are_private_files(vault, paths):
    vault.save_binance_keys(API_KEY, API_SECRET)
    assert stat.S_IMODE(paths[1].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths[1].parent.stat().st_mode) & 0o077 == 0


@posix_only
def test_master_key_readable_by_others_is_refused(vault, paths):
    vault.save_binance_keys(API_KEY, API_SECRET)
    os.chmod(paths[1], 0o644)
    with pytest.raises(VaultError, match="chmod 600"):
        vault.load_binance_keys()


def test_master_key_inside_data_or_project_is_refused(tmp_path):
    data = tmp_path / "data"
    with pytest.raises(VaultError, match="données"):
        load_master_key(data / "master.key", create=True, data_dir=data)
    with pytest.raises(VaultError, match="projet"):
        load_master_key(config.PROJECT_ROOT / "master.key", create=True, data_dir=data)
    assert not (config.PROJECT_ROOT / "master.key").exists()


@pytest.mark.parametrize("key, secret", [
    ("", API_SECRET), (API_KEY, ""), ("court", API_SECRET), (API_KEY + " ", API_SECRET + "\n x"),
    (API_KEY, API_KEY), ("é" * 64, API_SECRET),
])
def test_invalid_keys_are_rejected_before_storage(vault, paths, key, secret):
    with pytest.raises(ValueError):
        vault.save_binance_keys(key, secret)
    assert not paths[0].exists()


def test_no_secret_in_logs(vault, caplog):
    caplog.set_level(logging.DEBUG)
    vault.save_binance_keys(API_KEY, API_SECRET)
    vault.load_binance_keys()
    assert API_SECRET not in caplog.text and API_KEY not in caplog.text


# --------------------------------------------------------------------------
# load_settings : l'environnement garde la priorité, le coffre sert sinon
# --------------------------------------------------------------------------


@pytest.fixture
def isolated_env(monkeypatch, paths):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)  # jamais le .env réel
    for name in list(os.environ):
        if name.startswith("BSM_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(key_vault, "VAULT_FILE", paths[0])
    monkeypatch.setenv("BSM_MASTER_KEY_FILE", str(paths[1]))
    return paths


def test_settings_use_vault_when_environment_is_empty(isolated_env):
    KeyVault().save_binance_keys(API_KEY, API_SECRET)
    settings = load_settings()
    assert settings.credentials_source == "vault"
    assert (settings.api_key, settings.api_secret) == (API_KEY, API_SECRET)
    for text in (repr(settings), str(settings), settings.model_dump_json(), json.dumps(settings.redacted())):
        assert API_KEY not in text and API_SECRET not in text


def test_environment_keys_take_priority_over_vault(isolated_env, monkeypatch):
    KeyVault().save_binance_keys(API_KEY, API_SECRET)
    monkeypatch.setenv("BSM_DEMO_API_KEY", "E" * 64)
    monkeypatch.setenv("BSM_DEMO_API_SECRET", "F" * 64)
    settings = load_settings()
    assert settings.credentials_source == "env"
    assert settings.api_key == "E" * 64


def test_unreadable_vault_never_blocks_settings(isolated_env):
    KeyVault().save_binance_keys(API_KEY, API_SECRET)
    isolated_env[1].unlink()
    settings = load_settings()
    assert not settings.has_credentials and settings.credentials_source == ""
    assert "absente" in settings.credentials_error


def test_vault_never_supplies_live_keys(isolated_env, monkeypatch):
    KeyVault().save_binance_keys(API_KEY, API_SECRET)
    monkeypatch.setenv("BSM_ENV", "LIVE")
    settings = load_settings()
    assert settings.live_api_key == "" and settings.api_key == ""
    assert settings.dry_run


# --------------------------------------------------------------------------
# Sauvegardes : ni coffre, ni clé maîtresse, ni comptes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "data/key_vault.json", "data/master.key", "data/accounts.json", "data/licence.json",
    "data/key_vault.json.lock", "keys/master.key", "master.key",
])
def test_backup_whitelist_excludes_secrets(name):
    assert not allowed_member(name)


def test_backup_never_contains_vault_or_master_key(tmp_path):
    import io
    import zipfile

    data = tmp_path / "data"
    (data / "positions").mkdir(parents=True)
    (data / "settings.json").write_text("{}")
    vault = KeyVault(data / "key_vault.json", master_key_file=tmp_path / "keys" / "master.key")
    vault.save_binance_keys(API_KEY, API_SECRET)
    (data / "accounts.json").write_text("{}")
    names = zipfile.ZipFile(io.BytesIO(build_backup(data, worker_running=False))).namelist()
    assert names == ["manifest.json", "data/settings.json"]
