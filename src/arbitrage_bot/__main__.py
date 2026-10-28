import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from .config import Settings
from .exchanges import PublicMarketData, EXCHANGE_REGISTRY, ExchangeConfigurationError
from .strategy import find_opportunities
from .execution import Executor, ExecutionError
from .fees import FeeConfigurationError, resolve_mexc_taker_fee, static_taker_fee_bps
from .trade_gate import NotionalTracker, TradeGate

class ColorFormatter(logging.Formatter):
    colors = {
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }

    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.enabled = sys.stderr.isatty() or os.getenv("FORCE_COLOR", "false").lower() == "true"

    def format(self, record):
        message = super().format(record)
        if not self.enabled:
            return message
        color = "\033[1;32m" if getattr(record, "arbitrage", False) else self.colors.get(record.levelno)
        return f"{color}{message}\033[0m" if color else message


handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])

SUPPORTED_EXCHANGES = tuple(sorted(EXCHANGE_REGISTRY))


def parse_pair(value: str) -> tuple[str, str]:
    normalized = value.lower().replace("↔", "-").replace("–", "-")
    parts = normalized.split("-")
    if len(parts) != 2 or parts[0] == parts[1] or any(p not in EXCHANGE_REGISTRY for p in parts):
        raise ValueError(f"unsupported pair {value}; exchanges: {', '.join(SUPPORTED_EXCHANGES)}")
    return parts[0], parts[1]


