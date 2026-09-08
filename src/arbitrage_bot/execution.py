import asyncio
import logging
from decimal import Decimal

from .exchanges import PublicMarketData
from .strategy import Opportunity

log = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    pass


class Executor:
    def __init__(self, market: PublicMarketData, timeout: float, max_hedge_slippage_bps: Decimal):
        self.market = market
        self.timeout = timeout
        self.max_hedge_slippage_bps = max_hedge_slippage_bps

    async def _balance(self, exchange: str, currency: str) -> Decimal:
        balance = await self.market.client(exchange).fetch_balance()
        return Decimal(str(balance.get(currency, {}).get("free", 0) or 0))

    def _amount(self, exchange: str, symbol: str, amount: Decimal) -> Decimal:
        client = self.market.client(exchange)
        limits = client.market(symbol).get("limits", {}).get("amount", {})
        minimum = Decimal(str(limits.get("min") or 0))
        maximum = Decimal(str(limits.get("max") or amount))
        bounded = min(amount, maximum)
        if bounded < minimum:
            raise ExecutionError(f"{exchange}: amount below minimum")
        rounded = Decimal(client.amount_to_precision(symbol, float(bounded)))
        if rounded <= 0 or rounded < minimum:
            raise ExecutionError(f"{exchange}: amount rounded below minimum")
        return rounded

    def _cost(self, exchange: str, symbol: str, amount: Decimal, price: Decimal) -> None:
        cost_limits = self.market.client(exchange).market(symbol).get("limits", {}).get("cost", {})
        minimum = Decimal(str(cost_limits.get("min") or 0))
        maximum = cost_limits.get("max")
        cost = amount * price
        if cost < minimum:
            raise ExecutionError(f"{exchange}: order cost below minimum")
        if maximum is not None and cost > Decimal(str(maximum)):
            raise ExecutionError(f"{exchange}: order cost above maximum")

    async def execute(self, opportunity: Opportunity) -> dict:
        buy_symbol = self.market.market_symbol(opportunity.buy_exchange, opportunity.symbol)
        sell_symbol = self.market.market_symbol(opportunity.sell_exchange, opportunity.symbol)
        base, quote = buy_symbol.split("/")
        buy_client, sell_client = self.market.client(opportunity.buy_exchange), self.market.client(opportunity.sell_exchange)
        if not buy_client.has.get("createOrder") or not sell_client.has.get("createOrder"):
            raise ExecutionError("both exchanges must support createOrder")
        if not buy_client.has.get("fetchOrder") or not sell_client.has.get("fetchOrder"):
            raise ExecutionError("both exchanges must support fetchOrder")
        amount = self._amount(opportunity.buy_exchange, buy_symbol, opportunity.quantity)
        buy_price = Decimal(buy_client.price_to_precision(buy_symbol, float(opportunity.buy_price)))
        sell_price = Decimal(sell_client.price_to_precision(sell_symbol, float(opportunity.sell_price)))
        quote_free, base_free = await asyncio.gather(
            self._balance(opportunity.buy_exchange, quote), self._balance(opportunity.sell_exchange, base)
        )
        buy_unit_cost = opportunity.buy_cost / opportunity.quantity
        amount = min(amount, quote_free / buy_unit_cost, base_free)
        amount = self._amount(opportunity.buy_exchange, buy_symbol, amount)
        amount = self._amount(opportunity.sell_exchange, sell_symbol, amount)
        self._cost(opportunity.buy_exchange, buy_symbol, amount, buy_price)
        self._cost(opportunity.sell_exchange, sell_symbol, amount, sell_price)
        if quote_free < amount * buy_price or base_free < amount:
            raise ExecutionError("insufficient pre-trade balance")
        params = {"timeInForce": "IOC"}
        orders = await asyncio.gather(
            buy_client.create_order(buy_symbol, "limit", "buy", float(amount), float(buy_price), params),
            sell_client.create_order(sell_symbol, "limit", "sell", float(amount), float(sell_price), params),
            return_exceptions=True,
        )
        states = await asyncio.gather(
            self._resolve(opportunity.buy_exchange, buy_symbol, orders[0]),
            self._resolve(opportunity.sell_exchange, sell_symbol, orders[1]),
        )
        if any(isinstance(state, Exception) for state in states):
            await self._cancel_open(opportunity, states)
            raise ExecutionError(f"order submission/reconciliation failed: {states}")
        buy_filled = Decimal(str(states[0].get("filled", 0) or 0))
        sell_filled = Decimal(str(states[1].get("filled", 0) or 0))
        residual = buy_filled - sell_filled
        if residual:
            await self._hedge_residual(opportunity, residual, buy_symbol, sell_symbol)
        return {"buy": states[0], "sell": states[1], "hedged_residual": residual}

    async def _resolve(self, exchange: str, symbol: str, result) -> dict:
        if isinstance(result, Exception):
            return result
        client = self.market.client(exchange)
        deadline = asyncio.get_running_loop().time() + self.timeout
        state = result
        while asyncio.get_running_loop().time() < deadline:
            if state.get("status") in {"closed", "canceled", "rejected"}:
                return state
            state = await client.fetch_order(result["id"], symbol)
            await asyncio.sleep(0.1)
        try:
            await client.cancel_order(result["id"], symbol)
        except Exception as exc:
            log.error("cancel failed for %s/%s: %s", exchange, result.get("id"), exc)
        return await client.fetch_order(result["id"], symbol)

    async def _hedge_residual(self, opportunity: Opportunity, residual: Decimal, buy_symbol: str, sell_symbol: str):
        exchange = opportunity.buy_exchange if residual > 0 else opportunity.sell_exchange
        symbol = buy_symbol if residual > 0 else sell_symbol
        side = "sell" if residual > 0 else "buy"
        book = await self.market.fetch(exchange, opportunity.symbol, 5)
        if not book.bids or not book.asks:
            raise ExecutionError("cannot hedge: empty order book")
        reference = book.bids[0][0] if side == "sell" else book.asks[0][0]
        multiplier = Decimal(1) - self.max_hedge_slippage_bps / Decimal(10000) if side == "sell" else Decimal(1) + self.max_hedge_slippage_bps / Decimal(10000)
        client = self.market.client(exchange)
        amount = self._amount(exchange, symbol, abs(residual))
        price = Decimal(client.price_to_precision(symbol, float(reference * multiplier)))
        log.error("hedging residual: %s %s %s", side, amount, symbol)
        order = await client.create_order(symbol, "limit", side, float(amount), float(price), {"timeInForce": "IOC"})
        state = await self._resolve(exchange, symbol, order)
        if isinstance(state, Exception) or Decimal(str(state.get("filled", 0) or 0)) < amount:
            raise ExecutionError(f"residual hedge incomplete: {state}")

    async def _cancel_open(self, opportunity: Opportunity, orders: list):
        for exchange, order in zip((opportunity.buy_exchange, opportunity.sell_exchange), orders):
            if isinstance(order, dict) and order.get("id"):
                try:
                    symbol = self.market.market_symbol(exchange, opportunity.symbol)
                    await self.market.client(exchange).cancel_order(order["id"], symbol)
                except Exception as exc:
                    log.error("cancel failed on %s: %s", exchange, exc)
