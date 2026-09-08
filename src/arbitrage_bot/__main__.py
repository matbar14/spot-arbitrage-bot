import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from .config import Settings
from .exchanges import PublicMarketData
from .strategy import find_opportunities
from .execution import Executor, ExecutionError
from .fees import FeeConfigurationError, resolve_mexc_taker_fee
from .trade_gate import TradeGate

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

async def run():
    settings = Settings()
    credentials = {"mexc": (settings.mexc_api_key, settings.mexc_secret), "nonkyc": (settings.nonkyc_api_key, settings.nonkyc_secret)}
    if settings.live_trading and not all(all(credentials[name]) for name in credentials):
        raise RuntimeError("LIVE_TRADING=true requires API keys for both exchanges")
    market = PublicMarketData(settings.timeout_seconds, credentials)
    executor = Executor(market, settings.order_timeout_seconds, Decimal(str(settings.max_hedge_slippage_bps)))
    trade_gate = TradeGate(settings.trade_cooldown_seconds, Decimal(str(settings.rearm_edge_bps)))
    try:
        await market.load_markets()
        active_symbol_list = []
        for symbol in settings.symbols:
            try:
                market.market_symbol("mexc", symbol)
                market.market_symbol("nonkyc", symbol)
                active_symbol_list.append(symbol)
            except ValueError:
                continue
        active_symbols = tuple(active_symbol_list)
        skipped_symbols = tuple(symbol for symbol in settings.symbols if symbol not in active_symbols)
        if skipped_symbols:
            logging.warning("SKIP unsupported on both exchanges: %s", ",".join(skipped_symbols))
        if not active_symbols:
            raise RuntimeError("none of the configured symbols is active on both exchanges")
        fees_by_symbol = {}
        for symbol in active_symbols:
            mexc_symbol = market.market_symbol("mexc", symbol)
            fees_by_symbol[symbol] = {
                "mexc": await resolve_mexc_taker_fee(
                    market.client("mexc"), mexc_symbol,
                    Decimal(str(settings.mexc_fee_bps)) if settings.mexc_fee_bps is not None else None,
                    live=settings.live_trading, auto_fetch=settings.auto_fetch_mexc_fee,
                ),
                "nonkyc": Decimal(str(settings.nonkyc_fee_bps)),
            }
            logging.info("fees %s: MEXC taker=%.2f bps, NonKYC manual=%.2f bps", symbol, fees_by_symbol[symbol]["mexc"], fees_by_symbol[symbol]["nonkyc"])
        while True:
            for symbol in active_symbols:
                try:
                    mexc, nonkyc = await asyncio.gather(market.fetch("mexc", symbol, settings.orderbook_limit), market.fetch("nonkyc", symbol, settings.orderbook_limit))
                    opportunities = find_opportunities(mexc, nonkyc, Decimal(str(settings.max_notional_quote)), fees_by_symbol[symbol], Decimal(str(settings.slippage_buffer_bps)))
                    for candidate in opportunities:
                        logging.info("CHECK %s buy=%s sell=%s net_edge=%.2f bps profit≈%s", symbol, candidate.buy_exchange, candidate.sell_exchange, candidate.net_edge_bps, candidate.estimated_profit)
                    opportunity = max(opportunities, key=lambda candidate: candidate.net_edge_bps, default=None)
                    trade_gate.observe(symbol, opportunity.net_edge_bps if opportunity else None)
                    is_fresh = opportunity and (datetime.now(timezone.utc) - opportunity.observed_at).total_seconds() <= settings.max_book_age_seconds
                    if opportunity and is_fresh and opportunity.net_edge_bps >= Decimal(str(settings.min_net_edge_bps)):
                        if not trade_gate.can_enter(symbol, opportunity.net_edge_bps):
                            logging.info("%s entry blocked: cooldown %.1fs or waiting for spread reset", symbol, trade_gate.remaining_cooldown(symbol))
                        else:
                            logging.info("🚨 ARBITRAGE %s buy=%s sell=%s qty=%s edge=%.2f bps profit≈%s", symbol, opportunity.buy_exchange, opportunity.sell_exchange, opportunity.quantity, opportunity.net_edge_bps, opportunity.estimated_profit, extra={"arbitrage": True})
                        if settings.live_trading and trade_gate.can_enter(symbol, opportunity.net_edge_bps):
                            try:
                                result = await executor.execute(opportunity)
                                trade_gate.mark_completed(symbol)
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

def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("Остановлено пользователем. Открытые запросы закрыты.")


if __name__ == "__main__":
    main()
