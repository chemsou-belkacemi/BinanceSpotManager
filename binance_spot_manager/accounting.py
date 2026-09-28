"""Vue comptable pure : cout moyen net, frais non valorisables explicites."""

from collections import defaultdict


def accounting_snapshot(position):
    buys = [entry for entry in position.entries if entry.executed_qty > 0]
    sells = [*position.take_profits, position.stop_loss]
    buy_fees, sell_fees = defaultdict(float), defaultdict(float)
    for sources, bucket in ((buys, buy_fees), (sells, sell_fees)):
        for item in sources:
            for fee in item.commissions:
                bucket[fee.asset] += fee.amount
    base, quote = position.base_asset, position.quote_asset
    spent = sum(e.quote_spent or e.executed_qty * e.average_fill_price for e in buys)
    net_bought = sum(e.executed_qty for e in buys) - buy_fees[base]
    cost = spent + buy_fees[quote]
    unit_cost = cost / net_bought if net_bought > 0 else 0
    consumed = sum(s.executed_qty for s in sells) + sell_fees[base]
    remaining = max(net_bought - consumed, 0)
    received = sum(s.quote_received for s in sells) - sell_fees[quote]
    realized = received - unit_cost * consumed
    unrealized = remaining * ((position.metrics.current_price or position.metrics.average_price) - unit_cost)
    unpriced = {asset: buy_fees[asset] + sell_fees[asset]
                for asset in buy_fees.keys() | sell_fees.keys()
                if asset not in {base, quote} and buy_fees[asset] + sell_fees[asset] > 0}
    return {"Paire": position.symbol, "Devise": quote, "Cout unitaire frais inclus": unit_cost,
            "Realise": realized, "Non realise": unrealized, "Total": realized + unrealized,
            "Quantite restante": remaining, "Frais non convertis": unpriced,
            "Complet": not unpriced and consumed <= net_bought + 1e-12}
