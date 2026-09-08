from decimal import Decimal
from typing import Any


class FeeConfigurationError(RuntimeError):
    pass


async def resolve_mexc_taker_fee(client: Any, symbol: str, fallback_bps: Decimal | None, *, live: bool, auto_fetch: bool) -> Decimal:
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
                    return value
            raise FeeConfigurationError(f"MEXC returned no valid taker fee for {symbol}: {fee}")
        except Exception as exc:
            if live:
                raise FeeConfigurationError(f"cannot obtain live MEXC fee for {symbol}: {exc}") from exc
    if fallback_bps is not None:
        return fallback_bps
    raise FeeConfigurationError(f"MEXC fee is unavailable for {symbol}; set MEXC_FEE_BPS or provide API keys")
