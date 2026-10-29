"""Credential-free deployment preflight. No private Binance endpoints are called."""
from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets

from quant.crypto.market import fetch_binance
from quant.crypto.monitor import http_status
from quant.crypto.observer import HOUR_MS, SYMBOLS


def failure(channel, symbol, exc):
    return {"channel":channel, "symbol":symbol, "ok":False,
            "error_type":type(exc).__name__, "http_status":http_status(exc)}


async def check_symbol(symbol):
    results = []
    try:
        rows = await asyncio.to_thread(fetch_binance, symbol, limit=3, timeout=8)
        expected = (int(time.time()*1000)//HOUR_MS)*HOUR_MS-HOUR_MS
        results.append({"channel":"rest", "symbol":symbol,
                        "ok":rows[-1]["timestamp"] == expected,
                        "last_hour_open_ms":rows[-1]["timestamp"]})
    except Exception as exc:
        results.append(failure("rest", symbol, exc))
    # A restricted/banned response must stop here, not trigger an alternate endpoint.
    if any(r.get("http_status") in (403,418,429,451) for r in results):
        return results
    try:
        async with websockets.connect(f"wss://stream.binance.com:9443/ws/{symbol.lower()}@kline_1h",
                                      open_timeout=8, close_timeout=2, ping_interval=None) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            payload = json.loads(raw)
            valid = payload.get("s") == symbol and payload.get("k", {}).get("i") == "1h"
            results.append({"channel":"websocket", "symbol":symbol, "ok":valid})
    except Exception as exc:
        results.append(failure("websocket", symbol, exc))
    return results


async def main():
    batches = await asyncio.gather(*(check_symbol(symbol) for symbol in SYMBOLS))
    results = [row for batch in batches for row in batch]
    ok = len(results) == 4 and all(row["ok"] for row in results)
    blocked = any(row.get("http_status") in (403,418,451) for row in results)
    print(json.dumps({"ok":ok, "mode":"signal_only", "order_submission":False,
                      "checks":results, "access_blocked":blocked}, ensure_ascii=False), flush=True)
    return 0 if ok else 2 if blocked else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
