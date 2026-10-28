"""Local web interface for the spot arbitrage bot.

Monitoring and position sizing only: the UI never places orders.
Launch with `python -m arbitrage_bot.web` and open http://127.0.0.1:8788.
"""
import asyncio
import logging
import os
import threading
import time
import webbrowser
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiohttp import web

from .__main__ import parse_pair
from .config import Settings
from .exchanges import Book, PublicMarketData, supported_exchange_ids, usable_exchange_ids
from .fees import resolve_mexc_taker_fee, static_taker_fee_bps
from .strategy import Opportunity, find_opportunities

STATIC_DIR = Path(__file__).resolve().parent / "static"
SNAPSHOT_MAX_ROWS = 1500


def _dec(value, default: Decimal | None = None) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _num(value: Decimal | None, places: int = 8) -> float | None:
    if value is None:
        return None
    quantized = value.quantize(Decimal(1).scaleb(-places)) if places else value
    return float(quantized)


def row_from_opportunity(opportunity: Opportunity, *, best: bool, min_edge_bps: float, book_age_s: float) -> dict:
    edge = float(opportunity.net_edge_bps)
    if edge >= min_edge_bps:
        level = "signal"
    elif edge >= 0:
        level = "watch"
    else:
        level = "negative"
    return {
        "symbol": opportunity.symbol,
        "buy": opportunity.buy_exchange,
        "sell": opportunity.sell_exchange,
        "net_edge_bps": round(edge, 2),
        "estimated_profit": float(opportunity.estimated_profit.quantize(Decimal("0.0001"))),
        "quantity": _num(opportunity.quantity),
        "buy_price": _num(opportunity.buy_price),
        "sell_price": _num(opportunity.sell_price),
        "best": best,
        "level": level,
        "book_age_s": round(book_age_s, 2),
    }


def direction_breakdown(opportunity: Opportunity, *, book_age_s: float) -> dict:
    edge = float(opportunity.net_edge_bps)
    return {
        "buy": opportunity.buy_exchange,
        "sell": opportunity.sell_exchange,
        "quantity": _num(opportunity.quantity),
        "vwap_buy": _num(opportunity.buy_price),
        "vwap_sell": _num(opportunity.sell_price),
        "buy_cost_incl_fee": _num(opportunity.buy_cost, 4),
        "sell_proceeds_net_fee": _num(opportunity.sell_proceeds, 4),
        "net_profit": _num(opportunity.estimated_profit, 4),
        "net_edge_bps": round(edge, 2),
        "level": "signal" if edge >= 0 else "negative",
        "book_age_s": round(book_age_s, 2),
    }


def book_view(book: Book, depth: int = 6) -> dict:
    return {
        "bids": [[_num(price), _num(amount)] for price, amount in book.bids[:depth]],
        "asks": [[_num(price), _num(amount)] for price, amount in book.asks[:depth]],
    }


