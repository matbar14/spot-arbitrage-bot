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

    def mark_completed(self, symbol: str, now: float | None = None) -> None:
        self._last_trade[symbol] = time.monotonic() if now is None else now
        self._armed[symbol] = False

    def remaining_cooldown(self, symbol: str, now: float | None = None) -> float:
        last_trade = self._last_trade.get(symbol)
        if last_trade is None:
            return 0.0
        current = time.monotonic() if now is None else now
        return max(0.0, self.cooldown_seconds - (current - last_trade))
