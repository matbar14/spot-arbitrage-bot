import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aiohttp.test_utils import TestClient, TestServer

from arbitrage_bot.config import Settings
from arbitrage_bot.exchanges import Book
from arbitrage_bot.strategy import Opportunity
from arbitrage_bot.web import Session, create_app, direction_breakdown, row_from_opportunity


def make_opportunity(buy: str, sell: str, edge: Decimal) -> Opportunity:
    return Opportunity(
        symbol="BTC/USDT", buy_exchange=buy, sell_exchange=sell,
        quantity=Decimal("2"), buy_price=Decimal("100"), sell_price=Decimal("101"),
        buy_cost=Decimal("200.5"), sell_proceeds=Decimal("201.8"),
        net_edge_bps=edge, estimated_profit=Decimal("1.3"),
        observed_at=datetime.now(timezone.utc),
    )


class RowTests(unittest.TestCase):
    def test_row_levels_and_rounding(self):
        row = row_from_opportunity(
            make_opportunity("mexc", "nonkyc", Decimal("40.123")),
            best=True, min_edge_bps=35, book_age_s=0.24,
        )
        self.assertEqual(row["level"], "signal")
        self.assertEqual(row["net_edge_bps"], 40.12)
        self.assertTrue(row["best"])

    def test_row_watch_level_below_threshold(self):
        row = row_from_opportunity(
            make_opportunity("mexc", "nonkyc", Decimal("1")),
            best=False, min_edge_bps=35, book_age_s=0.2,
        )
        self.assertEqual(row["level"], "watch")

    def test_row_negative_level(self):
        row = row_from_opportunity(
            make_opportunity("mexc", "nonkyc", Decimal("-5")),
            best=False, min_edge_bps=35, book_age_s=0.2,
        )
        self.assertEqual(row["level"], "negative")

    def test_direction_breakdown_fields(self):
        breakdown = direction_breakdown(
            make_opportunity("nonkyc", "mexc", Decimal("55.5")), book_age_s=0.4,
        )
        self.assertEqual(breakdown["buy"], "nonkyc")
        self.assertEqual(breakdown["sell"], "mexc")
        self.assertEqual(breakdown["quantity"], 2.0)
        self.assertEqual(breakdown["net_edge_bps"], 55.5)


class SessionTests(unittest.TestCase):
    def _make_session(self) -> Session:
        session = Session.__new__(Session)
        session.settings = Settings()
        session.exchange_ids = ("mexc", "nonkyc")
        session.all_mode = False
        session.poll_interval = 5
        session.max_symbols = 0
        session.market = None
        session.active_symbols = ("BTC/USDT",)
        session.symbol_venues = {"BTC/USDT": ("mexc", "nonkyc")}
        session.fees_by_symbol = {"BTC/USDT": {"mexc": Decimal("5"), "nonkyc": Decimal("20")}}
        session.task = None
        session.rows = []
        session.cycles = 3
        session.last_error = None
        session.started_at = 0.0
        session._lock = asyncio.Lock()
        return session

    def test_session_endpoints_with_fake_session(self):
        session = self._make_session()

        async def run():
            client = TestClient(TestServer(create_app(session)))
            await client.start_server()
            try:
                status = await client.get("/api/session")
                self.assertEqual(status.status, 200)
                payload = await status.json()
                self.assertEqual(payload["pair"], "mexc-nonkyc")
                self.assertEqual(payload["symbols_count"], 1)
                self.assertFalse(payload["running"])
                self.assertIn("min_net_edge_bps", payload["settings"])

                snapshot = await client.get("/api/snapshot")
                self.assertEqual(snapshot.status, 200)
                snap = await snapshot.json()
                self.assertEqual(snap["cycles"], 3)
                self.assertEqual(snap["rows"], [])

                stopped = await client.post("/api/scan/stop")
                self.assertEqual(stopped.status, 200)
                self.assertFalse((await stopped.json())["running"])
            finally:
                await client.close()

        asyncio.run(run())

    def test_invalid_pair_returns_400(self):
        session = self._make_session()

        async def run():
            client = TestClient(TestServer(create_app(session)))
            await client.start_server()
            try:
                response = await client.post("/api/session", json={"pair": "bogus-pair", "start": False})
                self.assertEqual(response.status, 400)
            finally:
                await client.close()

        asyncio.run(run())

    def test_snapshot_min_edge_filter_and_cap_fields(self):
        session = self._make_session()
        session.rows = [
            {"symbol": "A/USDT", "buy": "mexc", "sell": "nonkyc", "net_edge_bps": 12.5, "level": "watch"},
            {"symbol": "B/USDT", "buy": "mexc", "sell": "nonkyc", "net_edge_bps": -3.0, "level": "negative"},
            {"symbol": "C/USDT", "buy": "nonkyc", "sell": "mexc", "net_edge_bps": 0.0, "level": "watch"},
        ]

        async def run():
            client = TestClient(TestServer(create_app(session)))
            await client.start_server()
            try:
                unfiltered = await (await client.get("/api/snapshot")).json()
                self.assertEqual(unfiltered["rows_total"], 3)
                self.assertEqual(unfiltered["rows_shown"], 3)

                filtered = await (await client.get("/api/snapshot?min_edge=0")).json()
                self.assertEqual(filtered["rows_total"], 3)
                self.assertEqual(filtered["rows_shown"], 2)
                self.assertTrue(all(row["net_edge_bps"] >= 0 for row in filtered["rows"]))

                strict = await (await client.get("/api/snapshot?min_edge=1")).json()
                self.assertEqual(strict["rows_shown"], 1)
            finally:
                await client.close()

        asyncio.run(run())

    def test_all_mode_session_init(self):
        from arbitrage_bot.exchanges import usable_exchange_ids
        session = Session(Settings(**{**Settings().__dict__, "pair": "all"}))
        self.assertTrue(session.all_mode)
        self.assertEqual(session.pair_name, "all")
        self.assertEqual(session.exchange_ids, usable_exchange_ids())
        self.assertGreaterEqual(len(session.exchange_ids), 10)

    def test_parse_pair_rejects_all_for_console(self):
        from arbitrage_bot.__main__ import parse_pair
        with self.assertRaises(ValueError):
            parse_pair("all")

    def test_union_symbols_fake_clients(self):
        from arbitrage_bot.exchanges import PublicMarketData

        class FakeClient:
            def __init__(self, markets):
                self.markets = markets

        market = PublicMarketData.__new__(PublicMarketData)
        market.clients = {
            "aaa": FakeClient({
                "BTC/USDT": {"spot": True, "quote": "USDT"},
                "ETH/USDT": {"spot": True, "quote": "USDT"},
                "BTC/USDC": {"spot": True, "quote": "USDC"},
            }),
            "bbb": FakeClient({
                "BTC/USDT": {"spot": True, "quote": "USDT"},
                "SOL/USDT": {"spot": True, "active": False, "quote": "USDT"},
            }),
            "ccc": FakeClient({
                "BTC/USDT": {"spot": True, "active": True, "quote": "USDT"},
                "ETH/USDT": {"spot": False, "quote": "USDT"},
            }),
        }
        result = market.union_symbols("USDT", 2)
        self.assertEqual(result, {"BTC/USDT": ("aaa", "bbb", "ccc")})
        wide = market.union_symbols("USDT", 1)
        self.assertEqual(wide.get("ETH/USDT"), ("aaa",))
        self.assertNotIn("SOL/USDT", wide)
        capped = market.union_symbols("USDT", 2, maximum=0)
        self.assertEqual(len(capped), 1)


if __name__ == "__main__":
    unittest.main()
