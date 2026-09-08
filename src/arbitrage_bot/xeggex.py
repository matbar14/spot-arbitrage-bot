from typing import Any
import asyncio
import base64

from .nonkyc import NonKycExchange, NonKycError


class XeggexExchange(NonKycExchange):
    """CCXT-shaped adapter for the official XeggeX v2 REST API."""

    id = "xeggex"
    name = "XeggeX"
    api_url = "https://api.xeggex.com/api/v2"
    public_api_url = "https://xeggex.com/api/v2"
    error_name = "XeggeX"

    def _headers(self, payload: str) -> dict[str, str]:
        if not self.api_key or not self.secret:
            raise NonKycError("XeggeX private endpoint requires API key and secret")
        token = base64.b64encode(f"{self.api_key}:{self.secret}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    async def cancel_order(self, order_id: str, symbol: str | None = None):
        data = await self._request("POST", "/cancelorder", body={"id": order_id}, private=True)
        return self._parse_order(data, symbol)

    async def fetch_order_book(self, symbol: str, limit: int | None = None):
        buy_data, sell_data = await asyncio.gather(
            self._request("GET", "/market/obbuys", params={"symbol": symbol, "limit": limit or 50}, base_url=self.public_api_url),
            self._request("GET", "/market/obsells", params={"symbol": symbol, "limit": limit or 50}, base_url=self.public_api_url),
        )
        bids = [[level["price"], level["quantity"]] for level in buy_data]
        asks = [[level["price"], level["quantity"]] for level in sell_data]
        return {"symbol": symbol, "timestamp": None, "bids": bids[:limit], "asks": asks[:limit], "info": {"bids": buy_data, "asks": sell_data}}

    async def load_markets(self):
        await super().load_markets()
        for market in self.markets.values():
            market["id"] = market["symbol"]
        return self.markets

    async def fetch_order(self, order_id: str, symbol: str | None = None):
        for status in ("active", "filled", "cancelled"):
            params = {"status": status, "limit": 50, "skip": 0}
            if symbol:
                params["symbol"] = symbol
            data = await self._request("GET", "/getorders", params=params, private=True)
            rows = data.get("data", data) if isinstance(data, dict) else data
            for row in rows or []:
                row_id = row.get("id") or row.get("_id") or row.get("orderId")
                if str(row_id) == str(order_id):
                    return self._parse_order(row, symbol)
        raise NonKycError(f"XeggeX order {order_id} was not found in order history")
