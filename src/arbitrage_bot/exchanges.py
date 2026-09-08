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
        factories = {"mexc": ccxt.mexc, "nonkyc": NonKycExchange, "xeggex": XeggexExchange}
        self.clients: dict[str, Any] = {}
        for name in exchange_ids:
            factory = factories.get(name)
            if factory is None:
                raise ExchangeConfigurationError(f"unsupported exchange adapter: {name}")
            key, secret = credentials.get(name, ("", ""))
            options = {"enableRateLimit": True, "timeout": int(timeout * 1000)}
            if key and secret:
                options.update({"apiKey": key, "secret": secret})
            self.clients[name] = factory(options)

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
