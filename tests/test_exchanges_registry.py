import os
import unittest
from decimal import Decimal

from arbitrage_bot.__main__ import parse_pair
from arbitrage_bot.config import Settings
from arbitrage_bot.exchanges import EXCHANGE_REGISTRY
from arbitrage_bot.fees import static_taker_fee_bps


class ParsePairTests(unittest.TestCase):
    def test_legacy_pairs_still_work(self):
        self.assertEqual(parse_pair("mexc-nonkyc"), ("mexc", "nonkyc"))
        self.assertEqual(parse_pair("mexc-xeggex"), ("mexc", "xeggex"))
        self.assertEqual(parse_pair("nonkyc-xeggex"), ("nonkyc", "xeggex"))

    def test_new_registry_pairs(self):
        self.assertEqual(parse_pair("xt-whitebit"), ("xt", "whitebit"))
        self.assertEqual(parse_pair("mexc-xt"), ("mexc", "xt"))
        self.assertEqual(parse_pair("mexc↔gate"), ("mexc", "gate"))

    def test_invalid_pairs_rejected(self):
        for value in ("bogus-pair", "mexc-mexc", "mexc", "mexc-xt-gate", ""):
            with self.assertRaises(ValueError):
                parse_pair(value)


class RegistryTests(unittest.TestCase):
    def test_expected_exchanges_registered(self):
        for exchange_id in ("mexc", "nonkyc", "xeggex", "xt", "lbank", "bingx",
                            "htx", "gate", "bitrue", "whitebit"):
            self.assertIn(exchange_id, EXCHANGE_REGISTRY)
            self.assertIn("default_fee_bps", EXCHANGE_REGISTRY[exchange_id])

    def test_legacy_fee_fields_take_precedence(self):
        settings = Settings()
        self.assertEqual(static_taker_fee_bps(settings, "nonkyc"), Decimal("20"))
        self.assertEqual(static_taker_fee_bps(settings, "xeggex"), Decimal("20"))
        self.assertEqual(static_taker_fee_bps(settings, "xt"), Decimal("10"))

    def test_env_fee_override(self):
        os.environ["XT_FEE_BPS"] = "7.5"
        try:
            settings = Settings()
            self.assertEqual(static_taker_fee_bps(settings, "xt"), Decimal("7.5"))
        finally:
            del os.environ["XT_FEE_BPS"]

    def test_credentials_pattern(self):
        os.environ["XT_API_KEY"] = "k1"
        os.environ["XT_SECRET"] = "s1"
        try:
            credentials = Settings().exchange_credentials()
            self.assertEqual(credentials["xt"], ("k1", "s1"))
            self.assertIn("mexc", credentials)
            self.assertIn("whitebit", credentials)
        finally:
            del os.environ["XT_API_KEY"]
            del os.environ["XT_SECRET"]


if __name__ == "__main__":
    unittest.main()
