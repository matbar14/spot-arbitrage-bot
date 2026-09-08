import unittest

from arbitrage_bot.exchanges import PublicMarketData


class FakeClient:
    def __init__(self, markets):
        self.markets = markets

    def market(self, symbol):
        return self.markets[symbol]


class CommonSymbolsTests(unittest.TestCase):
    def test_filters_spot_active_quote_and_sorts(self):
        gateway = object.__new__(PublicMarketData)
        gateway.clients = {
            "mexc": FakeClient({
                "BTC/USDT": {"spot": True, "active": True, "quote": "USDT"},
                "ETH/USDT": {"spot": True, "active": True, "quote": "USDT"},
                "BAD/USDT": {"spot": False, "active": True, "quote": "USDT"},
            }),
            "xeggex": FakeClient({
                "BTC/USDT": {"spot": True, "active": True, "quote": "USDT"},
                "ETH/USDT": {"spot": True, "active": False, "quote": "USDT"},
                "BAD/USDT": {"spot": True, "active": True, "quote": "USDT"},
            }),
        }
        self.assertEqual(gateway.common_symbols(("mexc", "xeggex"), "USDT"), ("BTC/USDT",))
