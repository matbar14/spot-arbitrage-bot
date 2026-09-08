from datetime import datetime, timezone
from decimal import Decimal
import unittest

from arbitrage_bot.exchanges import Book
from arbitrage_bot.strategy import find_opportunities, find_opportunity


def book(exchange, bids, asks):
    return Book(exchange, "BTCUSDT", bids, asks, datetime.now(timezone.utc))


class StrategyTests(unittest.TestCase):
    def test_finds_net_profitable_direction(self):
        mexc = book("mexc", [(Decimal("101"), Decimal("2"))], [(Decimal("100"), Decimal("2"))])
        nonkyc = book("nonkyc", [(Decimal("99"), Decimal("2"))], [(Decimal("98"), Decimal("2"))])
        result = find_opportunity(mexc, nonkyc, Decimal("100"), {"mexc": Decimal("10"), "nonkyc": Decimal("20")}, Decimal("0"))
        self.assertIsNotNone(result)
        self.assertEqual(result.buy_exchange, "nonkyc")
        self.assertEqual(result.sell_exchange, "mexc")
        self.assertGreater(result.estimated_profit, 0)

    def test_rejects_insufficient_book_depth(self):
        mexc = book("mexc", [(Decimal("101"), Decimal("0.5"))], [(Decimal("100"), Decimal("0.5"))])
        nonkyc = book("nonkyc", [(Decimal("99"), Decimal("0.5"))], [(Decimal("98"), Decimal("0.5"))])
        result = find_opportunity(mexc, nonkyc, Decimal("100"), {"mexc": Decimal("0"), "nonkyc": Decimal("0")}, Decimal("0"))
        self.assertIsNotNone(result)
        self.assertEqual(result.quantity, Decimal("0.5"))

    def test_evaluates_both_directions(self):
        mexc = book("mexc", [(Decimal("101"), Decimal("2"))], [(Decimal("100"), Decimal("2"))])
        nonkyc = book("nonkyc", [(Decimal("99"), Decimal("2"))], [(Decimal("98"), Decimal("2"))])
        results = find_opportunities(mexc, nonkyc, Decimal("100"), {"mexc": Decimal("0"), "nonkyc": Decimal("0")}, Decimal("0"))
        self.assertEqual({(result.buy_exchange, result.sell_exchange) for result in results}, {("mexc", "nonkyc"), ("nonkyc", "mexc")})
        self.assertEqual(find_opportunity(mexc, nonkyc, Decimal("100"), {"mexc": Decimal("0"), "nonkyc": Decimal("0")}, Decimal("0")).buy_exchange, "nonkyc")

    def test_non_native_leg_uses_protective_level_and_first_level_capacity(self):
        mexc = book("mexc", [(Decimal("110"), Decimal("10"))], [(Decimal("100"), Decimal("10"))])
        nonkyc = book(
            "nonkyc",
            [(Decimal("109"), Decimal("2")), (Decimal("108"), Decimal("2")), (Decimal("107"), Decimal("2"))],
            [(Decimal("101"), Decimal("2")), (Decimal("102"), Decimal("2")), (Decimal("103"), Decimal("2"))],
        )
        results = find_opportunities(
            mexc, nonkyc, Decimal("1_000"), {"mexc": Decimal("0"), "nonkyc": Decimal("0")}, Decimal("0")
        )
        result = next(result for result in results if result.buy_exchange == "nonkyc")
        self.assertEqual(result.buy_price, Decimal("103"))
        self.assertEqual(result.quantity, Decimal("2"))
        self.assertEqual(result.sell_price, Decimal("110"))
        self.assertEqual(result.net_edge_bps, Decimal("679.6116504854368932038834951"))

    def test_mexc_leg_keeps_vwap_pricing(self):
        mexc = book(
            "mexc",
            [(Decimal("110"), Decimal("1")), (Decimal("109"), Decimal("1"))],
            [(Decimal("100"), Decimal("1")), (Decimal("101"), Decimal("1"))],
        )
        nonkyc = book(
            "nonkyc",
            [(Decimal("105"), Decimal("2")), (Decimal("104"), Decimal("2")), (Decimal("103"), Decimal("2"))],
            [(Decimal("111"), Decimal("1")), (Decimal("112"), Decimal("1")), (Decimal("113"), Decimal("1"))],
        )
        result = find_opportunity(
            mexc, nonkyc, Decimal("201"), {"mexc": Decimal("0"), "nonkyc": Decimal("0")}, Decimal("0")
        )
        self.assertEqual(result.buy_exchange, "mexc")
        self.assertEqual(result.quantity, Decimal("2"))
        self.assertEqual(result.buy_price, Decimal("100.5"))
        self.assertEqual(result.sell_price, Decimal("103"))