async def run(pair_override: str | None = None):
    settings = Settings()
    try:
        exchange_ids = parse_pair(pair_override or settings.pair)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    credentials = settings.exchange_credentials()
    if settings.live_trading and not all(all(credentials[name]) for name in exchange_ids):
        raise RuntimeError(f"LIVE_TRADING=true requires API keys for: {', '.join(exchange_ids)}")
    logging.info("exchange pair: %s ↔ %s", *exchange_ids)
    market = PublicMarketData(settings.timeout_seconds, credentials, exchange_ids)
    executor = Executor(
        market, settings.order_timeout_seconds, Decimal(str(settings.max_hedge_slippage_bps)),
        settings.protective_price_level, settings.orderbook_limit,
    )
    trade_gate = TradeGate(settings.trade_cooldown_seconds, Decimal(str(settings.rearm_edge_bps)))
    notional_tracker = NotionalTracker(
        Decimal(str(settings.max_session_notional)),
        Decimal(str(settings.max_total_session_notional)),
        Decimal(str(settings.rearm_edge_bps)),
    )
    try:
        await market.load_markets()
        if settings.symbols == ("ALL",):
            active_symbols = market.common_symbols(exchange_ids, settings.quote_currency, settings.max_symbols)
        else:
            active_symbol_list = []
            for symbol in settings.symbols:
                try:
                    for exchange in exchange_ids:
                        market.market_symbol(exchange, symbol)
                    active_symbol_list.append(market.market_symbol(exchange_ids[0], symbol))
                except ValueError:
                    continue
            active_symbols = tuple(active_symbol_list)
        skipped_symbols = tuple(symbol for symbol in settings.symbols if symbol != "ALL" and symbol not in active_symbols)
        if skipped_symbols:
            logging.warning("SKIP unavailable on selected exchange pair: %s", ",".join(skipped_symbols))
        if not active_symbols:
            raise RuntimeError("none of the configured symbols is active on both exchanges")
        logging.info("common active %s spot pairs: %d", settings.quote_currency, len(active_symbols))
        fees_by_symbol = {}
        for symbol in active_symbols:
            fee_values = {}
            for exchange in exchange_ids:
                if exchange == "mexc":
                    fee_values[exchange] = await resolve_mexc_taker_fee(
                        market.client(exchange), market.market_symbol(exchange, symbol),
                        Decimal(str(settings.mexc_fee_bps)) if settings.mexc_fee_bps is not None else None,
                        live=settings.live_trading, auto_fetch=settings.auto_fetch_mexc_fee,
                    )
                else:
                    fee_values[exchange] = static_taker_fee_bps(settings, exchange)
            fees_by_symbol[symbol] = fee_values
            logging.info("fees %s: %s", symbol, ", ".join(f"{name}={value:.2f} bps" for name, value in fee_values.items()))
        while True:
            for symbol in active_symbols:
                try:
                    books = await asyncio.gather(*(market.fetch(exchange, symbol, settings.orderbook_limit) for exchange in exchange_ids))
                    empty_books = tuple(book.exchange for book in books if not book.bids or not book.asks)
                    if empty_books:
                        logging.info("%s no actionable spread: empty order book on %s", symbol, ",".join(empty_books))
                        continue
                    edge_budget = notional_tracker.remaining(symbol)
                    if edge_budget <= 0:
                        logging.info(
                            "%s accumulation limit reached: pair=%.2f total=%.2f USDT",
                            symbol, settings.max_session_notional, settings.max_total_session_notional,
                        )
                        continue
                    slice_budget = min(
                        Decimal(str(settings.max_notional_quote)),
                        Decimal(str(settings.max_slice_notional)),
                        edge_budget,
                    )
                    opportunities = find_opportunities(
                        books[0], books[1], slice_budget,
                        fees_by_symbol[symbol], Decimal(str(settings.slippage_buffer_bps)),
                        settings.protective_price_level,
                    )
                    for candidate in opportunities:
                        if candidate.net_edge_bps > Decimal(str(settings.max_net_edge_bps)):
                            logging.warning("REJECT %s buy=%s sell=%s implausible net_edge=%.2f bps cap=%.2f bps", symbol, candidate.buy_exchange, candidate.sell_exchange, candidate.net_edge_bps, settings.max_net_edge_bps)
                        else:
                            logging.info("CHECK %s buy=%s sell=%s net_edge=%.2f bps profit≈%s", symbol, candidate.buy_exchange, candidate.sell_exchange, candidate.net_edge_bps, candidate.estimated_profit)
                    opportunities = [candidate for candidate in opportunities if candidate.net_edge_bps <= Decimal(str(settings.max_net_edge_bps))]
                    opportunity = max(opportunities, key=lambda candidate: candidate.net_edge_bps, default=None)
                    edge_bps = opportunity.net_edge_bps if opportunity else None
                    trade_gate.observe(symbol, edge_bps)
                    notional_tracker.observe(symbol, edge_bps)
                    if notional_tracker.remaining(symbol) <= 0:
                        logging.info(
                            "%s accumulation limit reached: pair=%.2f total=%.2f USDT",
                            symbol, settings.max_session_notional, settings.max_total_session_notional,
                        )
                        continue
                    is_fresh = opportunity and (datetime.now(timezone.utc) - opportunity.observed_at).total_seconds() <= settings.max_book_age_seconds
                    can_start = bool(opportunity and trade_gate.can_enter(symbol, opportunity.net_edge_bps))
                    can_continue = bool(
                        opportunity
                        and notional_tracker.used(symbol) > 0
                        and trade_gate.can_continue(symbol, opportunity.net_edge_bps)
                    )
                    can_trade = can_start or can_continue
                    if opportunity and is_fresh and opportunity.net_edge_bps >= Decimal(str(settings.min_net_edge_bps)):
                        if not can_trade:
                            logging.info("%s entry blocked: cooldown %.1fs or waiting for spread reset", symbol, trade_gate.remaining_cooldown(symbol))
                        else:
                            logging.info("🚨 ARBITRAGE %s buy=%s sell=%s qty=%s edge=%.2f bps profit≈%s tranche≈%.2f/%0.2f USDT", symbol, opportunity.buy_exchange, opportunity.sell_exchange, opportunity.quantity, opportunity.net_edge_bps, opportunity.estimated_profit, slice_budget, settings.max_session_notional, extra={"arbitrage": True})
                        if settings.live_trading and can_trade:
                            try:
                                result = await executor.execute(opportunity)
                                trade_gate.mark_completed(symbol)
                                notional_tracker.record(symbol, Decimal(str(result.get("executed_notional", 0))))
                                logging.info("FILLED %s: buy=%s sell=%s", symbol, result["buy"].get("filled"), result["sell"].get("filled"))
                            except ExecutionError as exc:
                                logging.error("TRADE ABORTED %s: %s", symbol, exc)
                    elif opportunity and is_fresh:
                        logging.info("%s no actionable spread: buy=%s sell=%s net_edge=%.2f bps profit≈%s threshold=%.2f bps", symbol, opportunity.buy_exchange, opportunity.sell_exchange, opportunity.net_edge_bps, opportunity.estimated_profit, settings.min_net_edge_bps)
                    elif opportunity:
                        logging.info("%s no actionable spread: stale book age>%.1fs", symbol, settings.max_book_age_seconds)
                    else:
                        logging.info("%s no actionable spread: insufficient executable depth", symbol)
                except Exception as exc:
                    logging.warning("%s market-data error: %s", symbol, exc)
            await asyncio.sleep(settings.poll_interval_seconds)
    finally:
        await market.close()

def main(argv=None):
    parser = argparse.ArgumentParser(description="Spot arbitrage monitor/trader")
    parser.add_argument("--pair", help="exchange pair, e.g. mexc-xeggex (overrides ARB_PAIR)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args.pair))
    except KeyboardInterrupt:
        logging.info("Остановлено пользователем. Открытые запросы закрыты.")
    except RuntimeError as exc:
        logging.error("%s", exc)
        if "all" in str(exc).lower() and "unsupported pair" in str(exc).lower():
            logging.error("подсказка: ARB_PAIR=all / --pair all поддерживается только веб-интерфейсом (python -m arbitrage_bot.web)")
        sys.exit(2)


if __name__ == "__main__":
    main()
