import unittest
from decimal import Decimal

from arbitrage_bot.trade_gate import NotionalTracker, TradeGate


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

    def test_continuation_requires_active_spread_and_cooldown(self):
        gate = TradeGate(30, Decimal("10"))
        gate.mark_completed("XRPUSDT", now=100)
        self.assertFalse(gate.can_continue("XRPUSDT", Decimal("100"), now=120))
        self.assertTrue(gate.can_continue("XRPUSDT", Decimal("100"), now=130))
        gate.observe("XRPUSDT", Decimal("5"))
        self.assertFalse(gate.can_continue("XRPUSDT", Decimal("100"), now=130))

    def test_notional_tracker_limits_series_and_resets_after_rearm(self):
        tracker = NotionalTracker(Decimal("100"), Decimal("100"), Decimal("10"))
        tracker.record("XRPUSDT", Decimal("10"))
        tracker.record("XRPUSDT", Decimal("15"))
        self.assertEqual(tracker.used("XRPUSDT"), Decimal("25"))
        self.assertEqual(tracker.remaining("XRPUSDT"), Decimal("75"))
        tracker.observe("XRPUSDT", Decimal("5"))
        self.assertEqual(tracker.used("XRPUSDT"), Decimal("0"))
        self.assertEqual(tracker.remaining("XRPUSDT"), Decimal("100"))

    def test_notional_tracker_applies_global_limit_across_symbols(self):
        tracker = NotionalTracker(Decimal("100"), Decimal("100"), Decimal("10"))
        tracker.record("XRPUSDT", Decimal("60"))
        self.assertEqual(tracker.remaining("BTCUSDT"), Decimal("40"))
        tracker.record("BTCUSDT", Decimal("40"))
        self.assertEqual(tracker.total_used(), Decimal("100"))
        self.assertEqual(tracker.remaining("ETHUSDT"), Decimal("0"))
