#!/usr/bin/env python3
"""Credential-free REST and WebSocket connectivity preflight for Spot Testnet."""
from __future__ import annotations

import asyncio
import json
import urllib.request

import websockets

from quant.crypto.observer import SYMBOLS
from quant.crypto.testnet import REST, WS


async def check(symbol):
    query = f"symbol={symbol}&interval=1h&limit=3"
    try:
        with urllib.request.urlopen(f"{REST}/api/v3/klines?{query}", timeout=10) as response:
            bars = json.loads(response.read())
        rest_ok = len(bars) >= 2
    except Exception as exc:
        return {"symbol": symbol, "rest": False, "websocket": False,
                "error_type": type(exc).__name__}
    try:
        async with websockets.connect(f"{WS}/{symbol.lower()}@kline_1h", open_timeout=10,
                                      close_timeout=2, ping_interval=20) as socket:
            message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        ws_ok = message.get("e") == "kline" and message.get("s") == symbol
    except Exception as exc:
        return {"symbol": symbol, "rest": rest_ok, "websocket": False,
                "error_type": type(exc).__name__}
    return {"symbol": symbol, "rest": rest_ok, "websocket": ws_ok}


async def main():
    results = await asyncio.gather(*(check(symbol) for symbol in SYMBOLS))
    ok = all(row["rest"] and row["websocket"] for row in results)
    print(json.dumps({"environment": "spot_testnet", "ok": ok, "checks": results}, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
