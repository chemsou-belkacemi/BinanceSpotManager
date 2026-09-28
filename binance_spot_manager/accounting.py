"""Vue comptable pure : cout moyen net, frais non valorisables explicites."""

from collections import defaultdict


def accounting_snapshot(position, fee_rates=None):
    """Return PnL in the position quote asset.

    ``fee_rates`` maps a third-party fee asset (for example BNB) to one unit
    expressed in the position quote asset. Such values are estimates when the
    current ticker is used and are therefore reported separately.
    """
    fee_rates = {str(asset).upper(): float(rate) for asset, rate in (fee_rates or {}).items()
                 if rate is not None and float(rate) > 0}
    buys = [entry for entry in position.entries if entry.executed_qty > 0]
    sells = [*position.take_profits, position.stop_loss, *position.manual_exits]
    buy_fees, sell_fees = defaultdict(float), defaultdict(float)
    for sources, bucket in ((buys, buy_fees), (sells, sell_fees)):
        for item in sources:
            for fee in item.commissions:
                bucket[fee.asset] += fee.amount
    base, quote = position.base_asset, position.quote_asset
    spent = sum(e.quote_spent or e.executed_qty * e.average_fill_price for e in buys)
    net_bought = sum(e.executed_qty for e in buys) - buy_fees[base]
    third_assets = {
        asset for asset in buy_fees.keys() | sell_fees.keys()
        if asset not in {base, quote} and buy_fees[asset] + sell_fees[asset] > 0
    }
    valued = {
        asset: {
            "amount": buy_fees[asset] + sell_fees[asset],
            "quote_value": (buy_fees[asset] + sell_fees[asset]) * fee_rates[asset],
            "rate": fee_rates[asset],
        }
        for asset in third_assets if asset in fee_rates
    }
    buy_third_quote = sum(buy_fees[asset] * fee_rates[asset]
                          for asset in third_assets if asset in fee_rates)
    sell_third_quote = sum(sell_fees[asset] * fee_rates[asset]
                           for asset in third_assets if asset in fee_rates)
    cost = spent + buy_fees[quote] + buy_third_quote
    unit_cost = cost / net_bought if net_bought > 0 else 0
    consumed = sum(s.executed_qty for s in sells) + sell_fees[base]
    remaining = max(net_bought - consumed, 0)
    received = sum(s.quote_received for s in sells) - sell_fees[quote] - sell_third_quote
    realized = received - unit_cost * consumed
    unrealized = remaining * ((position.metrics.current_price or position.metrics.average_price) - unit_cost)
    unpriced = {asset: buy_fees[asset] + sell_fees[asset]
                for asset in third_assets if asset not in fee_rates}
    return {"Paire": position.symbol, "Devise": quote, "Cout unitaire frais inclus": unit_cost,
            "Realise": realized, "Non realise": unrealized, "Total": realized + unrealized,
            "Quantite restante": remaining, "Frais non convertis": unpriced,
            "Frais valorises": valued,
            "Frais externes valorises": buy_third_quote + sell_third_quote,
            "Complet": not unpriced and consumed <= net_bought + 1e-12}
