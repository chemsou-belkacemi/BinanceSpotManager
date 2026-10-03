"""Droits d'une clé (réponse apiRestrictions) : contrôle pur, hors ligne, jamais appelé sur Demo."""

import ast
from pathlib import Path

import pytest

from binance_spot_manager.api_key_policy import assess_api_restrictions

ROOT = Path(__file__).resolve().parents[1]

SAFE = {
    "ipRestrict": True, "createTime": 1698645219000, "enableReading": True,
    "enableSpotAndMarginTrading": True, "enableWithdrawals": False,
    "enableInternalTransfer": False, "permitsUniversalTransfer": False,
    "enableMargin": False, "enableFutures": False, "enableVanillaOptions": False,
    "enablePortfolioMarginTrading": False,
}


def test_dedicated_restricted_key_is_accepted():
    report = assess_api_restrictions(SAFE)
    assert report.accepted and not report.refusals and not report.warnings


def test_withdrawals_enabled_is_refused():
    report = assess_api_restrictions(SAFE | {"enableWithdrawals": True})
    assert not report.accepted and "retrait" in report.refusals[0].lower()


def test_missing_withdrawal_flag_is_refused():
    payload = dict(SAFE)
    del payload["enableWithdrawals"]
    assert not assess_api_restrictions(payload).accepted


def test_unrestricted_ip_is_a_warning_only():
    report = assess_api_restrictions(SAFE | {"ipRestrict": False})
    assert report.accepted and any("IP" in w for w in report.warnings)


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_spot_trading_absent_is_refused(value):
    report = assess_api_restrictions(SAFE | {"enableSpotAndMarginTrading": value})
    assert not report.accepted and any("Spot" in r for r in report.refusals)


def test_transfer_and_unused_rights_are_reported():
    report = assess_api_restrictions(SAFE | {"permitsUniversalTransfer": True, "enableFutures": True})
    assert report.accepted and len(report.warnings) == 2


@pytest.mark.parametrize("payload", [None, [], "texte", 42])
def test_unreadable_answer_is_refused(payload):
    assert not assess_api_restrictions(payload).accepted


def test_policy_is_never_called_and_no_sapi_route_exists():
    """Demo n'a pas cette route : aucune route /sapi, aucun appel du contrôle dans le code."""
    for path in [*ROOT.glob("binance_spot_manager/*.py"), *ROOT.glob("scripts/*.py"),
                 *ROOT.glob("pages/*.py"), ROOT / "app.py", ROOT / "ui_common.py"]:
        source = path.read_text(encoding="utf-8")
        if path.name != "api_key_policy.py":
            assert "/sapi/" not in source, path
            calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
                     and getattr(n.func, "id", getattr(n.func, "attr", "")) == "assess_api_restrictions"]
            assert not calls, path
