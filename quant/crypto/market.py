from __future__ import annotations

import csv
import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


def normalize_kline(row):
    return {"timestamp": int(row[0]), "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
            "close_time": int(row[6]), "quote_volume": float(row[7]),
            "trades": int(row[8]), "closed": True}


def read_csv(path: str | Path):
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row.get("timestamp") or row.get("open_time") or row.get("time")
            try:
                n = int(ts)
            except (TypeError, ValueError):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                n = int(dt.astimezone(timezone.utc).timestamp() * 1000)
            rows.append({"timestamp": n, "open": float(row["open"]), "high": float(row["high"]),
                         "low": float(row["low"]), "close": float(row["close"]),
                         "volume": float(row.get("volume", 0) or 0), "closed": True})
    validate_bars(rows)
    return rows


def validate_bars(rows):
    if not rows:
        raise ValueError("No candle data")
    prior = None
    for r in rows:
        if any(not isinstance(r[k], (int, float)) or not math.isfinite(r[k]) for k in ("timestamp", "open", "high", "low", "close")):
            raise ValueError("Candle fields must be numeric")
        if r["timestamp"] % 3_600_000 or r.get("closed") is False:
            raise ValueError("Require aligned, closed hourly candles")
        if min(r["open"], r["high"], r["low"], r["close"]) <= 0 or r["low"] > min(r["open"], r["close"]) or r["high"] < max(r["open"], r["close"]):
            raise ValueError("Invalid OHLC candle")
        if prior is not None and (r["timestamp"] <= prior or r["timestamp"] - prior != 3_600_000):
            raise ValueError("Candles must be sorted, unique, and contiguous hourly bars")
        prior = r["timestamp"]


def fetch_binance(symbol="BTCUSDT", limit=1000, start=None, end=None, timeout=15, *, allow_empty=False):
    if not symbol.isalnum() or not 1 <= limit <= 1000:
        raise ValueError("Invalid symbol or limit")
    query = {"symbol": symbol.upper(), "interval": "1h", "limit": limit}
    if start is not None: query["startTime"] = int(start)
    if end is not None: query["endTime"] = int(end)
    req = urllib.request.Request(BINANCE_KLINES + "?" + urllib.parse.urlencode(query),
                                 headers={"User-Agent": "QuantCryptoPaper/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = json.loads(response.read())
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = [normalize_kline(x) for x in raw if int(x[6]) < now_ms]
    if rows or not allow_empty:
        validate_bars(rows)
    return rows


class BinanceSpotReadOnly:
    """Public market data only. Authenticated account and order methods are intentionally absent."""
    fetch_klines = staticmethod(fetch_binance)

    async def stream_closed_hourly(self, symbol="BTCUSDT"):
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install the optional crypto extra to use Binance WebSocket streaming") from exc
        url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@kline_1h"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            async for message in ws:
                payload = json.loads(message)["k"]
                if payload["x"]:
                    yield {"timestamp": int(payload["t"]), "open": float(payload["o"]),
                           "high": float(payload["h"]), "low": float(payload["l"]),
                           "close": float(payload["c"]), "volume": float(payload["v"]), "closed": True}

