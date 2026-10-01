"""Interface louée : connexion avant tout affichage, saisie des clés dans le coffre."""

import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from binance_spot_manager import auth, config, key_vault
from binance_spot_manager.auth import AccountStore
from binance_spot_manager.key_vault import KeyVault
from binance_spot_manager.position_store import JsonFileStore

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "mot de passe du client"
API_KEY = "K" * 60 + "LAST"
API_SECRET = "S" * 64


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """Instance isolée : comptes, coffre et clé maîtresse dans un dossier temporaire."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(auth, "ACCOUNTS_FILE", data / "accounts.json")
    monkeypatch.setattr(key_vault, "VAULT_FILE", data / "key_vault.json")
    monkeypatch.setenv("BSM_MASTER_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    monkeypatch.delenv("BSM_AUTH_REQUIRED", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    for name in ("BSM_DEMO_API_KEY", "BSM_DEMO_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    store = JsonFileStore(tmp_path / "settings.json")
    monkeypatch.setattr("binance_spot_manager.position_store.get_settings_store", lambda: store)
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_balances",
                        lambda self: {"BNB": {"free": 0.1, "locked": 0}})
    monkeypatch.setattr("binance_spot_manager.binance_client.BinanceSpotClient.get_price", lambda self, symbol: 500)
    config.get_settings.cache_clear()
    yield data
    config.get_settings.cache_clear()  # ne jamais laisser des clés de test dans le cache global


def texts(app):
    return " ".join(str(getattr(element, "value", "")) for element in
                    [*app.markdown, *app.title, *app.caption, *app.error, *app.success, *app.info, *app.warning])


def login(app, code, password=PASSWORD):
    app.text_input[0].input("client")
    app.text_input[1].input(password)
    app.text_input[2].input(code)
    app.button[0].click().run()


def test_without_account_the_owner_install_is_unchanged(instance):
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    assert not app.exception
    assert "Par où commencer" in texts(app)


def test_login_required_once_an_account_exists(instance):
    account = AccountStore().create("client", PASSWORD)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    assert not app.exception
    assert "Connexion requise" in texts(app)
    assert "Par où commencer" not in texts(app) and not app.sidebar.markdown  # rien d'autre affiché

    login(app, "000000" if auth.totp(account.totp_secret, time.time()) != "000000" else "111111")
    assert auth.GENERIC_FAILURE in texts(app) and "Par où commencer" not in texts(app)

    login(app, auth.totp(account.totp_secret, time.time()))
    assert not app.exception
    assert "Par où commencer" in texts(app)

    # Inactivité dépassée : retour à la page de connexion.
    app.session_state[auth.SESSION_KEY]["last_seen"] = time.time() - auth.idle_timeout_seconds() - 1
    app.run()
    assert "Connexion requise" in texts(app) and "Par où commencer" not in texts(app)


def test_every_page_is_protected(instance):
    AccountStore().create("client", PASSWORD)
    for page in sorted((ROOT / "pages").glob("*.py")):
        app = AppTest.from_file(str(page), default_timeout=20).run()
        assert not app.exception, page.name
        assert "Connexion requise" in texts(app), page.name
        assert len(app.text_input) == 3 and not app.sidebar.markdown, page.name


def test_forced_login_without_account_explains_how_to_create_one(instance, monkeypatch):
    monkeypatch.setenv("BSM_AUTH_REQUIRED", "true")
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    assert "creer_compte.py" in texts(app) and "Par où commencer" not in texts(app)


def test_keys_are_entered_encrypted_and_never_shown_again(instance):
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    assert not app.exception
    key_input = next(w for w in app.text_input if w.label == "Clé API")
    secret_input = next(w for w in app.text_input if w.label == "Secret API")
    assert key_input.proto.type == key_input.proto.PASSWORD
    assert secret_input.proto.type == secret_input.proto.PASSWORD
    key_input.input(API_KEY)
    secret_input.input(API_SECRET)
    next(b for b in app.button if b.label == "Chiffrer et enregistrer mes clés").click().run()
    assert not app.exception
    assert KeyVault().load_binance_keys() == (API_KEY, API_SECRET)
    shown = texts(app)
    assert API_SECRET not in shown and API_KEY not in shown
    assert "LAST" in shown  # seuls les 4 derniers caractères de la clé API
    assert next(w for w in app.text_input if w.label == "Secret API").value == ""  # formulaire vidé
    assert config.get_settings().credentials_source == "vault"

    app.checkbox(key="confirm_delete_keys").check().run()
    next(b for b in app.button if b.label == "Supprimer mes clés").click().run()
    assert not app.exception
    assert KeyVault().load_binance_keys() is None


def test_keys_cannot_be_replaced_while_positions_are_open(instance, monkeypatch):
    monkeypatch.setattr("binance_spot_manager.dashboard_service.DashboardService.summary",
                        lambda self: {"open": 2, "total": 2})
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20).run()
    app.switch_page("pages/5_Settings.py").run()
    next(w for w in app.text_input if w.label == "Clé API").input(API_KEY)
    next(w for w in app.text_input if w.label == "Secret API").input(API_SECRET)
    next(b for b in app.button if b.label == "Chiffrer et enregistrer mes clés").click().run()
    assert "position(s) ouverte(s)" in texts(app)
    assert not KeyVault().binance_status().defined
