"""Persistent, credential-free hourly observation. No execution client is imported."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

from .market import fetch_binance, validate_bars
from .strategy import target_weights

HOUR_MS = 3_600_000
SYMBOLS = ("BTCUSDT", "ETHUSDT")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SignalObserver:
    def __init__(self, path, *, fast=24, slow=72, now_ms=None):
        if not 2 <= fast < slow <= 999:
            raise ValueError("Require 2 <= fast < slow <= 999")
        self.fast, self.slow = fast, slow
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.version = f"sma-{fast}-{slow}-candidate-v2"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS observer_config(id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL, created_ms INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS observer_candles(symbol TEXT NOT NULL, ts INTEGER NOT NULL, candle TEXT NOT NULL, received_ms INTEGER NOT NULL, PRIMARY KEY(symbol,ts));
        CREATE TABLE IF NOT EXISTS observer_signals(symbol TEXT NOT NULL, ts INTEGER NOT NULL, strategy TEXT NOT NULL, target_weight REAL NOT NULL, close REAL NOT NULL, input_hash TEXT NOT NULL, recorded_ms INTEGER NOT NULL, PRIMARY KEY(symbol,ts));
        CREATE TABLE IF NOT EXISTS observer_health(symbol TEXT PRIMARY KEY, state TEXT NOT NULL, updated_ms INTEGER NOT NULL, error_type TEXT);
        CREATE TABLE IF NOT EXISTS observer_events(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, event TEXT NOT NULL, observed_ms INTEGER NOT NULL);
        """)
        implementation_hash = hashlib.sha256(
            Path(__file__).read_bytes() + Path(__file__).with_name("strategy.py").read_bytes()).hexdigest()
        settings = canonical({"fast": fast, "slow": slow, "symbols": SYMBOLS,
                              "strategy": self.version, "per_asset_target": 0.25,
                              "implementation_hash": implementation_hash, "mode": "signal_only"})
        saved = self.db.execute("SELECT settings FROM observer_config WHERE id=1").fetchone()
        if saved and saved[0] != settings:
            self.db.close()
            raise ValueError("Observer strategy/code changed; use a new observation database")
        self.db.execute("INSERT OR IGNORE INTO observer_config VALUES(1,?,?)", (settings, self.now_ms()))

    def close(self):
        self.db.close()

    def health(self, symbol, state, error_type=None):
        self.db.execute("INSERT INTO observer_health VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET state=excluded.state,updated_ms=excluded.updated_ms,error_type=excluded.error_type",
                        (symbol, state, self.now_ms(), error_type))
        if state in ("reconnecting", "blocked"):
            self.db.execute("INSERT INTO observer_events(symbol,event,observed_ms) VALUES(?,?,?)",
                            (symbol, canonical({"state": state, "error_type": error_type}), self.now_ms()))

    def last_timestamp(self, symbol):
        return self.db.execute("SELECT MAX(ts) FROM observer_candles WHERE symbol=?", (symbol,)).fetchone()[0]

    def ingest(self, symbol, rows):
        if symbol not in SYMBOLS:
            raise ValueError("Observer supports only BTCUSDT and ETHUSDT")
        if not rows:
            return {"symbol": symbol, "new_candles": 0, "new_signals": 0, "order_sent": False}
        validate_bars(rows)
        normalized = []
        for row in rows:
            bar = {"timestamp": int(row["timestamp"]), **{k: float(row[k]) for k in ("open", "high", "low", "close")},
                   "volume": float(row.get("volume", 0)), "closed": True}
            if not math.isfinite(bar["volume"]) or bar["volume"] < 0:
                raise ValueError("Invalid candle volume")
            if bar["timestamp"] + HOUR_MS > self.now_ms():
                raise ValueError("Unclosed candle cannot enter observation journal")
            normalized.append(bar)
        last_ts = self.last_timestamp(symbol)
        candles, signals = 0, 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for bar in normalized:
                ts, encoded = bar["timestamp"], canonical(bar)
                saved = self.db.execute("SELECT candle FROM observer_candles WHERE symbol=? AND ts=?", (symbol, ts)).fetchone()
                if saved:
                    if saved[0] != encoded:
                        raise ValueError("Conflicting candle replay; stop observation and investigate source")
                    continue
                if last_ts is not None and ts != last_ts + HOUR_MS:
                    raise ValueError("Missing hourly candles; REST backfill required")
                self.db.execute("INSERT INTO observer_candles VALUES(?,?,?,?)", (symbol, ts, encoded, self.now_ms()))
                history = [json.loads(r[0]) for r in self.db.execute(
                    "SELECT candle FROM observer_candles WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT ?",
                    (symbol, ts, self.slow))][::-1]
                candles += 1
                if len(history) == self.slow:
                    # Candidate long/cash signal maps to the agreed per-asset 25% cap.
                    weight = 0.25 * target_weights(history, self.fast, self.slow)[-1]
                    input_hash = hashlib.sha256(canonical({"version": self.version, "bars": history}).encode()).hexdigest()
                    self.db.execute("INSERT INTO observer_signals VALUES(?,?,?,?,?,?,?)",
                                    (symbol, ts, self.version, weight, bar["close"], input_hash, self.now_ms()))
                    signals += 1
                last_ts = ts
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"symbol": symbol, "new_candles": candles, "new_signals": signals, "order_sent": False}

    async def sync_symbol(self, symbol, *, fetch=fetch_binance):
        """Fill all missing closed candles; page through outages longer than 1000 hours."""
        cutoff = (self.now_ms() // HOUR_MS) * HOUR_MS - 1
        totals = {"symbol": symbol, "new_candles": 0, "new_signals": 0, "order_sent": False}
        self.health(symbol, "syncing")
        while True:
            last = self.last_timestamp(symbol)
            start = last + HOUR_MS if last is not None else None
            if start is not None and start > cutoff:
                break
            rows = await asyncio.to_thread(fetch, symbol, limit=1000, start=start, end=cutoff, allow_empty=True)
            if not rows:
                break
            result = self.ingest(symbol, rows)
            totals["new_candles"] += result["new_candles"]
            totals["new_signals"] += result["new_signals"]
            if self.last_timestamp(symbol) == last:
                raise ValueError("Backfill did not advance; refusing to report healthy")
        expected = cutoff + 1 - HOUR_MS
        if self.last_timestamp(symbol) != expected:
            self.health(symbol, "blocked", "MissingClosedCandles")
            raise ValueError("Binance has not provided the latest closed hour")
        self.health(symbol, "synced")
        return totals

    def status(self):
        return self._status(self.db, self.now_ms())

    @staticmethod
    def read_status(path):
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError("Observation database does not exist; run crypto watch --once first")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return SignalObserver._status(db, int(time.time()*1000))

    @staticmethod
    def _status(db, now_ms):
        config = db.execute("SELECT * FROM observer_config WHERE id=1").fetchone()
        symbols = []
        expected = (now_ms // HOUR_MS)*HOUR_MS-HOUR_MS
        for symbol in SYMBOLS:
            last = db.execute("SELECT MAX(ts) FROM observer_candles WHERE symbol=?", (symbol,)).fetchone()[0]
            signal = db.execute("SELECT * FROM observer_signals WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)).fetchone()
            health = db.execute("SELECT * FROM observer_health WHERE symbol=?", (symbol,)).fetchone()
            symbols.append({"symbol": symbol, "latest_hour_open_ms": last,
                            "data_current": last == expected,
                            "candles": db.execute("SELECT COUNT(*) FROM observer_candles WHERE symbol=?", (symbol,)).fetchone()[0],
                            "signals": db.execute("SELECT COUNT(*) FROM observer_signals WHERE symbol=?", (symbol,)).fetchone()[0],
                            "latest_signal": dict(signal) if signal else None,
                            "health": dict(health) if health else None,
                            "stream_heartbeat_recent": bool(health and health["state"] == "streaming"
                                                            and 0 <= now_ms-health["updated_ms"] <= 90_000)})
        return {"mode": "signal_only", "order_submission": False, "checked_ms": now_ms,
                "service_running": all(s["stream_heartbeat_recent"] for s in symbols),
                "created_ms": config["created_ms"], "strategy": json.loads(config["settings"]),
                "symbols": symbols, "note": "Historical backfill does not count as live observation days"}
