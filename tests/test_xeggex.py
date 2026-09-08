import base64
import asyncio
import unittest

from arbitrage_bot.xeggex import XeggexExchange


class XeggexTests(unittest.TestCase):
    def test_basic_auth_header(self):
        exchange = XeggexExchange({"apiKey": "key", "secret": "secret", "timeout": 1})
        self.assertEqual(exchange._headers("ignored")["Authorization"], "Basic " + base64.b64encode(b"key:secret").decode())
        self.assertIsNone(exchange._session)

    def test_uses_slash_symbol_for_orderbook(self):
        exchange = XeggexExchange({"timeout": 1})
        calls = []

        async def request(method, path, **kwargs):
            calls.append((method, path))
            return [{"price": "10", "quantity": "2"}] if "obbuys" in path else [{"price": "11", "quantity": "3"}]

        exchange._request = request
        book = asyncio.run(exchange.fetch_order_book("BTC/USDT", 1))
        self.assertEqual(calls, [("GET", "/market/obbuys"), ("GET", "/market/obsells")])
        self.assertEqual(book["bids"], [["10", "2"]])
