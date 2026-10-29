"""Spot order construction and Testnet-only execution helpers."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from .safety import LIVE_SYMBOLS


def floor_step(value, step):
    value, step = Decimal(str(value)), Decimal(str(step))
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class SymbolRules:
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, payload, symbol):
        rows = payload.get("symbols", []) if isinstance(payload, dict) else []
        row = next((r for r in rows if r.get("symbol") == symbol), None)
        if row is None: raise ValueError("Symbol missing from Binance exchangeInfo")
        filters = {f["filterType"]: f for f in row.get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        price = filters.get("PRICE_FILTER", {})
        notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
        try:
            return cls(Decimal(price["tickSize"]), Decimal(lot["stepSize"]), Decimal(lot["minQty"]),
                       Decimal(notional["minNotional"]))
        except (KeyError, ArithmeticError):
            raise ValueError("Incomplete Binance trading filters") from None

    def limit_ioc(self, *, symbol, side, quantity, limit_price, reference_price,
                  max_order_quote, max_price_deviation):
        symbol, side = symbol.upper(), side.upper()
        if symbol not in LIVE_SYMBOLS or side not in {"BUY", "SELL"}:
            raise ValueError("Symbol or side is not allowed")
        price = floor_step(limit_price, self.tick_size)
        qty = floor_step(quantity, self.step_size)
        if qty < self.min_qty or qty * price < self.min_notional:
            raise ValueError("Order is below Binance minimum quantity or notional")
        if qty*price > Decimal(str(max_order_quote)):
            raise ValueError("Order exceeds configured max_order_quote")
        if abs(price/Decimal(str(reference_price))-1) > Decimal(str(max_price_deviation)):
            raise ValueError("Order price exceeds configured price deviation")
        return {"symbol":symbol,"side":side,"type":"LIMIT","time_in_force":"IOC",
                "quantity":format(qty,"f"),"price":format(price,"f")}


class BinanceTestnetOrderClient:
    """Only permits authenticated writes to Binance Spot Testnet."""
    def __init__(self, client, base_url):
        if base_url.rstrip("/") != "https://testnet.binance.vision":
            raise ValueError("Order submission is restricted to Binance Spot Testnet")
        self.client = client

    @staticmethod
    def client_order_id(strategy_version, symbol, timestamp, side):
        raw = f"{strategy_version}|{symbol}|{timestamp}|{side}".encode()
        return "q-" + hashlib.sha256(raw).hexdigest()[:32]

    def submit_ioc(self, order, *, strategy_version, timestamp):
        params = dict(order)
        params["new_client_order_id"] = self.client_order_id(strategy_version, order["symbol"], timestamp, order["side"])
        try:
            response = self.client.rest_api.new_order(**params)
            return response.data() if callable(getattr(response, "data", None)) else response
        except Exception as exc:
            # A timeout is ambiguous. Caller must query by the deterministic client order id before retry.
            raise RuntimeError(f"Testnet order result is uncertain ({type(exc).__name__}); reconcile before retry") from None