class Session:
    """Holds one exchange-pair context: market clients, symbols, fees, scan loop.

    In all-linkages mode (pair == "all") every usable registry venue is loaded
    and every symbol is scanned across all venues listing it.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        raw_pair = (settings.pair or "").strip().lower()
        self.all_mode: bool = raw_pair == "all"
        self.exchange_ids: tuple[str, ...] = tuple(usable_exchange_ids()) if self.all_mode else parse_pair(raw_pair)
        self.poll_interval = settings.poll_interval_seconds
        self.max_symbols = settings.max_symbols
        self.market: PublicMarketData | None = None
        self.active_symbols: tuple[str, ...] = ()
        self.symbol_venues: dict[str, tuple[str, ...]] = {}
        self.fees_by_symbol: dict[str, dict[str, Decimal]] = {}
        self.task: asyncio.Task | None = None
        self.rows: list[dict] = []
        self.cycles = 0
        self.last_error: str | None = None
        self.started_at = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def pair_name(self) -> str:
        return "all" if self.all_mode else "-".join(self.exchange_ids)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def configure(self, pair: str | None = None, poll_interval: float | None = None, max_symbols: int | None = None) -> None:
        async with self._lock:
            await self._stop_task()
            if pair:
                normalized = str(pair).strip().lower()
                self.all_mode = normalized == "all"
                self.exchange_ids = tuple(usable_exchange_ids()) if self.all_mode else parse_pair(normalized)
            if poll_interval and poll_interval > 0:
                self.poll_interval = poll_interval
            if max_symbols is not None:
                self.max_symbols = max_symbols
            if self.market is not None:
                await self.market.close()
                self.market = None
            self.market = PublicMarketData(self.settings.timeout_seconds, self._credentials(), self.exchange_ids)
            await self.market.load_markets()
            await self._load_symbols_and_fees()
            self.rows = []
            self.cycles = 0

    def _credentials(self) -> dict[str, tuple[str, str]]:
        return self.settings.exchange_credentials()

    async def _load_symbols_and_fees(self) -> None:
        assert self.market is not None
        quote = self.settings.quote_currency
        venues_by_symbol: dict[str, tuple[str, ...]] = {}
        if self.settings.symbols == ("ALL",):
            if self.all_mode:
                venues_by_symbol = self.market.union_symbols(quote, 2, self.max_symbols)
            else:
                for symbol in self.market.common_symbols(self.exchange_ids, quote, self.max_symbols):
                    venues_by_symbol[symbol] = self.exchange_ids
        else:
            for symbol in self.settings.symbols:
                normalized = symbol.upper().replace("-", "/")
                if "/" not in normalized:
                    normalized = f"{normalized[:-4]}/{normalized[-4:]}"
                venues = []
                for exchange in self.exchange_ids:
                    try:
                        self.market.market_symbol(exchange, normalized)
                        venues.append(exchange)
                    except ValueError:
                        continue
                if len(venues) >= 2:
                    venues_by_symbol[normalized] = tuple(venues)
        if not venues_by_symbol:
            raise RuntimeError("none of the configured symbols is active on at least two exchanges")
        self.symbol_venues = venues_by_symbol
        self.active_symbols = tuple(venues_by_symbol)
        fees = {}
        for symbol, venues in venues_by_symbol.items():
            values = {}
            for exchange in venues:
                if exchange == "mexc":
                    values[exchange] = await resolve_mexc_taker_fee(
                        self.market.client(exchange), self.market.market_symbol(exchange, symbol),
                        Decimal(str(self.settings.mexc_fee_bps)) if self.settings.mexc_fee_bps is not None else None,
                        live=False, auto_fetch=self.settings.auto_fetch_mexc_fee,
                    )
                else:
                    values[exchange] = static_taker_fee_bps(self.settings, exchange)
            fees[symbol] = values
        self.fees_by_symbol = fees

    async def scan_once(self) -> None:
        assert self.market is not None
        budget = min(Decimal(str(self.settings.max_notional_quote)), Decimal(str(self.settings.max_slice_notional)))
        cap = Decimal(str(self.settings.max_net_edge_bps))
        buffer = Decimal(str(self.settings.slippage_buffer_bps))
        rows: list[dict] = []
        errors: list[str] = []
        processed = 0
        for symbol, venues in self.symbol_venues.items():
            try:
                results = await asyncio.gather(
                    *(self.market.fetch(exchange, symbol, self.settings.orderbook_limit) for exchange in venues),
                    return_exceptions=True,
                )
                books = [book for book in results if isinstance(book, Book) and book.bids and book.asks]
                fees = self.fees_by_symbol.get(symbol, {})
                fresh: list[Opportunity] = []
                if len(books) >= 2:
                    for index in range(len(books) - 1):
                        for other in books[index + 1:]:
                            pair_fees = {
                                book.exchange: fees[book.exchange]
                                for book in (books[index], other) if book.exchange in fees
                            }
                            if len(pair_fees) < 2:
                                continue
                            for opportunity in find_opportunities(
                                books[index], other, budget, pair_fees, buffer,
                                self.settings.protective_price_level,
                            ):
                                if opportunity.net_edge_bps <= cap:
                                    fresh.append(opportunity)
                if fresh:
                    now = datetime.now(timezone.utc)
                    book_age = max((now - book.received_at).total_seconds() for book in books)
                    best_edge = max(opportunity.net_edge_bps for opportunity in fresh)
                    for opportunity in fresh:
                        rows.append(row_from_opportunity(
                            opportunity, best=opportunity.net_edge_bps == best_edge,
                            min_edge_bps=self.settings.min_net_edge_bps, book_age_s=book_age,
                        ))
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
            processed += 1
            if processed % 25 == 0:
                rows.sort(key=lambda row: row["net_edge_bps"], reverse=True)
                self.rows = rows
        rows.sort(key=lambda row: row["net_edge_bps"], reverse=True)
        self.rows = rows
        self.cycles += 1
        self.last_error = "; ".join(errors[:3]) if errors else None

    async def _loop(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logging.warning("scan cycle error: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def start(self) -> None:
        async with self._lock:
            if self.market is None:
                self.market = PublicMarketData(self.settings.timeout_seconds, self._credentials(), self.exchange_ids)
                await self.market.load_markets()
                await self._load_symbols_and_fees()
            if not self.running:
                self.task = asyncio.create_task(self._loop())

    async def _stop_task(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_task()

    async def close(self) -> None:
        async with self._lock:
            await self._stop_task()
            if self.market is not None:
                await self.market.close()
                self.market = None

    def status_payload(self) -> dict:
        s = self.settings
        return {
            "pair": self.pair_name,
            "all_mode": self.all_mode,
            "exchanges": list(usable_exchange_ids()),
            "running": self.running,
            "poll_interval": self.poll_interval,
            "max_symbols": self.max_symbols,
            "symbols": list(self.active_symbols[:500]),
            "symbols_count": len(self.active_symbols),
            "cycles": self.cycles,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "last_error": self.last_error,
            "settings": {
                "min_net_edge_bps": s.min_net_edge_bps,
                "max_net_edge_bps": s.max_net_edge_bps,
                "max_notional_quote": s.max_notional_quote,
                "max_slice_notional": s.max_slice_notional,
                "max_session_notional": s.max_session_notional,
                "slippage_buffer_bps": s.slippage_buffer_bps,
                "protective_price_level": s.protective_price_level,
                "orderbook_limit": s.orderbook_limit,
                "quote_currency": s.quote_currency,
            },
            "fees_default": {
                exchange_id: (
                    (5.0 if s.mexc_fee_bps is None else s.mexc_fee_bps)
                    if exchange_id == "mexc"
                    else float(static_taker_fee_bps(s, exchange_id))
                )
                for exchange_id in supported_exchange_ids()
            },
        }

    async def calc(self, payload: dict) -> dict:
        if self.market is None:
            await self.configure()
        assert self.market is not None
        symbol = str(payload.get("symbol", "")).strip().upper().replace("-", "/")
        if "/" not in symbol:
            symbol = f"{symbol[:-4]}/{symbol[-4:]}"
        buy = str(payload.get("buy") or self.exchange_ids[0]).strip().lower()
        sell = str(payload.get("sell") or self.exchange_ids[1]).strip().lower()
        if buy == sell:
            raise ValueError("buy and sell exchanges must differ")
        for exchange in (buy, sell):
            if exchange not in self.market.clients:
                raise ValueError(f"unsupported exchange {exchange}")
        pair_ids = (buy, sell)
        normalized = self.market.market_symbol(buy, symbol)
        notional = _dec(payload.get("notional"), Decimal("100"))
        if notional is None or notional <= 0:
            raise ValueError("notional must be a positive number")
        buffer_bps = _dec(payload.get("buffer_bps"), Decimal(str(self.settings.slippage_buffer_bps)))
        overrides = payload.get("fees") or {}
        fees = {}
        for exchange in pair_ids:
            override = _dec(overrides.get(exchange))
            if override is not None and override >= 0:
                fees[exchange] = override
            elif exchange == "mexc":
                fees[exchange] = await resolve_mexc_taker_fee(
                    self.market.client(exchange), self.market.market_symbol(exchange, normalized),
                    Decimal(str(self.settings.mexc_fee_bps)) if self.settings.mexc_fee_bps is not None else None,
                    live=False, auto_fetch=self.settings.auto_fetch_mexc_fee,
                )
            else:
                fees[exchange] = static_taker_fee_bps(self.settings, exchange)
        books = await asyncio.gather(
            *(self.market.fetch(exchange, normalized, self.settings.orderbook_limit) for exchange in pair_ids)
        )
        ages = {book.exchange: (datetime.now(timezone.utc) - book.received_at).total_seconds() for book in books}
        opportunities = find_opportunities(
            books[0], books[1], notional, fees, buffer_bps, self.settings.protective_price_level,
        )
        directions = [direction_breakdown(opportunity, book_age_s=ages[opportunity.buy_exchange]) for opportunity in opportunities]
        directions.sort(key=lambda item: item["net_edge_bps"], reverse=True)
        cap = Decimal(str(self.settings.max_net_edge_bps))
        return {
            "symbol": normalized,
            "pair": f"{buy}-{sell}",
            "notional": _num(notional, 2),
            "buffer_bps": _num(buffer_bps, 2),
            "fees": {exchange: _num(value, 2) for exchange, value in fees.items()},
            "directions": directions,
            "books": {book.exchange: book_view(book) for book in books},
            "book_age_s": {exchange: round(age, 2) for exchange, age in ages.items()},
            "empty_books": [book.exchange for book in books if not book.bids or not book.asks],
            "implausible": any(Decimal(str(item["net_edge_bps"])) > cap for item in directions),
        }


async def index(_request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def get_session(request: web.Request) -> web.Response:
    session: Session = request.app["session"]
    return web.json_response(session.status_payload())


async def post_session(request: web.Request) -> web.Response:
    session: Session = request.app["session"]
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="invalid JSON body")
    try:
        await session.configure(
            pair=payload.get("pair"),
            poll_interval=payload.get("poll_interval"),
            max_symbols=payload.get("max_symbols"),
        )
        if payload.get("start", True):
            await session.start()
    except (ValueError, RuntimeError) as exc:
        raise web.HTTPBadRequest(text=str(exc))
    return web.json_response(session.status_payload())


async def start_scan(request: web.Request) -> web.Response:
    session: Session = request.app["session"]
    await session.start()
    return web.json_response(session.status_payload())


async def stop_scan(request: web.Request) -> web.Response:
    session: Session = request.app["session"]
    await session.stop()
    return web.json_response(session.status_payload())


async def get_snapshot(request: web.Request) -> web.Response:
    session: Session = request.app["session"]
    try:
        min_edge = float(request.query.get("min_edge", ""))
    except ValueError:
        min_edge = None
    total = len(session.rows)
    rows = session.rows
    if min_edge is not None:
        rows = [row for row in rows if row["net_edge_bps"] >= min_edge]
    rows = rows[:SNAPSHOT_MAX_ROWS]
    return web.json_response({
        "pair": session.pair_name,
        "all_mode": session.all_mode,
        "running": session.running,
        "cycles": session.cycles,
        "last_error": session.last_error,
        "rows": rows,
        "rows_total": total,
        "rows_shown": len(rows),
    })


async def post_calc(request: web.Request) -> web.Response:
    session: Session = request.app["session"]
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="invalid JSON body")
    try:
        return web.json_response(await session.calc(payload))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    except Exception as exc:
        raise web.HTTPInternalServerError(text=f"market data error: {exc}")


def create_app(session: Session) -> web.Application:
    app = web.Application()
    app["session"] = session
    app.router.add_get("/", index)
    app.router.add_get("/api/session", get_session)
    app.router.add_post("/api/session", post_session)
    app.router.add_post("/api/scan/start", start_scan)
    app.router.add_post("/api/scan/stop", stop_scan)
    app.router.add_get("/api/snapshot", get_snapshot)
    app.router.add_post("/api/calc", post_calc)
    async def on_cleanup(app: web.Application) -> None:
        await session.close()
    app.on_cleanup.append(on_cleanup)
    return app


def main(argv=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Web UI for the spot arbitrage bot (monitoring and sizing only)")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8788")))
    parser.add_argument("--pair", choices=None, help="exchange pair, e.g. mexc-xeggex, or 'all' (overrides ARB_PAIR)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = parser.parse_args(argv)

    settings = Settings()
    if args.pair:
        settings = Settings(**{**settings.__dict__, "pair": args.pair.lower()})
    session = Session(settings)
    app = create_app(session)
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser and os.getenv("NO_BROWSER", "").lower() != "true":
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.info("web UI: %s (Ctrl+C to stop)", url)
    web.run_app(app, host=args.host, port=args.port, print=lambda *_: None)


if __name__ == "__main__":
    main()
