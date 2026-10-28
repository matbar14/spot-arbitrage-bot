import asyncio
import unittest
from decimal import Decimal

from arbitrage_bot.fees import (
    DEFAULT_MEXC_TAKER_BPS,
    FeeConfigurationError,
    resolve_mexc_taker_fee,
)


class Client:
    apiKey = "key"
    secret = "secret"
    has = {"fetchTradingFee": True}

    def __init__(self, taker=0.0005):
        self.calls = 0
        self.taker = taker

    async def fetch_trading_fee(self, symbol):
        self.calls += 1
        return {"taker": self.taker}


class FailingClient(Client):
    def __init__(self):
        super().__init__()
        self.error = None

    async def fetch_trading_fee(self, symbol):
        self.calls += 1
        raise RuntimeError("rate limit exceeded")


class FeeTests(unittest.TestCase):
    def test_converts_taker_percent_to_bps(self):
        fee = asyncio.run(resolve_mexc_taker_fee(Client(), "BTC/USDT", None, live=True, auto_fetch=True))
        self.assertEqual(fee, Decimal("5.0000"))

    def test_live_fails_without_fee(self):
        with self.assertRaises(FeeConfigurationError):
            asyncio.run(resolve_mexc_taker_fee(object(), "BTC/USDT", None, live=True, auto_fetch=True))

    def test_dry_run_defaults_when_fee_unavailable(self):
        fee = asyncio.run(resolve_mexc_taker_fee(object(), "BTC/USDT", None, live=False, auto_fetch=True))
        self.assertEqual(fee, DEFAULT_MEXC_TAKER_BPS)

    def test_fee_fetched_once_and_cached_per_client(self):
        client = Client()
        first = asyncio.run(resolve_mexc_taker_fee(client, "BTC/USDT", None, live=True, auto_fetch=True))
        second = asyncio.run(resolve_mexc_taker_fee(client, "ETH/USDT", None, live=True, auto_fetch=True))
        self.assertEqual(first, second)
        self.assertEqual(client.calls, 1)

    def test_fallback_used_when_lookup_fails(self):
        client = FailingClient()
        fee = asyncio.run(resolve_mexc_taker_fee(client, "BTC/USDT", Decimal("7"), live=False, auto_fetch=True))
        self.assertEqual(fee, Decimal("7"))
        self.assertEqual(client.calls, 1)

    def test_live_fails_even_when_lookup_fails_with_no_fallback(self):
        with self.assertRaises(FeeConfigurationError):
            asyncio.run(resolve_mexc_taker_fee(FailingClient(), "BTC/USDT", None, live=True, auto_fetch=True))

    def test_live_uses_fallback_when_keys_missing(self):
        fee = asyncio.run(resolve_mexc_taker_fee(object(), "BTC/USDT", Decimal("6"), live=True, auto_fetch=True))
        self.assertEqual(fee, Decimal("6"))


if __name__ == "__main__":
    unittest.main()
