#!/usr/bin/env python3
"""Depth probe: for spread candidates, compute executable size & net profit
against real 50-level orderbooks via the bot's own adapters (read-only).

Usage from the repository root:
    python scripts/depth_probe.py
Edit CANDIDATES below (symbol, buy exchange, sell exchange) or pass nothing
to probe the default list.
"""
import asyncio
import os
import sys
from decimal import Decimal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from arbitrage_bot.exchanges import PublicMarketData  # noqa: E402

CANDIDATES = [
    # symbol, buy_ex, sell_ex
    ("SIREN/USDT", "bingx", "mexc"),
    ("AI/USDT", "mexc", "lbank"),
    ("BLUAI/USDT", "mexc", "xt"),
    ("BLUAI/USDT", "mexc", "bingx"),
    ("CAP/USDT", "lbank", "mexc"),
    ("BZZ/USDT", "xt", "mexc"),
    ("NIGHT/USDT", "nonkyc", "mexc"),
]
SPEND_TARGETS = [25.0, 50.0, 100.0, 200.0]
FEE = {"mexc": Decimal("0.0005"), "nonkyc": Decimal("0.002"), "xeggex": Decimal("0.002"),
       "xt": Decimal("0.002"), "lbank": Decimal("0.001"), "bingx": Decimal("0.001"),
       "htx": Decimal("0.002"), "gate": Decimal("0.002"), "bitrue": Decimal("0.00098"),
       "whitebit": Decimal("0.001"), "kucoin": Decimal("0.001"), "bybit": Decimal("0.001"),
       "bitget": Decimal("0.001")}
LIMIT = 50


def walk_buy(asks, spend):
    """Spend `spend` USDT into asks; return (qty, cost); cost < spend if shallow."""
    got, cost = Decimal("0"), Decimal("0")
    target = Decimal(str(spend))
    for p, q in asks:
        level_cost = p * q
        if cost + level_cost >= target:
            need = target - cost
            got += need / p
            return got, target
        got += q
        cost += level_cost
    return got, cost


def walk_sell(bids, qty):
    """Sell `qty` into bids; return (proceeds, unsold)."""
    left, proceeds = qty, Decimal("0")
    for p, q in bids:
        take = min(left, q)
        proceeds += take * p
        left -= take
        if left <= 0:
            break
    return proceeds, left


async def probe(market: PublicMarketData, symbol, buy_ex, sell_ex):
    try:
        books = await asyncio.gather(*(
            market.fetch(ex, symbol, LIMIT) for ex in (buy_ex, sell_ex)
        ))
    except Exception as exc:
        print(f"{symbol:<13} {buy_ex}->{sell_ex}: error: {exc}")
        return
    asks, bids = books[0].asks, books[1].bids
    if not asks or not bids:
        print(f"{symbol:<13} {buy_ex}->{sell_ex}: empty books")
        return
    top_ask, top_bid = asks[0][0], bids[0][0]
    gross_bps = float((top_bid / top_ask - 1) * 10000)
    parts = [f"{symbol:<13} {buy_ex}->{sell_ex:<7} top {float(top_ask):.6g}/{float(top_bid):.6g} gross {gross_bps:7.1f}bps"]
    for spend in SPEND_TARGETS:
        got, cost = walk_buy(asks, spend)
        if cost < Decimal(str(spend)) * Decimal("0.999"):
            parts.append(f"${spend}: buy-book only ${float(cost):.2f}")
            continue
        proceeds, unsold = walk_sell(bids, got)
        if unsold > got * Decimal("0.001"):
            parts.append(f"${spend}: sell-book absorbs only {float(got - unsold):.6g}")
            continue
        buy_fee = cost * FEE[buy_ex]
        sell_fee = proceeds * FEE[sell_ex]
        profit = proceeds - sell_fee - cost - buy_fee
        edge_bps = float(profit / cost * 10000)
        parts.append(f"${spend}: edge {edge_bps:7.1f}bps profit {float(profit):+9.4f}$")
    print("  |  ".join(parts), flush=True)


async def main():
    exchange_ids = tuple(dict.fromkeys(ex for _, b, s in CANDIDATES for ex in (b, s)))
    market = PublicMarketData(10.0, None, exchange_ids)
    try:
        await market.load_markets()
        for sym, b, s in CANDIDATES:
            await probe(market, sym, b, s)
            await asyncio.sleep(0.2)
    finally:
        await market.close()


if __name__ == "__main__":
    asyncio.run(main())
