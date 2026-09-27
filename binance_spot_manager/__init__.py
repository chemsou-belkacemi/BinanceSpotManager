"""BinanceSpotManager V2 — gestionnaire de positions Spot Binance (mode Demo).

Ce package est volontairement decoupe en moteurs a responsabilite unique.
Voir README.md pour l'architecture detaillee.
"""

__version__ = "2.0.0.dev0"

__all__ = [
    "config",
    "binance_client",
    "symbol_rules",
    "models",
    "position_store",
    "event_store",
    "strategy_engine",
    "risk_engine",
    "position_engine",
    "execution_engine",
    "automation_engine",
    "reconciliation_engine",
    "notification_engine",
    "bot_process_manager",
    "dashboard_service",
]
