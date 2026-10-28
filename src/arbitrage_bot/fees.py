import logging
import weakref
from decimal import Decimal
from typing import Any

# Standard MEXC spot taker commission (takerCommission=0.0005 -> 5 bps).
# Used only in dry-run when the real fee cannot be obtained; live mode stays strict.
DEFAULT_MEXC_TAKER_BPS = Decimal("5")

# MEXC returns an account-wide taker fee, identical for every symbol, so one
# successful lookup is enough. Cache per client instance to avoid hammering the
# authenticated endpoint once per symbol at startup (SYMBOLS=ALL -> ~100 calls).
_fee_cache: "weakref.WeakKeyDictionary[Any, Decimal]" = weakref.WeakKeyDictionary()


class FeeConfigurationError(RuntimeError):
    pass


def _cached_taker_bps(client: Any) -> Decimal | None:
    try:
        return _fee_cache.get(client)
    except TypeError:
        return None


def _store_taker_bps(client: Any, value: Decimal) -> None:
    try:
        _fee_cache[client] = value
    except TypeError:
        pass


async def resolve_mexc_taker_fee(client: Any, symbol: str, fallback_bps: Decimal | None, *, live: bool, auto_fetch: bool) -> Decimal:
    cached = _cached_taker_bps(client)
    if cached is not None:
        return cached
    has = getattr(client, "has", {})
    api_key = getattr(client, "apiKey", "")
    secret = getattr(client, "secret", "")
    if auto_fetch and has.get("fetchTradingFee") and api_key and secret:
        try:
            fee = await client.fetch_trading_fee(symbol)
            taker = fee.get("taker")
            if taker is not None:
                value = Decimal(str(taker)) * Decimal(10000)
                if value >= 0:
                    _store_taker_bps(client, value)
                    return value
            raise FeeConfigurationError(f"MEXC returned no valid taker fee for {symbol}: {fee}")
        except Exception as exc:
            if live:
                raise FeeConfigurationError(f"cannot obtain live MEXC fee for {symbol}: {exc}") from exc
    if fallback_bps is not None:
        _store_taker_bps(client, fallback_bps)
        return fallback_bps
    if live:
        raise FeeConfigurationError(f"MEXC fee is unavailable for {symbol}; set MEXC_FEE_BPS or provide API keys")
    logging.warning(
        "MEXC fee unavailable for %s; using default %.2f bps taker estimate (dry-run only)",
        symbol, DEFAULT_MEXC_TAKER_BPS,
    )
    _store_taker_bps(client, DEFAULT_MEXC_TAKER_BPS)
    return DEFAULT_MEXC_TAKER_BPS
