"""Live trading panel: config validation, paper execution, stats, endpoints."""

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aiohttp.test_utils import TestClient, TestServer

from arbitrage_bot.config import Settings
from arbitrage_bot.web import Session, create_app


def make_session() -> Session:
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
    session.cycles = 0
    session.last_error = None
    session.started_at = 0.0
    session._lock = asyncio.Lock()
    session.live_enabled = False
    session.live_capital = 100.0
    session.live_min_edge_bps = 35.0
    session.live_started_at = None
    session.paper_trades = []
    session._trade_cooldowns = {}
    return session


def calc_response(profit: float, edge: float, buy: str = "mexc", sell: str = "nonkyc"):
    return {
        "symbol": "BTC/USDT",
        "notional": 100.0,
        "directions": [{
            "buy": buy, "sell": sell,
            "quantity": 0.001, "vwap_buy": 99000.0, "vwap_sell": 99400.0,
            "buy_cost_incl_fee": 100.0, "sell_proceeds_net_fee": 100.0 + profit,
            "net_profit": profit, "net_edge_bps": edge,
            "book_age_s": 0.1,
        }],
    }


class ConfigureLiveTests(unittest.TestCase):
    def test_valid_config_applies(self):
        session = make_session()
        session.configure_live(capital_per_venue=250, min_edge_bps=50, enabled=True)
        self.assertEqual(session.live_capital, 250.0)
        self.assertEqual(session.live_min_edge_bps, 50.0)
        self.assertTrue(session.live_enabled)
        self.assertIsNotNone(session.live_started_at)

    def test_invalid_capital_rejected(self):
        session = make_session()
        with self.assertRaises(ValueError):
            session.configure_live(capital_per_venue=0)
        with self.assertRaises(ValueError):
            session.configure_live(capital_per_venue=-5)
        self.assertEqual(session.live_capital, 100.0)

    def test_invalid_edge_rejected(self):
        session = make_session()
        with self.assertRaises(ValueError):
            session.configure_live(min_edge_bps=2000)
        self.assertEqual(session.live_min_edge_bps, 35.0)

    def test_reset_clears_history(self):
        session = make_session()
        session.paper_trades = [{"net_profit": 1.0}]
        session._trade_cooldowns = {"x": 1.0}
        session.configure_live(reset=True)
        self.assertEqual(session.paper_trades, [])
        self.assertEqual(session._trade_cooldowns, {})


class PaperExecutionTests(unittest.TestCase):
    def test_confirmed_signal_recorded(self):
        session = make_session()
        session.live_enabled = True
        row = {"symbol": "BTC/USDT", "buy": "mexc", "sell": "nonkyc", "net_edge_bps": 60}

        async def fake_calc(payload):
            self.assertEqual(payload["notional"], 100.0)
            return calc_response(profit=0.42, edge=55)

        session.calc = fake_calc
        asyncio.run(session._paper_execute([row]))
        self.assertEqual(len(session.paper_trades), 1)
        trade = session.paper_trades[0]
        self.assertEqual(trade["symbol"], "BTC/USDT")
        self.assertAlmostEqual(trade["net_profit"], 0.42)
        self.assertAlmostEqual(trade["roi_pct"], 0.42)

    def test_cooldown_blocks_repeat(self):
        session = make_session()
        session.live_enabled = True
        row = {"symbol": "BTC/USDT", "buy": "mexc", "sell": "nonkyc", "net_edge_bps": 60}
        session.calc = lambda payload: _async(calc_response(0.5, 55))
        asyncio.run(session._paper_execute([row]))
        asyncio.run(session._paper_execute([row]))
        self.assertEqual(len(session.paper_trades), 1)  # второй вход в тот же спред заблокирован

    def test_below_threshold_skipped(self):
        session = make_session()
        session.live_enabled = True
        row = {"symbol": "BTC/USDT", "buy": "mexc", "sell": "nonkyc", "net_edge_bps": 20}
        called = []

        async def fake_calc(payload):
            called.append(payload)

        session.calc = fake_calc
        asyncio.run(session._paper_execute([row]))
        self.assertEqual(called, [])
        self.assertEqual(session.paper_trades, [])

    def test_unconfirmed_spread_not_recorded(self):
        session = make_session()
        session.live_enabled = True
        row = {"symbol": "BTC/USDT", "buy": "mexc", "sell": "nonkyc", "net_edge_bps": 60}
        # свежие стаканы дали слишком слабый edge — сделка не записывается
        session.calc = lambda payload: _async(calc_response(0.1, 10))
        asyncio.run(session._paper_execute([row]))
        self.assertEqual(session.paper_trades, [])

    def test_wrong_direction_not_recorded(self):
        session = make_session()
        session.live_enabled = True
        row = {"symbol": "BTC/USDT", "buy": "mexc", "sell": "nonkyc", "net_edge_bps": 60}
        session.calc = lambda payload: _async(calc_response(0.5, 55, buy="nonkyc", sell="mexc"))
        asyncio.run(session._paper_execute([row]))
        self.assertEqual(session.paper_trades, [])

    def test_leveraged_tokens_skipped(self):
        session = make_session()
        session.live_enabled = True
        row = {"symbol": "ADA3S/USDT", "buy": "xt", "sell": "gate", "net_edge_bps": 500}
        called = []

        async def fake_calc(payload):
            called.append(payload)
            return calc_response(40.0, 400, buy="xt", sell="gate")

        session.calc = fake_calc
        asyncio.run(session._paper_execute([row]))
        self.assertEqual(called, [])  # leveraged-токен даже не проверяется
        self.assertEqual(session.paper_trades, [])

    def test_implausible_edge_over_cap_rejected(self):
        session = make_session()
        session.live_enabled = True
        session.settings.max_net_edge_bps  # noqa: B018 — поле существует
        object.__setattr__(session.settings, "max_net_edge_bps", 500)  # frozen dataclass
        row = {"symbol": "BTC/USDT", "buy": "mexc", "sell": "nonkyc", "net_edge_bps": 60}
        session.calc = lambda payload: _async(calc_response(40.0, 4446))  # свежие стаканы дали аномалию
        asyncio.run(session._paper_execute([row]))
        self.assertEqual(session.paper_trades, [])  # кап max_net_edge_bps отсекает фантом


