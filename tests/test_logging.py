import logging
import unittest

from arbitrage_bot.__main__ import ColorFormatter


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.formatter = ColorFormatter()
        self.formatter.enabled = True

    def _format(self, level, message, arbitrage=False):
        record = logging.LogRecord("test", level, "", 0, message, (), None)
        record.arbitrage = arbitrage
        return self.formatter.format(record)

    def test_regular_info_is_not_green(self):
        output = self._format(logging.INFO, "no actionable spread")
        self.assertNotIn("\033[", output)

    def test_arbitrage_is_green(self):
        output = self._format(logging.INFO, "ARBITRAGE", arbitrage=True)
        self.assertIn("\033[1;32m", output)

    def test_warning_remains_yellow(self):
        output = self._format(logging.WARNING, "warning")
        self.assertIn("\033[33m", output)
