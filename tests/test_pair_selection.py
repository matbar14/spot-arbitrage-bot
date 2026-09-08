import unittest

from arbitrage_bot.__main__ import parse_pair


class PairSelectionTests(unittest.TestCase):
    def test_xeggex_pair(self):
        self.assertEqual(parse_pair("mexc-xeggex"), ("mexc", "xeggex"))

    def test_rejects_unknown_pair(self):
        with self.assertRaises(ValueError):
            parse_pair("mexc-binance")
