from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import asyncio
import logging
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
    "xt":       {"factory": getattr(ccxt, "xt", None),        "default_fee_bps": Decimal("20")},
    "lbank":    {"factory": getattr(ccxt, "lbank", None),     "default_fee_bps": Decimal("10")},
    "bingx":    {"factory": getattr(ccxt, "bingx", None),     "default_fee_bps": Decimal("10")},
    "htx":      {"factory": getattr(ccxt, "htx", None),       "default_fee_bps": Decimal("20"), "book_limits": (5, 10, 20, 150)},
    "gate":     {"factory": getattr(ccxt, "gate", None),      "default_fee_bps": Decimal("20")},
    "bitrue":   {"factory": getattr(ccxt, "bitrue", None),    "default_fee_bps": Decimal("9.8")},
    "whitebit": {"factory": getattr(ccxt, "whitebit", None),  "default_fee_bps": Decimal("10")},
    "kucoin":   {"factory": getattr(ccxt, "kucoin", None),    "default_fee_bps": Decimal("10"), "book_limits": (20, 100)},
    "bybit":    {"factory": getattr(ccxt, "bybit", None),     "default_fee_bps": Decimal("10")},
    "bitget":   {"factory": getattr(ccxt, "bitget", None),    "default_fee_bps": Decimal("10")},  # per-market: majors 20, alts 10
}


def supported_exchange_ids() -> tuple[str, ...]:
    return tuple(sorted(EXCHANGE_REGISTRY))

def usable_exchange_ids() -> tuple[str, ...]:
    """Registered exchanges with an actually available adapter."""
    return tuple(sorted(
        exchange_id for exchange_id, spec in EXCHANGE_REGISTRY.items()
        if spec.get("factory") is not None
    ))

def _coerce_book_limit(spec: dict[str, Any], limit: int) -> int:
    """Clamp the requested depth size to what the venue actually accepts.

    Some APIs only serve fixed depth sizes (e.g. KuCoin: 20 or 100 levels).
    Exchanges without a "book_limits" entry accept any size.
    """
    allowed = spec.get("book_limits")
    if not allowed or limit in allowed:
        return limit
    bigger = [size for size in allowed if size >= limit]
    return min(bigger) if bigger else max(allowed)

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
        """Load markets for every client without letting one venue kill startup.

        A single slow endpoint (e.g. lbank withdrawConfigs.do timing out)
        must not abort the whole session: every failing venue gets one
        relaxed-timeout retry, then is dropped with a warning. Raises
        ExchangeConfigurationError only when fewer than two venues survive —
        an arbitrage session on a single venue is meaningless anyway.
        """
        results = await asyncio.gather(
            *(self._load_one(name, client) for name, client in list(self.clients.items()))
        )
        failed = {name for name, ok in results if not ok}
        if failed:
            logging.warning("load_markets: повторная попытка для %s", ", ".join(sorted(failed)))
            retry = await asyncio.gather(
                *(self._load_one(name, self.clients[name]) for name in sorted(failed) if name in self.clients)
            )
            failed = {name for name, ok in retry if not ok}
        for name in sorted(failed):
            client = self.clients.pop(name, None)
            if client is not None:
                logging.warning("load_markets: %s исключён из сессии — рынки не загрузились", name)
                try:
                    await client.close()
                except Exception:
                    pass
        if len(self.clients) < 2:
            raise ExchangeConfigurationError(
                "рынки загрузились менее чем на двух биржах (упали: "
                + ", ".join(sorted(failed)) + ") — проверьте сеть или повторите позже"
            )

    async def _load_one(self, name: str, client: Any) -> tuple[str, bool]:
        """One load_markets attempt with relaxed timeout and no currencies meta."""
        old_timeout = getattr(client, "timeout", None)
        has_map = getattr(client, "has", None)
        old_flag = has_map.get("fetchCurrencies") if isinstance(has_map, dict) else None
        try:
            # Market loading hits heavy meta endpoints that are slower than
            # order books; give the attempt extra headroom. ccxt timeouts are
            # milliseconds, local adapters use seconds — hence the 1000 split.
            if isinstance(old_timeout, (int, float)) and old_timeout > 0:
                client.timeout = max(old_timeout, 30_000 if old_timeout >= 1000 else 30)
            if old_flag is True and isinstance(has_map, dict):
                # Deposit/withdraw chain info is irrelevant for spot spreads
                # and the call is notoriously slow on some venues (lbank).
                has_map["fetchCurrencies"] = False
            await client.load_markets()
            return name, True
        except Exception as exc:  # noqa: BLE001 — resilience is the whole point
            logging.warning("load_markets: %s — %s: %s", name, type(exc).__name__, exc)
            return name, False
        finally:
            if isinstance(old_timeout, (int, float)) and old_timeout > 0:
                client.timeout = old_timeout
            if old_flag is not None and isinstance(has_map, dict):
                has_map["fetchCurrencies"] = old_flag

    async def fetch(self, exchange: str, symbol: str, limit: int) -> Book:
        client = self.clients[exchange]
        market_symbol = self.market_symbol(exchange, symbol)
        if market_symbol not in client.markets:
            raise ValueError(f"{exchange}: unsupported symbol {market_symbol}")
        data = await client.fetch_order_book(market_symbol, _coerce_book_limit(EXCHANGE_REGISTRY.get(exchange, {}), limit))
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

    def union_symbols(self, quote: str, minimum_venues: int = 2, maximum: int = 0) -> dict[str, tuple[str, ...]]:
        """Symbols quoted in `quote` actively listed on at least `minimum_venues` venues.

        Returns an ordered {symbol: (venue, ...)} map — the universe for the
        web scanner's all-linkages mode.
        """
        holder: dict[str, list[str]] = {}
        for exchange_id, client in self.clients.items():
            for symbol, market in client.markets.items():
                if market.get("spot") and market.get("active") is not False and market.get("quote") == quote:
                    holder.setdefault(symbol, []).append(exchange_id)
        usable = {
            symbol: tuple(venues)
            for symbol, venues in sorted(holder.items())
            if len(venues) >= minimum_venues
        }
        if maximum > 0:
            usable = dict(list(usable.items())[:maximum])
        return usable

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
