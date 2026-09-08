"""CCXT-shaped adapter for the official NonKYC v2 REST API.

NonKYC is not distributed by CCXT. This adapter implements only the spot
methods used by this application and follows NonKYC's official Python client.
"""

import asyncio
from decimal import Decimal, ROUND_DOWN
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp


class NonKycError(RuntimeError):
    pass


class NonKycExchange:
    id = "nonkyc"
    name = "NonKYC"
    api_url = "https://api.nonkyc.io/api/v2"
    error_name = "NonKYC"

    def __init__(self, options: dict[str, Any]):
        self.api_key = options.get("apiKey", "")
        self.secret = options.get("secret", "")
        self.timeout = options.get("timeout", 5_000) / 1_000
        self.enable_rate_limit = options.get("enableRateLimit", True)
        self.rate_limit = 0.2
        self._last_request = 0.0
        self._request_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self.markets: dict[str, dict[str, Any]] = {}
        self.has = {
            "fetchOrderBook": True,
            "fetchBalance": True,
            "createOrder": True,
            "fetchOrder": True,
            "cancelOrder": True,
        }

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    async def _throttle(self):
        if not self.enable_rate_limit:
            return
        async with self._request_lock:
            delay = self.rate_limit - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    def _headers(self, payload: str) -> dict[str, str]:
        if not self.api_key or not self.secret:
            raise NonKycError("NonKYC private endpoint requires API key and secret")
        nonce = str(int(time.time() * 1_000))
        signature = hmac.new(self.secret.encode(), f"{self.api_key}{payload}{nonce}".encode(), hashlib.sha256).hexdigest()
        return {"X-API-KEY": self.api_key, "X-API-NONCE": nonce, "X-API-SIGN": signature, "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None, private: bool = False, base_url: str | None = None):
        await self._throttle()
        query = f"?{urlencode(params)}" if params else ""
        encoded = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{base_url or self.api_url}{path}{query}"
        headers = self._headers(f"{url}{encoded}") if private else {}
        async with self._get_session().request(method, url, data=encoded or None, headers=headers) as response:
            text = await response.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise NonKycError(f"NonKYC returned non-JSON HTTP {response.status}") from exc
            if response.status >= 400:
                raise NonKycError(f"{self.error_name} HTTP {response.status}: {data}")
            if isinstance(data, dict) and data.get("error"):
                raise NonKycError(f"{self.error_name} error: {data['error']}")
            return data

    async def load_markets(self):
        raw_markets = await self._request("GET", "/market/getlist")
        raw_markets = raw_markets.get("data", raw_markets) if isinstance(raw_markets, dict) else raw_markets
        markets = {}
        for raw in raw_markets:
            if not raw.get("isActive", False) or raw.get("isPaused", False):
                continue
            symbol = raw["symbol"].upper()
            base, quote = symbol.split("/")
            markets[symbol] = {
                "id": symbol.replace("/", "_"),
                "symbol": symbol,
                "base": base,
                "quote": quote,
                "spot": True,
                "active": True,
                "precision": {"amount": raw.get("quantityDecimals", 8), "price": raw.get("priceDecimals", 8)},
                "limits": {
                    "amount": {"min": raw.get("minimumQuantity")},
                    "cost": {"min": raw.get("minimumQuote") or raw.get("minQuote")},
                },
                "info": raw,
            }
        self.markets = markets
        return markets

    def market(self, symbol: str) -> dict[str, Any]:
        return self.markets[symbol]

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        return Decimal(str(value))

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        digits = self.market(symbol)["precision"]["amount"]
        quantum = Decimal(1).scaleb(-digits)
        return format(self._decimal(amount).quantize(quantum, rounding=ROUND_DOWN), "f")

    def price_to_precision(self, symbol: str, price: float) -> str:
        digits = self.market(symbol)["precision"]["price"]
        quantum = Decimal(1).scaleb(-digits)
        return format(self._decimal(price).quantize(quantum, rounding=ROUND_DOWN), "f")

    async def fetch_order_book(self, symbol: str, limit: int | None = None):
        data = await self._request("GET", f"/market/getorderbookbysymbol/{self.market(symbol)['id']}")
        bids = [[level["price"], level["quantity"]] for level in data.get("bids", [])]
        asks = [[level["price"], level["quantity"]] for level in data.get("asks", [])]
        return {"symbol": symbol, "timestamp": data.get("timestamp"), "bids": bids[:limit], "asks": asks[:limit], "info": data}

    async def fetch_balance(self):
        data = await self._request("GET", "/balances", private=True)
        balances = data.get("data", data) if isinstance(data, dict) else data
        result: dict[str, Any] = {"info": data}
        for balance in balances:
            currency = balance.get("ticker") or balance.get("symbol") or balance.get("assetTicker")
            if not currency:
                continue
            free = self._decimal(balance.get("available", 0))
            used = self._decimal(balance.get("held", 0))
            result[currency.upper()] = {"free": float(free), "used": float(used), "total": float(free + used)}
        return result

    @staticmethod
    def _id(data: dict) -> str:
        result = data.get("result", data)
        order_id = result.get("id") or result.get("_id") or result.get("orderId")
        if not order_id:
            raise NonKycError(f"NonKYC response has no order id: {data}")
        return str(order_id)

    def _parse_order(self, raw: dict, symbol: str | None = None) -> dict:
        data = raw.get("result", raw)
        status = str(data.get("status", "open")).lower()
        status = {"active": "open", "filled": "closed", "cancelled": "canceled"}.get(status, status)
        filled = data.get("executedQuantity", data.get("filled", 0))
        amount = data.get("quantity", data.get("originalQuantity"))
        return {"id": str(data.get("id") or data.get("_id") or data.get("orderId")), "symbol": symbol, "status": status, "side": data.get("side"), "price": data.get("price"), "amount": amount, "filled": filled, "info": raw}

    async def create_order(self, symbol: str, order_type: str, side: str, amount: float, price: float | None = None, params: dict | None = None):
        params = params or {}
        if order_type not in {"limit", "market"}:
            raise NonKycError(f"unsupported order type {order_type}")
        if order_type == "limit" and price is None:
            raise NonKycError("limit order requires price")
        body = {
            "symbol": self.market(symbol)["id"], "side": side, "type": order_type,
            "quantity": self.amount_to_precision(symbol, amount), "strictValidate": True,
            "userProvidedId": params.get("clientOrderId") or params.get("newClientOrderId"),
        }
        if price is not None:
            body["price"] = self.price_to_precision(symbol, price)
        body = {key: value for key, value in body.items() if value is not None}
        created = await self._request("POST", "/createorder", body=body, private=True)
        order_id = self._id(created)
        if params.get("timeInForce") == "IOC":
            # NonKYC REST has no native IOC. Its official example creates then cancels.
            await self.cancel_order(order_id, symbol)
        return await self.fetch_order(order_id, symbol)

    async def fetch_order(self, order_id: str, symbol: str | None = None):
        data = await self._request("GET", f"/getorder/{order_id}", private=True)
        return self._parse_order(data, symbol)

    async def cancel_order(self, order_id: str, symbol: str | None = None):
        data = await self._request("POST", "/cancel_order", body={"id": order_id}, private=True)
        return self._parse_order(data, symbol)
