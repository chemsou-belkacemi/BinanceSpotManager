"""Presentation helpers: signs and explicit quote -> USDT valuation."""
import math
import re


def pnl_css(value):
    try:
        # Existing formatted UI amounts use spaces as thousands separators.
        text = str(value).replace(" ", "").replace("\u202f", "").replace("\u00a0", "")
        match = re.match(r"[+-]?\d+(?:[.,]\d+)?", text)
        number = float(match.group().replace(",", ".")) if match else 0.0
    except (ValueError, TypeError):
        number = 0.0
    return "color: #16a34a" if number > 0 else "color: #ef4444" if number < 0 else ""


def position_return(position, usdt_rate=1.0, fee_rates=None):
    from .accounting import accounting_snapshot
    snapshot = accounting_snapshot(position, fee_rates=fee_rates)
    capital = sum(e.quote_spent or e.executed_qty * e.average_fill_price for e in position.filled_entries)
    capital += sum(e.commission_total(position.quote_asset) for e in position.filled_entries)
    capital += sum(
        e.commission_total(asset) * rate
        for e in position.filled_entries
        for asset, rate in (fee_rates or {}).items()
    )
    total = snapshot["Total"]
    valid_rate = usdt_rate is not None and math.isfinite(usdt_rate) and usdt_rate > 0
    return {"total_usdt": total * usdt_rate if valid_rate else None,
            "realized_usdt": snapshot["Realise"] * usdt_rate if valid_rate else None,
            "unrealized_usdt": snapshot["Non realise"] * usdt_rate if valid_rate else None,
            "percent": 100 * total / capital if capital > 0 else 0,
            "complete": snapshot["Complet"] and valid_rate,
            "estimated_fee_assets": tuple(sorted(snapshot["Frais valorises"])),
            "unpriced_fee_assets": tuple(sorted(snapshot["Frais non convertis"]))}
