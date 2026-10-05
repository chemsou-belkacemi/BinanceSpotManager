"""Durcissement pour la location : TLS, proxys, verrou Demo hors ligne, réseau, conteneurs, pages."""

import ast
import os
import re
import ssl
from pathlib import Path

import pytest

from binance_spot_manager import config, notification_engine
from binance_spot_manager.binance_client import BinanceSpotClient
from binance_spot_manager.config import Environment, RunMode, SecurityError, Settings, load_settings
from binance_spot_manager.notification_engine import EmailChannel, Notification

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [*ROOT.glob("binance_spot_manager/*.py"), *ROOT.glob("scripts/*.py"),
           *ROOT.glob("pages/*.py"), ROOT / "app.py", ROOT / "ui_common.py"]


# --------------------------------------------------------------------------
# TLS et proxys
# --------------------------------------------------------------------------


def test_smtp_starttls_verifies_certificate(monkeypatch):
    seen = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            seen["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context=None):
            seen["context"] = context

        def login(self, user, password):
            seen["login"] = user

        def send_message(self, message):
            seen["sent"] = True

    monkeypatch.setattr(notification_engine.smtplib, "SMTP", FakeSMTP)
    settings = Settings(smtp_host="smtp.example.org", smtp_user="u", smtp_password="p",
                        smtp_from="a@example.org", smtp_to="b@example.org")
    assert EmailChannel(settings).send(Notification(event="t", title="t", body="b"))
    context = seen["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname
    assert seen["login"] == "u" and seen["sent"]


def test_binance_session_ignores_environment_proxies_and_ca_bundle(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://intercepteur.example:8080")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/fausse-autorite.pem")
    client = BinanceSpotClient(Settings())
    assert client._session.trust_env is False
    settings = client._session.merge_environment_settings(client._url("/api/v3/ping"), {}, None, None, None)
    assert settings["proxies"] == {} and settings["verify"] is True


def test_price_stream_disables_environment_proxy(monkeypatch):
    from binance_spot_manager.market_price_stream import DemoMarketPriceStream

    stream = DemoMarketPriceStream(Settings())
    assert stream._connect.keywords == {"proxy": None}


# --------------------------------------------------------------------------
# Verrou Demo : load_settings hors ligne (auparavant seulement en intégration)
# --------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)  # jamais le .env réel
    for name in list(os.environ):
        if name.startswith("BSM_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr("binance_spot_manager.key_vault.VAULT_FILE", tmp_path / "absent.json")
    return monkeypatch


def test_run_mode_live_is_downgraded(clean_env):
    clean_env.setenv("BSM_RUN_MODE", "LIVE")
    settings = load_settings()
    assert settings.run_mode is RunMode.DRY_RUN and settings.is_demo


@pytest.mark.parametrize("run_mode", ["LIVE", "DEMO_AUTO", "DEMO_MANUAL"])
def test_env_live_is_inert(clean_env, run_mode):
    clean_env.setenv("BSM_ENV", "live")
    clean_env.setenv("BSM_RUN_MODE", run_mode)
    clean_env.setenv("BSM_LIVE_API_KEY", "L" * 64)
    clean_env.setenv("BSM_LIVE_API_SECRET", "M" * 64)
    settings = load_settings()
    assert settings.environment is Environment.LIVE
    assert settings.run_mode is RunMode.DRY_RUN
    with pytest.raises(SecurityError):
        settings.assert_write_allowed("create_order")


@pytest.mark.parametrize("raw", ["", "demo", "LIVE ", "prod", "REAL"])
def test_only_exact_live_leaves_demo(clean_env, raw):
    clean_env.setenv("BSM_ENV", raw)
    settings = load_settings()
    assert settings.environment is (Environment.LIVE if raw.strip().upper() == "LIVE" else Environment.DEMO)
    assert settings.run_mode is RunMode.DRY_RUN


def test_forged_demo_url_refuses_writes(clean_env):
    clean_env.setenv("BSM_DEMO_BASE_URL", "https://api.binance.com")
    clean_env.setenv("BSM_RUN_MODE", "DEMO_AUTO")
    with pytest.raises(SecurityError):
        load_settings().assert_write_allowed("create_order")


# --------------------------------------------------------------------------
# Architecture : seul binance_client parle à Binance
# --------------------------------------------------------------------------

NETWORK_MODULES = {"requests", "urllib.request", "http.client", "httpx", "aiohttp", "urllib3",
                   "websockets", "websocket", "socket", "ssl"}
#: Modules autorisés à importer une bibliothèque réseau, et pourquoi.
ALLOWED_NETWORK_IMPORTS = {
    "binance_client.py": {"requests"},              # seul client REST Binance (liste blanche Demo)
    "market_price_stream.py": {"websockets"},       # flux public de prix, hôtes Demo seulement
    "csi_client.py": {"requests"},                  # API locale de CSI
    "telegram_signals.py": {"requests"},            # api.telegram.org
    "notification_engine.py": {"urllib.request", "ssl"},  # Telegram et SMTP
    "watchdog.py": {"urllib.request"},              # signal de vie externe facultatif (https, aucune donnée)
}
BINANCE_HOST = re.compile(r"binance\.(com|vision|us)|binanceapi|/api/v3/|/sapi/", re.IGNORECASE)
#: Seuls ces fichiers peuvent nommer un hôte ou une route Binance (texte d'aide compris).
BINANCE_HOST_ALLOWED = {"config.py", "binance_client.py", "market_price_stream.py", "5_Settings.py", "app.py"}


def imported_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_only_whitelisted_modules_import_network_libraries(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {name for name in imported_modules(tree)
             if name in NETWORK_MODULES or name.split(".")[0] in NETWORK_MODULES - {"urllib"}}
    found = {name if name in NETWORK_MODULES else name.split(".")[0] for name in found}
    assert found <= ALLOWED_NETWORK_IMPORTS.get(path.name, set()), (
        f"{path.name} importe {sorted(found)} : passer par BinanceSpotClient pour joindre Binance"
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_binance_hosts_and_routes_stay_in_the_client(path):
    if path.name in BINANCE_HOST_ALLOWED:
        return
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    literals = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]
    offending = [text for text in literals if BINANCE_HOST.search(text)]
    assert not offending, f"{path.name} nomme Binance hors du client : {offending[:3]}"


# --------------------------------------------------------------------------
# Chaque page exige la connexion avant tout affichage
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [ROOT / "app.py", *sorted(ROOT.glob("pages/*.py"))], ids=lambda p: p.name)
def test_every_page_calls_the_login_guard_before_rendering(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guard_index = None
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "require_login":
            guard_index = index
            break
    assert guard_index is not None, f"{path.name} n'appelle pas require_login()"
    for node in tree.body[:guard_index]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        assert "st" not in names, f"{path.name} : affichage avant la connexion ({ast.unparse(node)[:60]})"
        assert not names & {"get_settings", "get_service", "reload_settings", "banner", "sidebar_status"}, path.name


# --------------------------------------------------------------------------
# Conteneurs et image
# --------------------------------------------------------------------------


def compose_service(name):
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S)", text, re.MULTILINE | re.DOTALL)
    assert match, name
    return match.group(1)


def test_compose_hardening_applies_to_long_running_services():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    hardening = re.search(r"^x-hardening: &hardening\n(.*?)(?=^\S)", text, re.MULTILINE | re.DOTALL).group(1)
    for expected in ("no-new-privileges:true", "cap_drop: [ALL]", "read_only: true", "/tmp:",
                     "pids_limit:", "max-size:", "max-file:"):
        assert expected in hardening, expected
    app = re.search(r"^x-app: &app\n(.*?)(?=^\S)", text, re.MULTILINE | re.DOTALL).group(1)
    assert "<<: *hardening" in app
    for service in ("worker", "ui"):
        block = compose_service(service)
        assert "<<: *app" in block and "mem_limit:" in block and "cpus:" in block


def test_master_key_is_read_only_except_in_keytool_and_never_in_data_volume():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    app = re.search(r"^x-app: &app\n(.*?)(?=^\S)", text, re.MULTILINE | re.DOTALL).group(1)
    assert "bsm-keys:/var/lib/bsm-keys:ro" in app
    assert "BSM_MASTER_KEY_FILE: /var/lib/bsm-keys/master.key" in app
    keytool = compose_service("keytool")
    assert "bsm-keys:/var/lib/bsm-keys\n" in keytool and "network_mode: none" in keytool
    for tool in ("test", "integration", "proxy"):
        assert "bsm-keys" not in compose_service(tool)
    assert not re.search(r"/app/data/.*master", text)


def test_dockerignore_excludes_agents_keys_and_secrets():
    lines = set((ROOT / ".dockerignore").read_text(encoding="utf-8").split())
    assert {".claude/", ".env", "*.env", "data/", "*.key", "*.pem"} <= lines


def test_gitignore_excludes_keys():
    lines = set((ROOT / ".gitignore").read_text(encoding="utf-8").split())
    assert {".env", "data/", "*.key", "*.pem"} <= lines


def test_cryptography_is_pinned_with_an_upper_bound():
    requirement = next(line for line in (ROOT / "requirements.txt").read_text().splitlines()
                       if line.startswith("cryptography"))
    assert ">=" in requirement and "<" in requirement