def _async(value):
    async def inner():
        return value
    return inner()


class LiveStatsTests(unittest.TestCase):
    def test_payload_math(self):
        session = make_session()
        session.live_enabled = True
        session.live_started_at = None  # duration 0
        base = {"quantity": 0.001, "vwap_buy": 1.0, "vwap_sell": 1.0, "net_edge_bps": 50.0, "book_age_s": 0.1}
        session.paper_trades = [
            dict(base, ts="t1", symbol="BTC/USDT", buy="mexc", sell="nonkyc",
                 notional=100.0, buy_cost=100.0, sell_proceeds=100.5, net_profit=0.5, roi_pct=0.5),
            dict(base, ts="t2", symbol="ETH/USDT", buy="mexc", sell="nonkyc",
                 notional=100.0, buy_cost=100.0, sell_proceeds=99.8, net_profit=-0.2, roi_pct=-0.2),
        ]
        stats = session.live_stats_payload()
        self.assertEqual(stats["trades_total"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertAlmostEqual(stats["total_profit"], 0.3)
        self.assertEqual(stats["deployed_capital"], 200.0)  # 100 USDT × 2 задействованные площадки
        self.assertAlmostEqual(stats["worst_trade"], -0.2)
        self.assertEqual(stats["venues"]["mexc"]["buys"], 2)
        self.assertEqual(stats["venues"]["nonkyc"]["sells"], 2)
        self.assertEqual(len(stats["recent"]), 2)

    def test_disabled_empty_payload(self):
        session = make_session()
        stats = session.live_stats_payload()
        self.assertFalse(stats["enabled"])
        self.assertEqual(stats["trades_total"], 0)
        self.assertEqual(stats["deployed_capital"], 200.0)  # капитал × число площадок


class LiveEndpointsTests(unittest.TestCase):
    def test_stats_and_config_endpoints(self):
        session = make_session()

        async def run():
            client = TestClient(TestServer(create_app(session)))
            await client.start_server()
            try:
                stats = await client.get("/api/live/stats")
                self.assertEqual(stats.status, 200)
                payload = await stats.json()
                self.assertFalse(payload["enabled"])
                self.assertEqual(payload["capital_per_venue"], 100.0)

                bad = await client.post("/api/live/config", json={"capital_per_venue": -1})
                self.assertEqual(bad.status, 400)

                config = await client.post("/api/live/config", json={
                    "capital_per_venue": 100, "min_edge_bps": 40, "enabled": True})
                self.assertEqual(config.status, 200)
                payload = await config.json()
                self.assertTrue(payload["enabled"])
                self.assertEqual(payload["min_edge_bps"], 40.0)

                reset = await client.post("/api/live/config", json={"reset": True})
                self.assertEqual(reset.status, 200)
            finally:
                await client.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
