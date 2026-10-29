"""Fail-closed Binance Spot Testnet hourly strategy executor.

This module is deliberately pinned to the Spot Testnet host and refuses live URLs.
It is a systems acceptance harness, not a production/live trading implementation.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path

from .market import normalize_kline
from .observer import HOUR_MS, SYMBOLS
from .orders import BinanceTestnetOrderClient, SymbolRules
from .strategy import target_weights

REST = "https://testnet.binance.vision"
WS = "wss://stream.testnet.binance.vision/ws"
ASSETS = {"BTCUSDT": ("BTC", "USDT"), "ETHUSDT": ("ETH", "USDT")}


def _plain(value):
    if hasattr(value, "data") and callable(value.data):
        value = value.data()
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, list):
        return [_plain(x) for x in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def make_testnet_client(api_key=None, api_secret=None):
    api_key = api_key if api_key is not None else os.environ.get("BINANCE_TESTNET_API_KEY", "")
    api_secret = api_secret if api_secret is not None else os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    if not api_key or not api_secret:
        raise ValueError("Set separate Binance Spot Testnet API credentials on the server")
    from binance_common.configuration import ConfigurationRestAPI
    from binance_common.constants import SPOT_REST_API_TESTNET_URL
    from binance_sdk_spot.spot import Spot
    config = ConfigurationRestAPI(api_key=api_key, api_secret=api_secret,
                                  base_path=SPOT_REST_API_TESTNET_URL, timeout=10_000)
    return Spot(config_rest_api=config)


def fetch_testnet_klines(symbol, limit=1000):
    if symbol not in SYMBOLS or not 1 <= limit <= 1000:
        raise ValueError("Invalid Testnet market data request")
    query = urllib.parse.urlencode({"symbol": symbol, "interval": "1h", "limit": limit})
    request = urllib.request.Request(f"{REST}/api/v3/klines?{query}",
                                     headers={"User-Agent": "QuantSpotTestnet/0.1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = json.loads(response.read())
    now = int(time.time() * 1000)
    return [normalize_kline(row) for row in raw if int(row[6]) < now]


class TestnetTrader:
    """Persistent one-signal/one-order journal; every uncertain state locks trading."""
    __test__ = False

    def __init__(self, db_path, client, *, fast=24, slow=72, max_order_quote=25,
                 max_daily_loss_quote=10, max_price_deviation=0.005,
                 now_ms=None, enabled=False, confirmation="", adaptive_profiles=False):
        if not enabled or confirmation != "I_ACCEPT_TESTNET_ORDERS":
            raise ValueError("Testnet orders require --enable-testnet and explicit confirmation")
        if not (2 <= fast < slow <= 999):
            raise ValueError("Require 2 <= fast < slow <= 999")
        if min(float(max_order_quote), float(max_daily_loss_quote)) <= 0:
            raise ValueError("Testnet order and daily loss limits must be positive")
        if not 0 < float(max_price_deviation) <= 0.02:
            raise ValueError("Testnet price deviation must be in (0, 0.02]")
        self.client = client
        self.orders = BinanceTestnetOrderClient(client, REST)
        self.fast, self.slow = fast, slow
        self.adaptive_profiles = adaptive_profiles
        self.max_order_quote = Decimal(str(max_order_quote))
        self.max_daily_loss_quote = Decimal(str(max_daily_loss_quote))
        self.max_price_deviation = Decimal(str(max_price_deviation))
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS bot_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_signals(symbol TEXT NOT NULL,ts INTEGER NOT NULL,weight REAL NOT NULL,close REAL NOT NULL,PRIMARY KEY(symbol,ts));
        CREATE TABLE IF NOT EXISTS bot_orders(client_id TEXT PRIMARY KEY,symbol TEXT NOT NULL,ts INTEGER NOT NULL,side TEXT NOT NULL,request TEXT NOT NULL,response TEXT,status TEXT NOT NULL,updated_ms INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER NOT NULL,event TEXT NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO bot_state VALUES('lock','')")
        strategy = ("adaptive-approved-profiles-v1" if adaptive_profiles
                    else f"sma-{fast}-{slow}-candidate-v2")
        self.db.execute("INSERT OR IGNORE INTO bot_state VALUES('strategy',?)",
                        (json.dumps({"fast": fast, "slow": slow, "symbols": SYMBOLS,
                                     "per_asset_cap": .25, "portfolio_cap": .50,
                                     "strategy": strategy}, sort_keys=True),))
        stored = json.loads(self.db.execute("SELECT value FROM bot_state WHERE key='strategy'").fetchone()[0])
        expected = {"fast": fast, "slow": slow, "symbols": list(SYMBOLS),
                    "per_asset_cap": .25, "portfolio_cap": .50,
                    "strategy": strategy}
        if stored != expected:
            self.db.close()
            raise ValueError("Testnet strategy/config changed; start a new database")
        self._recover()

    def close(self):
        self.db.close()

    def _event(self, name, **data):
        self.db.execute("INSERT INTO bot_events(ts,event) VALUES(?,?)",
                        (self.now_ms(), json.dumps({"name": name, **data}, sort_keys=True)))

    def _lock(self, reason):
        self.halt(reason)
        raise RuntimeError(f"Testnet trading locked: {reason}")

    def _set_persistent_lock(self, reason):
        current = self.db.execute("SELECT value FROM bot_state WHERE key='lock'").fetchone()[0]
        if not current:
            self.db.execute("UPDATE bot_state SET value=? WHERE key='lock'", (reason,))
            self._event("locked", reason=reason)

    def halt(self, reason="manual_stop"):
        self.db.execute("UPDATE bot_state SET value=? WHERE key='lock'", (reason,))
        self._event("locked", reason=reason)

    def reset_manual_halt(self):
        reason = self.db.execute("SELECT value FROM bot_state WHERE key='lock'").fetchone()[0]
        if reason != "manual_shutdown":
            raise ValueError("Only a manual_shutdown lock can be reset by this command")
        open_orders = _plain(self.client.rest_api.get_open_orders())
        if not isinstance(open_orders, list) or open_orders:
            raise ValueError("Open orders exist or could not be checked; lock remains active")
        balances = self._account()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE bot_state SET value='' WHERE key='lock'")
            self._event("manual_unlock_after_reconciliation", open_orders=0,
                        balance_assets=sorted(balances))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"locked": False, "open_orders": 0,
                "balance_assets_checked": sorted(balances), "orders_submitted": False}

    def reset_daily_loss_lock_after_review(self):
        reason = self.db.execute("SELECT value FROM bot_state WHERE key='lock'").fetchone()[0]
        if reason != "daily_loss_limit":
            raise ValueError("Only a daily_loss_limit lock can be reset by this review command")
        open_orders = _plain(self.client.rest_api.get_open_orders())
        if not isinstance(open_orders, list) or open_orders:
            raise ValueError("Open orders exist or could not be checked; lock remains active")
        balances = self._account()
        prices = {symbol: self._price(symbol) for symbol in SYMBOLS}
        equity = balances.get("USDT", (Decimal(0), Decimal(0)))[0]
        for symbol, (base, _) in ASSETS.items():
            equity += sum(balances.get(base, (Decimal(0), Decimal(0)))) * prices[symbol]
        day_key = f"day:{self.now_ms() // 86_400_000}"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE bot_state SET value='' WHERE key='lock'")
            self.db.execute("INSERT INTO bot_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (day_key, str(equity)))
            self._event("daily_loss_unlock_after_human_review", open_orders=0,
                        equity_quote=str(equity), balance_assets=sorted(balances))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"locked": False, "lock_reset": "daily_loss_limit",
                "open_orders": 0, "reviewed_equity_quote": str(equity),
                "orders_submitted": False}

    def _recover(self):
        lock = self.db.execute("SELECT value FROM bot_state WHERE key='lock'").fetchone()[0]
        if lock and lock != "daily_loss_limit":
            return
        try:
            for row in self.db.execute("SELECT client_id,symbol FROM bot_orders WHERE status IN ('SUBMITTING','UNKNOWN')").fetchall():
                result = _plain(self.client.rest_api.get_order(symbol=row[1], orig_client_order_id=row[0]))
                self._save_result(row[0], result)
            open_orders = _plain(self.client.rest_api.get_open_orders())
            if open_orders:
                self._lock("open_orders_at_startup")
        except RuntimeError:
            raise
        except Exception:
            self._lock("startup_reconciliation_failed")

    def _save_result(self, client_id, result):
        status = str(result.get("status", "UNKNOWN")) if isinstance(result, dict) else "UNKNOWN"
        encoded = json.dumps(result, sort_keys=True, default=str)
        self.db.execute("UPDATE bot_orders SET response=?,status=?,updated_ms=? WHERE client_id=?",
                        (encoded, status, self.now_ms(), client_id))
        if status in ("UNKNOWN", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"):
            self._lock("order_state_uncertain")

    def _account(self):
        result = _plain(self.client.rest_api.get_account(omit_zero_balances=True))
        if not isinstance(result, dict) or not isinstance(result.get("balances"), list):
            self._lock("invalid_account_response")
        return {row["asset"]: (Decimal(str(row["free"])), Decimal(str(row["locked"])))
                for row in result["balances"]}

    def _rules(self, symbol):
        result = _plain(self.client.rest_api.exchange_info(symbol=symbol))
        return SymbolRules.from_exchange_info(result, symbol)

    def _price(self, symbol):
        result = _plain(self.client.rest_api.ticker_price(symbol=symbol))
        try:
            price = Decimal(str(result["price"]))
        except (KeyError, TypeError, ValueError):
            self._lock("invalid_ticker_response")
        if not price.is_finite() or price <= 0:
            self._lock("invalid_ticker_price")
        return price

    def process_closed_bar(self, symbol, bars, *, profile_id=None):
        if symbol not in SYMBOLS:
            self._lock("symbol_not_whitelisted")
        if len(bars) < self.slow:
            return {"symbol": symbol, "state": "warming_up", "order_sent": False}
        bar = bars[-1]
        ts = int(bar["timestamp"])
        if ts + HOUR_MS > self.now_ms():
            self._lock("unclosed_candle")
        if self.db.execute("SELECT 1 FROM bot_signals WHERE symbol=? AND ts=?", (symbol, ts)).fetchone():
            return {"symbol": symbol, "state": "duplicate_ignored", "order_sent": False}
        lock_reason = self.db.execute("SELECT value FROM bot_state WHERE key='lock'").fetchone()[0]
        if lock_reason and lock_reason != "daily_loss_limit":
            raise RuntimeError(f"Testnet trading locked: manual_or_prior_lock ({lock_reason})")
        if self.adaptive_profiles:
            if profile_id not in ("sma_24_72", "cash"):
                self._lock("invalid_adaptive_profile")
            if (self.fast, self.slow) != (24, 72):
                self._lock("adaptive_profile_parameters_changed")
            weight = (.25 * target_weights(bars[-72:], 24, 72)[-1]
                      if profile_id == "sma_24_72" else 0.0)
        else:
            profile_id = f"sma_{self.fast}_{self.slow}"
            weight = .25 * target_weights(bars[-self.slow:], self.fast, self.slow)[-1]
        account = self._account()
        prices = {s: self._price(s) for s in SYMBOLS}
        equity = account.get("USDT", (Decimal(0), Decimal(0)))[0]
        for s, (base, _) in ASSETS.items():
            equity += sum(account.get(base, (Decimal(0), Decimal(0)))) * prices[s]
        day = self.now_ms() // 86_400_000
        key = f"day:{day}"
        start = self.db.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        if start is None:
            self.db.execute("INSERT INTO bot_state VALUES(?,?)", (key, str(equity)))
        elif Decimal(start[0]) - equity >= self.max_daily_loss_quote:
            self._set_persistent_lock("daily_loss_limit")
        reduce_only = lock_reason == "daily_loss_limit" or self.db.execute(
            "SELECT value FROM bot_state WHERE key='lock'").fetchone()[0] == "daily_loss_limit"
        base, quote = ASSETS[symbol]
        price = prices[symbol]
        held = sum(account.get(base, (Decimal(0), Decimal(0))))
        target = equity * Decimal(str(weight)) / price
        if reduce_only:
            target = min(target, held)
        delta = target - held
        side = "BUY" if delta > 0 else "SELL"
        limit_price = price * (Decimal(1) + self.max_price_deviation if side == "BUY"
                               else Decimal(1) - self.max_price_deviation)
        max_qty = self.max_order_quote / limit_price
        qty = min(abs(delta), max_qty)
        if side == "BUY":
            other_value = sum(sum(account.get(ASSETS[s][0], (Decimal(0), Decimal(0)))) * prices[s]
                              for s in SYMBOLS if s != symbol)
            aggregate_room = max(Decimal(0), equity * Decimal("0.50") - other_value - held * price)
            asset_room = max(Decimal(0), equity * Decimal("0.25") - held * price)
            qty = min(qty, aggregate_room / price, asset_room / price)
            quote_free = account.get(quote, (Decimal(0), Decimal(0)))[0]
            qty = min(qty, quote_free * Decimal("0.999") / limit_price)
        if side == "SELL":
            qty = min(qty, account.get(base, (Decimal(0), Decimal(0)))[0])
        self._event("risk_check", symbol=symbol, profile_id=profile_id,
                    passed=True, target_weight=weight, reduce_only=reduce_only,
                    max_order_quote=str(self.max_order_quote),
                    max_daily_loss_quote=str(self.max_daily_loss_quote),
                    max_price_deviation=str(self.max_price_deviation),
                    order_quantity=str(qty))
        request = None
        response = None
        status = "NOOP"
        if qty > 0:
            rules = self._rules(symbol)
            request = rules.limit_ioc(symbol=symbol, side=side, quantity=qty,
                                      limit_price=limit_price, reference_price=price,
                                      max_order_quote=self.max_order_quote,
                                      max_price_deviation=self.max_price_deviation)
            client_id = self.orders.client_order_id(profile_id,
                                                    symbol, ts, side)
            self.db.execute("INSERT INTO bot_orders VALUES(?,?,?,?,?,?,?,?)",
                            (client_id, symbol, ts, side, json.dumps(request, sort_keys=True),
                             None, "SUBMITTING", self.now_ms()))
            try:
                response = _plain(self.orders.submit_ioc(request,
                                  strategy_version=profile_id,
                                  timestamp=ts))
            except Exception:
                self.db.execute("UPDATE bot_orders SET status='UNKNOWN',updated_ms=? WHERE client_id=?",
                                (self.now_ms(), client_id))
                self._lock("submit_result_unknown")
            self._save_result(client_id, response)
            status = response.get("status", "UNKNOWN") if isinstance(response, dict) else "UNKNOWN"
        self.db.execute("INSERT INTO bot_signals VALUES(?,?,?,?)", (symbol, ts, weight, float(bar["close"])))
        self._event("hour_closed", symbol=symbol, timestamp=ts, weight=weight, profile_id=profile_id,
                    order_status=status, order_sent=request is not None)
        return {"symbol": symbol, "timestamp": ts, "weight": weight, "order_sent": request is not None,
                "profile_id": profile_id, "request": request, "response": response}

    def status(self):
        lock = self.db.execute("SELECT value FROM bot_state WHERE key='lock'").fetchone()[0]
        counts = {row[0]: row[1] for row in self.db.execute("SELECT status,COUNT(*) FROM bot_orders GROUP BY status")}
        return {"mode": "spot_testnet", "production_orders": False, "locked": bool(lock),
                "lock_reason": lock, "orders": counts,
                "signals": self.db.execute("SELECT COUNT(*) FROM bot_signals").fetchone()[0]}


async def run_testnet(trader, *, fetch=fetch_testnet_klines, once=False):
    """Bootstrap history without replaying trades, then execute only new closed candles."""
    import websockets
    history = {}
    for symbol in SYMBOLS:
        rows = await asyncio.to_thread(fetch, symbol, 1000)
        if len(rows) < trader.slow:
            raise ValueError("Insufficient Testnet candles to initialize strategy")
        history[symbol] = deque(rows[-trader.slow:], maxlen=trader.slow)
    if once:
        return {"mode": "spot_testnet", "state": "initialized", "order_sent": False,
                "latest": {s: history[s][-1]["timestamp"] for s in SYMBOLS}}
    streams = "/".join(f"{s.lower()}@kline_1h" for s in SYMBOLS)
    await _testnet_stream(trader, history, streams)


async def _testnet_stream(trader, history, streams):
    import websockets
    delay = 3
    while True:
        try:
            async with websockets.connect(f"{WS}/{streams}", ping_interval=20, ping_timeout=20,
                                          open_timeout=15, close_timeout=3) as ws:
                delay = 3
                async for raw in ws:
                    payload = json.loads(raw)
                    event = payload.get("data", payload)
                    if event.get("e") == "serverShutdown":
                        break
                    k = event.get("k", {})
                    symbol = k.get("s")
                    if symbol not in SYMBOLS or not k.get("x"):
                        continue
                    bar = normalize_kline([k["t"], k["o"], k["h"], k["l"], k["c"], k["v"],
                                           k["T"], k.get("q", "0"), k.get("n", 0)])
                    prior = history[symbol][-1]
                    if bar["timestamp"] == prior["timestamp"]:
                        continue
                    if bar["timestamp"] != prior["timestamp"] + HOUR_MS:
                        raise RuntimeError("Hourly data gap; refusing to trade before manual recovery")
                    history[symbol].append(bar)
                    try:
                        result = trader.process_closed_bar(symbol, list(history[symbol]))
                    except Exception as exc:
                        if not trader.status()["locked"]:
                            trader._lock(f"execution_error:{type(exc).__name__}")
                        raise
                    print(json.dumps(result, sort_keys=True, default=str), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, (RuntimeError, ValueError)):
                print(json.dumps({"state": "stopped_fail_closed", "error_type": type(exc).__name__}), flush=True)
                raise
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None) or getattr(exc, "code", None)
            if code in (403, 418, 451):
                print(json.dumps({"state": "blocked", "http_status": code}), flush=True)
                raise RuntimeError(f"Testnet market access blocked (HTTP {code})") from None
            # A network outage is retried, but any persisted lock remains in force.
            print(json.dumps({"state": "reconnecting", "error_type": type(exc).__name__,
                              "retry_seconds": delay}), flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def run_adaptive(controller, *, trader=None, fetch=fetch_testnet_klines):
    """Hourly AI selector. Shadow mode has no Testnet account or order client."""
    import websockets
    histories = {}
    for symbol in SYMBOLS:
        rows = await asyncio.to_thread(fetch, symbol, 1000)
        if len(rows) < 72:
            raise ValueError("Insufficient Testnet candles to initialize adaptive strategy")
        histories[symbol] = deque(rows[-72:], maxlen=72)
    latest = {s: histories[s][-1]["timestamp"] for s in SYMBOLS}
    if len(set(latest.values())) != 1:
        raise RuntimeError("BTC and ETH latest closed hours differ; refusing adaptive evaluation")
    streams = "/".join(f"{s.lower()}@kline_1h" for s in SYMBOLS)
    pending = {}
    delay = 3
    while True:
        try:
            async with websockets.connect(f"{WS}/{streams}", ping_interval=20, ping_timeout=20,
                                          open_timeout=15, close_timeout=3) as ws:
                delay = 3
                async for raw in ws:
                    payload = json.loads(raw)
                    event = payload.get("data", payload)
                    if event.get("e") == "serverShutdown":
                        break
                    k = event.get("k", {})
                    symbol = k.get("s")
                    if symbol not in SYMBOLS or not k.get("x"):
                        continue
                    bar = normalize_kline([k["t"], k["o"], k["h"], k["l"], k["c"], k["v"],
                                           k["T"], k.get("q", "0"), k.get("n", 0)])
                    prior = histories[symbol][-1]
                    if bar["timestamp"] == prior["timestamp"]:
                        continue
                    if bar["timestamp"] != prior["timestamp"] + HOUR_MS:
                        raise RuntimeError("Hourly data gap; backfill and reconcile before resuming")
                    histories[symbol].append(bar)
                    pending.setdefault(bar["timestamp"], set()).add(symbol)
                    if pending[bar["timestamp"]] != set(SYMBOLS):
                        continue
                    features_history = {s: list(histories[s]) for s in SYMBOLS}
                    decision = controller.evaluate(features_history)
                    print(json.dumps({"event": "adaptive_evaluation", **decision}, sort_keys=True), flush=True)
                    profile = controller.status()["active_profile"]
                    if trader is not None:
                        # Evaluation happens once per paired hour before either symbol can trade.
                        for pair_symbol in SYMBOLS:
                            result = trader.process_closed_bar(pair_symbol, list(histories[pair_symbol]),
                                                               profile_id=profile)
                            print(json.dumps(result, sort_keys=True, default=str), flush=True)
                    del pending[bar["timestamp"]]
                    # A second symbol event for an already-completed pair is a duplicate.
                    pending = {ts: seen for ts, seen in pending.items() if ts > bar["timestamp"]}
        except asyncio.CancelledError:
            raise
        except (RuntimeError, ValueError) as exc:
            print(json.dumps({"state": "stopped_fail_closed", "error_type": type(exc).__name__}), flush=True)
            raise
        except Exception as exc:
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None) or getattr(exc, "code", None)
            if code in (403, 418, 451):
                raise RuntimeError(f"Testnet market access blocked (HTTP {code})") from None
            print(json.dumps({"state": "reconnecting", "error_type": type(exc).__name__,
                              "retry_seconds": delay}), flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
