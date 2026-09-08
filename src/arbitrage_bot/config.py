from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))

def _optional_float(name: str) -> float | None:
    value = os.getenv(name, "").strip()
    return float(value) if value else None

@dataclass(frozen=True)
class Settings:
    live_trading: bool = os.getenv("LIVE_TRADING", "false").lower() == "true"
    symbols: tuple[str, ...] = tuple(s.strip().upper() for s in os.getenv("SYMBOLS", "ALL").split(",") if s.strip())
    quote_currency: str = os.getenv("QUOTE_CURRENCY", "USDT").upper()
    max_symbols: int = int(os.getenv("MAX_SYMBOLS", "0"))
    max_notional_quote: float = _float("MAX_NOTIONAL_QUOTE", 100)
    max_slice_notional: float = _float("MAX_SLICE_NOTIONAL", 10)
    max_session_notional: float = _float("MAX_SESSION_NOTIONAL", 100)
    max_total_session_notional: float = _float("MAX_TOTAL_SESSION_NOTIONAL", 100)
    min_net_edge_bps: float = _float("MIN_NET_EDGE_BPS", 35)
    max_net_edge_bps: float = _float("MAX_NET_EDGE_BPS", 5000)
    poll_interval_seconds: float = _float("POLL_INTERVAL_SECONDS", 3)
    orderbook_limit: int = int(os.getenv("ORDERBOOK_LIMIT", "10"))
    mexc_fee_bps: float | None = _optional_float("MEXC_FEE_BPS")
    nonkyc_fee_bps: float = _float("NONKYC_FEE_BPS", 20)
    auto_fetch_mexc_fee: bool = os.getenv("AUTO_FETCH_MEXC_FEE", "true").lower() == "true"
    slippage_buffer_bps: float = _float("SLIPPAGE_BUFFER_BPS", 10)
    timeout_seconds: float = _float("REQUEST_TIMEOUT_SECONDS", 5)
    max_open_orders: int = int(os.getenv("MAX_OPEN_ORDERS", "2"))
    order_timeout_seconds: float = _float("ORDER_TIMEOUT_SECONDS", 2)
    max_daily_loss_quote: float = _float("MAX_DAILY_LOSS_QUOTE", 25)
    mexc_api_key: str = os.getenv("MEXC_API_KEY", "")
    mexc_secret: str = os.getenv("MEXC_SECRET", "")
    nonkyc_api_key: str = os.getenv("NONKYC_API_KEY", "")
    nonkyc_secret: str = os.getenv("NONKYC_SECRET", "")
    max_book_age_seconds: float = _float("MAX_BOOK_AGE_SECONDS", 2)
    max_hedge_slippage_bps: float = _float("MAX_HEDGE_SLIPPAGE_BPS", 75)
    protective_price_level: int = int(os.getenv("PROTECTIVE_PRICE_LEVEL", "3"))
    trade_cooldown_seconds: float = _float("TRADE_COOLDOWN_SECONDS", 30)
    rearm_edge_bps: float = _float("REARM_EDGE_BPS", 10)
    pair: str = os.getenv("ARB_PAIR", "mexc-nonkyc").lower()
    xeggex_fee_bps: float = _float("XEGGEX_FEE_BPS", 20)
    xeggex_api_key: str = os.getenv("XEGGEX_API_KEY", "")
    xeggex_secret: str = os.getenv("XEGGEX_SECRET", "")
