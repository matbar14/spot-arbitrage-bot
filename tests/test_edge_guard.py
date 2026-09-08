import unittest
from decimal import Decimal

from arbitrage_bot.strategy import Opportunity


class EdgeGuardTests(unittest.TestCase):
    def test_extreme_edge_is_above_default_safety_cap(self):
        opportunity = Opportunity("TROLL/USDT", "nonkyc", "xeggex", Decimal("1"), Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2"), Decimal("1631428.51"), Decimal("1"), None)
        self.assertGreater(opportunity.net_edge_bps, Decimal("5000"))
