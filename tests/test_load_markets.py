"""Resilient market loading: one flaky venue must not kill the session.

Regression tests for the lbank RequestTimeout failure: a single exchange
failing its load_markets (slow meta endpoint) used to abort the whole
configure() call via asyncio.gather. Now failed venues are retried once
with a relaxed timeout and dropped if they stay dead.
"""

import asyncio
import unittest

from arbitrage_bot.exchanges import ExchangeConfigurationError, PublicMarketData


class FakeClient:
    id = "fake"
    timeout = 5000  # ccxt-style milliseconds
    has = {"fetchCurrencies": True}

    def __init__(self, failures: int = 0):
        self.failures = failures
        self.markets = {}
        self.closed = False

    async def load_markets(self):
        if self.failures > 0:
            self.failures -= 1
            raise TimeoutError("slow meta endpoint")

    async def close(self):
        self.closed = True


def make_market(failures: dict[str, int]) -> PublicMarketData:
    market = PublicMarketData.__new__(PublicMarketData)
    market.clients = {name: FakeClient(count) for name, count in failures.items()}
    return market


class LoadMarketsTests(unittest.TestCase):
    def test_flaky_venue_survives_after_retry(self):
        market = make_market({"a": 1, "b": 0})
        asyncio.run(market.load_markets())
        self.assertEqual(set(market.clients), {"a", "b"})
        self.assertEqual(market.clients["a"].failures, 0)

    def test_dead_venue_is_dropped_not_fatal(self):
        market = make_market({"a": 0, "b": 99, "c": 0})
        asyncio.run(market.load_markets())
        self.assertEqual(set(market.clients), {"a", "c"})
        self.assertEqual(market.clients["a"].failures, 0)

    def test_dead_venue_client_is_closed(self):
        market = make_market({"a": 0, "b": 99, "c": 0})
        dead = market.clients["b"]
        asyncio.run(market.load_markets())
        self.assertTrue(dead.closed)

    def test_two_dead_venues_still_fatal(self):
        market = make_market({"a": 99, "b": 99, "c": 0})
        with self.assertRaises(ExchangeConfigurationError):
            asyncio.run(market.load_markets())

    def test_fewer_than_two_venues_raises(self):
        market = make_market({"a": 99, "b": 99})
        with self.assertRaises(ExchangeConfigurationError):
            asyncio.run(market.load_markets())

    def test_timeout_and_flags_restored_after_load(self):
        market = make_market({"a": 0, "b": 0})
        asyncio.run(market.load_markets())
        for client in market.clients.values():
            self.assertEqual(client.timeout, 5000)
            self.assertTrue(client.has["fetchCurrencies"])

    def test_relaxed_timeout_applied_during_load(self):
        seen = {}

        class Probe(FakeClient):
            async def load_markets(self):
                seen["timeout"] = self.timeout
                seen["currencies"] = self.has["fetchCurrencies"]

        market = make_market({"a": 0, "b": 0})
        market.clients["a"] = Probe(0)
        asyncio.run(market.load_markets())
        self.assertEqual(seen["timeout"], 30_000)  # bumped for the heavy load
        self.assertFalse(seen["currencies"])  # currencies meta skipped


if __name__ == "__main__":
    unittest.main()
