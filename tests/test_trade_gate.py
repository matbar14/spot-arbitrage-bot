import unittest
from decimal import Decimal

from arbitrage_bot.trade_gate import TradeGate


class TradeGateTests(unittest.TestCase):
    def test_cooldown_and_spread_reset(self):
        gate = TradeGate(30, Decimal("10"))
        self.assertTrue(gate.can_enter("XRPUSDT", Decimal("40"), now=100))
        gate.mark_completed("XRPUSDT", now=100)
        self.assertFalse(gate.can_enter("XRPUSDT", Decimal("100"), now=110))
        self.assertEqual(gate.remaining_cooldown("XRPUSDT", now=110), 20)
        self.assertFalse(gate.can_enter("XRPUSDT", Decimal("100"), now=140))
        gate.observe("XRPUSDT", Decimal("5"))
        self.assertTrue(gate.can_enter("XRPUSDT", Decimal("100"), now=140))
