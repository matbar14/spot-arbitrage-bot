import unittest
from decimal import Decimal

from arbitrage_bot.nonkyc import NonKycExchange


class NonKycAdapterTests(unittest.TestCase):
    def test_precisions_and_order_status_mapping(self):
        exchange = NonKycExchange({"timeout": 1})
        exchange.markets = {"BTC/USDT": {"precision": {"amount": 4, "price": 2}, "limits": {}}}
        try:
            self.assertEqual(exchange.amount_to_precision("BTC/USDT", 1.23459), "1.2345")
            self.assertEqual(exchange.price_to_precision("BTC/USDT", 10.129), "10.12")
            order = exchange._parse_order({"id": "1", "status": "filled", "executedQuantity": "1.2"}, "BTC/USDT")
            self.assertEqual(order["status"], "closed")
            self.assertEqual(Decimal(order["filled"]), Decimal("1.2"))
        finally:
            self.assertIsNone(exchange._session)
