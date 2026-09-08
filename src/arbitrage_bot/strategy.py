from dataclasses import dataclass
from decimal import Decimal
from .exchanges import Book

@dataclass(frozen=True)
class Opportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    quantity: Decimal
    buy_price: Decimal
    sell_price: Decimal
    buy_cost: Decimal
    sell_proceeds: Decimal
    net_edge_bps: Decimal
    estimated_profit: Decimal
    observed_at: object

def _fill(levels, quote_budget: Decimal):
    remaining, base, quote = quote_budget, Decimal(0), Decimal(0)
    for price, amount in levels:
        spend = min(remaining, price * amount)
        base += spend / price
        quote += spend
        remaining -= spend
        if remaining <= 0:
            break
    return base, quote, remaining

def _fill_quantity(levels, quantity: Decimal, buying: bool):
    remaining, total_quote = quantity, Decimal(0)
    for price, amount in levels:
        used = min(remaining, amount)
        total_quote += used * price
        remaining -= used
        if remaining <= 0:
            break
    return quantity - remaining, total_quote

def _protective_price(levels, level: int) -> Decimal | None:
    if not levels:
        return None
    index = min(max(level, 1) - 1, len(levels) - 1)
    return levels[index][0]


def find_opportunity(
    left: Book,
    right: Book,
    budget: Decimal,
    fees_bps: dict[str, Decimal],
    buffer_bps: Decimal,
    protective_price_level: int = 3,
):
    opportunities = find_opportunities(left, right, budget, fees_bps, buffer_bps, protective_price_level)
    return max(opportunities, key=lambda opportunity: opportunity.net_edge_bps, default=None)


def find_opportunities(
    left: Book,
    right: Book,
    budget: Decimal,
    fees_bps: dict[str, Decimal],
    buffer_bps: Decimal,
    protective_price_level: int = 3,
):
    if left.symbol != right.symbol:
        return []
    candidates = [(left, right), (right, left)]
    opportunities = []
    for buy, sell in candidates:
        buy_protected_price = _protective_price(buy.asks, protective_price_level) if buy.exchange != "mexc" else None
        sell_protected_price = _protective_price(sell.bids, protective_price_level) if sell.exchange != "mexc" else None
        if buy.exchange != "mexc" and buy_protected_price is None:
            continue
        if sell.exchange != "mexc" and sell_protected_price is None:
            continue
        buy_capacity = buy.asks[0][1] if buy.exchange != "mexc" else sum(amount for _, amount in buy.asks)
        sell_capacity = sell.bids[0][1] if sell.exchange != "mexc" else sum(amount for _, amount in sell.bids)
        affordable_capacity = (
            budget / buy_protected_price
            if buy.exchange != "mexc"
            else _fill(buy.asks, budget)[0]
        )
        quantity = min(affordable_capacity, sell_capacity, buy_capacity)
        if quantity <= 0:
            continue
        if buy.exchange != "mexc":
            bought, spent = quantity, quantity * buy_protected_price
        else:
            bought, spent = _fill_quantity(buy.asks, quantity, buying=True)
        if sell.exchange != "mexc":
            sold, proceeds = bought, bought * sell_protected_price
        else:
            sold, proceeds = _fill_quantity(sell.bids, bought, buying=False)
        if bought <= 0 or sold <= 0 or bought != sold:
            continue
        buy_fee = spent * fees_bps[buy.exchange] / Decimal(10000)
        sell_fee = proceeds * fees_bps[sell.exchange] / Decimal(10000)
        net_profit = proceeds - sell_fee - spent - buy_fee
        net = net_profit / spent * Decimal(10000) - buffer_bps
        opportunity = Opportunity(
            left.symbol, buy.exchange, sell.exchange, sold,
            buy_protected_price if buy.exchange != "mexc" else spent / sold,
            sell_protected_price if sell.exchange != "mexc" else proceeds / sold,
            spent + buy_fee, proceeds - sell_fee, net, net_profit,
            min(left.received_at, right.received_at),
        )
        opportunities.append(opportunity)
    return opportunities
