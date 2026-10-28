from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import asyncio
from typing import Any

import ccxt.async_support as ccxt
from .nonkyc import NonKycExchange
from .xeggex import XeggexExchange

class ExchangeConfigurationError(RuntimeError):
    pass


# Central registry: every exchange the bot can work with.
# "factory" is None when the current ccxt build lacks the adapter — the
# exchange stays listed but raises ExchangeConfigurationError on use.
# "default_fee_bps" is the conservative dry-run taker fee estimate (VIP0),
# overridable via <EXCHANGE_ID_UPPER>_FEE_BPS in the environment.
EXCHANGE_REGISTRY: dict[str, dict[str, Any]] = {
    "mexc":     {"factory": ccxt.mexc,                        "default_fee_bps": Decimal("5")},
    "nonkyc":   {"factory": NonKycExchange,                   "default_fee_bps": Decimal("20")},
    "xeggex":   {"factory": XeggexExchange,                   "default_fee_bps": Decimal("20")},
    "xt":       {"factory": getattr(ccxt, "xt", None),        "default_fee_bps": Decimal("10")},
    "lbank":    {"factory": getattr(ccxt, "lbank", None),     "default_fee_bps": Decimal("10")},
    "bingx":    {"factory": getattr(ccxt, "bingx", None),     "default_fee_bps": Decimal("10")},
    "htx":      {"factory": getattr(ccxt, "htx", None),       "default_fee_bps": Decimal("20")},
    "gate":     {"factory": getattr(ccxt, "gate", None),      "default_fee_bps": Decimal("20")},
    "bitrue":   {"factory": getattr(ccxt, "bitrue", None),    "default_fee_bps": Decimal("30")},
    "whitebit": {"factory": getattr(ccxt, "whitebit", None),  "default_fee_bps": Decimal("10")},
}


def supported_exchange_ids() -> tuple[str, ...]:
    return tuple(sorted(EXCHANGE_REGISTRY))

@dataclass(frozen=True)
class Book:
    exchange: str
    symbol: str
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    received_at: datetime

def _levels(raw: list[Any]) -> list[tuple[Decimal, Decimal]]:
    return [(Decimal(str(row[0])), Decimal(str(row[1]))) for row in raw if len(row) >= 2]

class PublicMarketData:
    def __init__(self, timeout: float = 5, credentials: dict[str, tuple[str, str]] | None = None, exchange_ids: tuple[str, ...] = ("mexc", "nonkyc")):
        credentials = credentials or {}
        self.clients: dict[str, Any] = {}
        for name in exchange_ids:
            spec = EXCHANGE_REGISTRY.get(name)
            factory = spec.get("factory") if spec else None
            if factory is None:
                raise ExchangeConfigurationError(f"unsupported exchange adapter: {name}")
            key, secret = credentials.get(name, ("", ""))
            options = {"enableRateLimit": True, "timeout": int(timeout * 1000)}
            if key and secret:
                options.update({"apiKey": key, "secret": secret})
            client = factory(options)
            client.native_ioc = name == "mexc"
            self.clients[name] = client

    async def close(self):
        await asyncio.gather(*(client.close() for client in self.clients.values()), return_exceptions=True)

    async def load_markets(self):
        await asyncio.gather(*(client.load_markets() for client in self.clients.values()))

    async def fetch(self, exchange: str, symbol: str, limit: int) -> Book:
        client = self.clients[exchange]
        market_symbol = self.market_symbol(exchange, symbol)
        if market_symbol not in client.markets:
            raise ValueError(f"{exchange}: unsupported symbol {market_symbol}")
        data = await client.fetch_order_book(market_symbol, limit)
        return Book(exchange, symbol, _levels(data.get("bids", [])), _levels(data.get("asks", [])), datetime.now(timezone.utc))

    def client(self, exchange: str):
        return self.clients[exchange]

    def common_symbols(self, exchanges: tuple[str, str], quote: str, maximum: int = 0) -> tuple[str, ...]:
        available = []
        for symbol in set(self.clients[exchanges[0]].markets) & set(self.clients[exchanges[1]].markets):
            markets = [self.clients[exchange].market(symbol) for exchange in exchanges]
            if all(market.get("spot") and market.get("active") is not False and market.get("quote") == quote for market in markets):
                available.append(symbol)
        available.sort()
        return tuple(available[:maximum] if maximum > 0 else available)

    def market_symbol(self, exchange: str, symbol: str) -> str:
        normalized = symbol.upper().replace("-", "/")
        if "/" not in normalized:
            normalized = f"{normalized[:-4]}/{normalized[-4:]}"
        if normalized not in self.clients[exchange].markets:
            raise ValueError(f"{exchange}: unsupported spot symbol {normalized}")
        market = self.clients[exchange].market(normalized)
        if not market.get("spot", False) or market.get("active") is False:
            raise ValueError(f"{exchange}: {normalized} is not an active spot market")
        return normalized
