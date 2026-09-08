import asyncio
import unittest
from decimal import Decimal

from arbitrage_bot.fees import FeeConfigurationError, resolve_mexc_taker_fee


class Client:
    apiKey = "key"
    secret = "secret"
    has = {"fetchTradingFee": True}

    async def fetch_trading_fee(self, symbol):
        return {"taker": 0.0005}


class FeeTests(unittest.TestCase):
    def test_converts_taker_percent_to_bps(self):
        fee = asyncio.run(resolve_mexc_taker_fee(Client(), "BTC/USDT", None, live=True, auto_fetch=True))
        self.assertEqual(fee, Decimal("5.0000"))

    def test_live_fails_without_fee(self):
        with self.assertRaises(FeeConfigurationError):
            asyncio.run(resolve_mexc_taker_fee(object(), "BTC/USDT", None, live=True, auto_fetch=True))
