import time
from decimal import Decimal


class TradeGate:
    def __init__(self, cooldown_seconds: float, rearm_edge_bps: Decimal):
        self.cooldown_seconds = cooldown_seconds
        self.rearm_edge_bps = rearm_edge_bps
        self._last_trade: dict[str, float] = {}
        self._armed: dict[str, bool] = {}

    def observe(self, symbol: str, edge_bps: Decimal | None) -> None:
        if edge_bps is None or edge_bps < self.rearm_edge_bps:
            self._armed[symbol] = True

    def can_enter(self, symbol: str, edge_bps: Decimal, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        last_trade = self._last_trade.get(symbol)
        if last_trade is not None and current - last_trade < self.cooldown_seconds:
            return False
        return self._armed.get(symbol, True)

    def can_continue(self, symbol: str, edge_bps: Decimal, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        last_trade = self._last_trade.get(symbol)
        if last_trade is None or self._armed.get(symbol, True) or edge_bps < self.rearm_edge_bps:
            return False
        return current - last_trade >= self.cooldown_seconds

    def mark_completed(self, symbol: str, now: float | None = None) -> None:
        self._last_trade[symbol] = time.monotonic() if now is None else now
        self._armed[symbol] = False

    def remaining_cooldown(self, symbol: str, now: float | None = None) -> float:
        last_trade = self._last_trade.get(symbol)
        if last_trade is None:
            return 0.0
        current = time.monotonic() if now is None else now
        return max(0.0, self.cooldown_seconds - (current - last_trade))


class NotionalTracker:
    def __init__(self, pair_target_quote: Decimal, total_target_quote: Decimal, rearm_edge_bps: Decimal):
        self.pair_target_quote = max(Decimal(0), pair_target_quote)
        self.total_target_quote = max(Decimal(0), total_target_quote)
        self.rearm_edge_bps = rearm_edge_bps
        self._used: dict[str, Decimal] = {}

    def observe(self, symbol: str, edge_bps: Decimal | None) -> None:
        if edge_bps is None or edge_bps < self.rearm_edge_bps:
            self._used.pop(symbol, None)

    def remaining(self, symbol: str) -> Decimal:
        pair_remaining = self.pair_target_quote - self._used.get(symbol, Decimal(0))
        total_remaining = self.total_target_quote - sum(self._used.values(), Decimal(0))
        return max(Decimal(0), min(pair_remaining, total_remaining))

    def record(self, symbol: str, notional_quote: Decimal) -> None:
        if notional_quote > 0:
            self._used[symbol] = self._used.get(symbol, Decimal(0)) + notional_quote

    def used(self, symbol: str) -> Decimal:
        return self._used.get(symbol, Decimal(0))

    def total_used(self) -> Decimal:
        return sum(self._used.values(), Decimal(0))
