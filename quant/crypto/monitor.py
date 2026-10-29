"""Public WebSocket watcher backed by an immutable hourly observation journal."""
from __future__ import annotations

import asyncio
import json

from .observer import HOUR_MS, SYMBOLS, SignalObserver


def emit_event(event):
    print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)


def http_status(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) or getattr(exc, "code", None)


async def watch_symbols(observer: SignalObserver, *, reconnect_seconds=3, once=False):
    if once:
        for symbol in SYMBOLS:
            try:
                emit_event(await observer.sync_symbol(symbol))
            except Exception as exc:
                code = http_status(exc)
                observer.health(symbol, "blocked", f"HTTP{code}" if code else type(exc).__name__)
                raise
        emit_event(observer.status())
        return
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("Install the crypto extra for Binance WebSocket watching") from exc

    async def consume(symbol):
        failures = 0
        while True:
            try:
                # Start the stream first so its buffer covers the REST synchronization window.
                url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@kline_1h"
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              open_timeout=15, close_timeout=3) as ws:
                    emit_event(await observer.sync_symbol(symbol))
                    observer.health(symbol, "streaming")
                    while True:
                        message = await asyncio.wait_for(ws.recv(), timeout=90)
                        k = json.loads(message)["k"]
                        failures = 0
                        observer.health(symbol, "streaming")
                        if not k["x"]:
                            continue
                        bar = {"timestamp":int(k["t"]),"open":float(k["o"]),"high":float(k["h"]),
                               "low":float(k["l"]),"close":float(k["c"]),"volume":float(k["v"]),"closed":True}
                        last = observer.last_timestamp(symbol)
                        if last is None or bar["timestamp"] > last + HOUR_MS:
                            emit_event(await observer.sync_symbol(symbol))
                        result = observer.ingest(symbol, [bar])
                        if result["new_candles"]:
                            emit_event(result)
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                observer.health(symbol, "blocked", type(exc).__name__)
                emit_event({"symbol":symbol,"state":"blocked","error_type":type(exc).__name__})
                raise
            except Exception as exc:
                code = http_status(exc)
                error_type = f"HTTP{code}" if code else type(exc).__name__
                if code in (403, 418, 451):
                    observer.health(symbol, "blocked", error_type)
                    emit_event({"symbol":symbol, "state":"blocked", "http_status":code,
                                "error_type":type(exc).__name__})
                    raise ValueError(f"Market access blocked (HTTP {code}); automatic retries stopped") from None
                failures += 1
                delay = min(60, reconnect_seconds * 2**min(failures-1, 6))
                if code == 429:
                    headers = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None)
                    try:
                        retry_after = float(headers.get("Retry-After", "60")) if headers else 60
                    except (ValueError, TypeError):
                        retry_after = 60
                    delay = max(delay, 60, retry_after)
                observer.health(symbol, "reconnecting", error_type)
                emit_event({"symbol":symbol,"state":"reconnecting","error_type":type(exc).__name__,
                            "http_status":code,"retry_seconds":delay})
                await asyncio.sleep(delay)

    tasks = [asyncio.create_task(consume(symbol)) for symbol in SYMBOLS]
    try:
        await asyncio.gather(*tasks)
    finally:
        # Finish both workers before the CLI closes their shared SQLite connection.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for symbol in SYMBOLS:
            health = observer.db.execute("SELECT state FROM observer_health WHERE symbol=?", (symbol,)).fetchone()
            if health is None or health[0] != "blocked":
                observer.health(symbol, "stopped")
