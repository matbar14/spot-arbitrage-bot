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
        session.poll_interval = 5
        session.max_symbols = 0
        session.market = None
        session.active_symbols = ("BTC/USDT",)
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


if __name__ == "__main__":
    unittest.main()
