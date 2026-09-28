"""Configuration centrale + garde-fous de securite DEMO / LIVE.

Regle absolue de la V2 :
    aucune ecriture (ordre, annulation, protection) ne peut partir
    ailleurs que vers une URL Binance Demo explicitement whitelistee.

Le mode LIVE existe dans le modele de configuration (pour que l'architecture
soit prete) mais toute tentative d'ecriture en LIVE leve une exception.
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Chemins projet
# --------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
POSITIONS_DIR = DATA_DIR / "positions"
SIGNALS_DIR = DATA_DIR / "signals"
LOGS_DIR = PROJECT_ROOT / "logs"

BOT_RUNTIME_FILE = DATA_DIR / "bot_runtime.json"
BOT_STOP_FLAG = DATA_DIR / "bot_stop.flag"
BOT_LOCK_FILE = DATA_DIR / "bot_worker.lock"
SETTINGS_FILE = DATA_DIR / "settings.json"
PRESETS_FILE = DATA_DIR / "presets.json"
EVENTS_FILE = LOGS_DIR / "events.jsonl"
ERROR_LOG_FILE = LOGS_DIR / "errors.log"


def ensure_directories() -> None:
    """Cree l'arborescence runtime si absente. Idempotent."""
    for directory in (DATA_DIR, POSITIONS_DIR, SIGNALS_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Enums d'environnement
# --------------------------------------------------------------------------


class Environment(str, Enum):
    DEMO = "DEMO"
    LIVE = "LIVE"


class RunMode(str, Enum):
    """Mode operationnel (section 71 du cahier des charges)."""

    DRY_RUN = "DRY_RUN"
    DEMO_MANUAL = "DEMO_MANUAL"
    DEMO_AUTO = "DEMO_AUTO"
    LIVE = "LIVE"  # reserve — bloque en V2


# --------------------------------------------------------------------------
# Whitelist de securite
# --------------------------------------------------------------------------

#: Seules ces URLs sont acceptees pour une operation d'ecriture.
#: `testnet.binance.vision` est le testnet Spot public de Binance.
#: `demo-api.binance.com` est conserve car present dans le cahier des charges.
ALLOWED_DEMO_BASE_URLS: frozenset[str] = frozenset(
    {
        "https://testnet.binance.vision",
        "https://demo-api.binance.com",
    }
)


class SecurityError(RuntimeError):
    """Levee quand une operation sortirait du perimetre Demo autorise."""


def normalize_base_url(url: str) -> str:
    """Retire les espaces et le slash final pour comparer de facon stricte."""
    return (url or "").strip().rstrip("/")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def _env_str(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env_str(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env_str(key)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


class Settings(BaseModel):
    """Configuration immuable chargee depuis l'environnement."""

    environment: Environment = Environment.DEMO
    run_mode: RunMode = RunMode.DRY_RUN

    demo_base_url: str = "https://testnet.binance.vision"
    demo_api_key: str = Field(default="", repr=False, exclude=True)
    demo_api_secret: str = Field(default="", repr=False, exclude=True)

    live_base_url: str = "https://api.binance.com"
    live_api_key: str = Field(default="", repr=False, exclude=True)
    live_api_secret: str = Field(default="", repr=False, exclude=True)

    recv_window: int = 5000
    http_timeout: int = 10

    worker_interval: int = 5
    heartbeat_stale_after: int = 20

    quote_asset: str = "USDT"
    capital_reserve_percent: float = 20.0
    max_risk_per_position_percent: float = 1.0
    max_total_risk_percent: float = 5.0
    max_open_positions: int = 5
    max_exposure_per_symbol_percent: float = 25.0

    taker_fee_percent: float = 0.1
    maker_fee_percent: float = 0.1

    telegram_bot_token: str = Field(default="", repr=False, exclude=True)
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = Field(default="", repr=False, exclude=True)
    smtp_from: str = ""
    smtp_to: str = ""

    model_config = {"frozen": True}

    # -- proprietes derivees -------------------------------------------------

    @property
    def is_demo(self) -> bool:
        return self.environment is Environment.DEMO

    @property
    def base_url(self) -> str:
        url = self.demo_base_url if self.is_demo else self.live_base_url
        return normalize_base_url(url)

    @property
    def api_key(self) -> str:
        return self.demo_api_key if self.is_demo else self.live_api_key

    @property
    def api_secret(self) -> str:
        return self.demo_api_secret if self.is_demo else self.live_api_secret

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def dry_run(self) -> bool:
        return self.run_mode is RunMode.DRY_RUN

    @property
    def requires_human_validation(self) -> bool:
        return self.run_mode is RunMode.DEMO_MANUAL

    @property
    def mode_label(self) -> str:
        """Libelle court destine a l'entete de l'interface."""
        return f"{self.environment.value} · {self.run_mode.value}"

    # -- garde-fous ----------------------------------------------------------

    def assert_write_allowed(self, operation: str = "operation") -> None:
        """Autorise ou interdit une ecriture. A appeler AVANT tout envoi.

        Leve SecurityError avec le message impose par le cahier des charges.
        """
        message = "SECURITE : operation interdite hors Binance Demo"

        if self.environment is not Environment.DEMO:
            raise SecurityError(
                f"{message} — mode {self.environment.value} non implemente en V2 "
                f"(operation refusee : {operation})"
            )

        if self.base_url not in ALLOWED_DEMO_BASE_URLS:
            raise SecurityError(
                f"{message} — URL non autorisee : {self.base_url!r} "
                f"(operation refusee : {operation})"
            )

    def redacted(self) -> dict[str, object]:
        """Vue serialisable sans aucun secret — utilisable dans les logs/UI."""
        return {
            "environment": self.environment.value,
            "run_mode": self.run_mode.value,
            "base_url": self.base_url,
            "api_key_set": bool(self.api_key),
            "api_secret_set": bool(self.api_secret),
            "quote_asset": self.quote_asset,
            "worker_interval": self.worker_interval,
            "capital_reserve_percent": self.capital_reserve_percent,
        }


def load_settings() -> Settings:
    """Charge .env puis construit les Settings (non mis en cache)."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    env_raw = _env_str("BSM_ENV", "DEMO").upper()
    environment = Environment.DEMO if env_raw != "LIVE" else Environment.LIVE

    mode_raw = _env_str("BSM_RUN_MODE", "DRY_RUN").upper()
    try:
        run_mode = RunMode(mode_raw)
    except ValueError:
        run_mode = RunMode.DRY_RUN
    if run_mode is RunMode.LIVE:
        # Refus explicite : jamais de bascule Live automatique.
        run_mode = RunMode.DRY_RUN

    return Settings(
        environment=environment,
        run_mode=run_mode,
        demo_base_url=_env_str("BSM_DEMO_BASE_URL", "https://testnet.binance.vision"),
        demo_api_key=_env_str("BSM_DEMO_API_KEY"),
        demo_api_secret=_env_str("BSM_DEMO_API_SECRET"),
        live_base_url=_env_str("BSM_LIVE_BASE_URL", "https://api.binance.com"),
        live_api_key=_env_str("BSM_LIVE_API_KEY"),
        live_api_secret=_env_str("BSM_LIVE_API_SECRET"),
        recv_window=_env_int("BSM_RECV_WINDOW", 5000),
        http_timeout=_env_int("BSM_HTTP_TIMEOUT", 10),
        worker_interval=_env_int("BSM_WORKER_INTERVAL", 5),
        heartbeat_stale_after=_env_int("BSM_HEARTBEAT_STALE_AFTER", 20),
        quote_asset=_env_str("BSM_QUOTE_ASSET", "USDT").upper(),
        capital_reserve_percent=_env_float("BSM_CAPITAL_RESERVE_PERCENT", 20.0),
        max_risk_per_position_percent=_env_float("BSM_MAX_RISK_PER_POSITION_PERCENT", 1.0),
        max_total_risk_percent=_env_float("BSM_MAX_TOTAL_RISK_PERCENT", 5.0),
        max_open_positions=_env_int("BSM_MAX_OPEN_POSITIONS", 5),
        max_exposure_per_symbol_percent=_env_float("BSM_MAX_EXPOSURE_PER_SYMBOL_PERCENT", 25.0),
        taker_fee_percent=_env_float("BSM_TAKER_FEE_PERCENT", 0.1),
        maker_fee_percent=_env_float("BSM_MAKER_FEE_PERCENT", 0.1),
        telegram_bot_token=_env_str("BSM_TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env_str("BSM_TELEGRAM_CHAT_ID"),
        smtp_host=_env_str("BSM_SMTP_HOST"),
        smtp_port=_env_int("BSM_SMTP_PORT", 587),
        smtp_user=_env_str("BSM_SMTP_USER"),
        smtp_password=_env_str("BSM_SMTP_PASSWORD"),
        smtp_from=_env_str("BSM_SMTP_FROM"),
        smtp_to=_env_str("BSM_SMTP_TO"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings mis en cache pour le process courant."""
    ensure_directories()
    return load_settings()


def reload_settings() -> Settings:
    """Vide le cache et recharge (utile apres modification de .env)."""
    get_settings.cache_clear()
    return get_settings()
