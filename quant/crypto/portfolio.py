"""Isolated multi-asset paper portfolio and deterministic signal-only journal."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .safety import LIVE_SYMBOLS
from .strategy import target_weights


class CryptoPortfolioPaper:
    def __init__(self, path, *, initial_cash=1000.0, fee_rate=0.001,
                 per_asset_cap=0.25, portfolio_cap=0.50, max_order_quote=250.0,
                 max_daily_loss=0.03):
        if initial_cash <= 0 or not 0 < per_asset_cap <= 0.25 or not 0 < portfolio_cap <= 0.5:
            raise ValueError("Invalid portfolio settings; caps may not exceed 25% per asset and 50% total")
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS portfolio_account(id INTEGER PRIMARY KEY CHECK(id=1), cash REAL NOT NULL, locked INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS assets(symbol TEXT PRIMARY KEY, quantity REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS portfolio_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, symbol TEXT, cash_delta REAL NOT NULL, asset_delta REAL NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS crypto_signals(ts INTEGER NOT NULL, symbol TEXT NOT NULL, strategy_version TEXT NOT NULL, target_weight REAL NOT NULL, close REAL NOT NULL, PRIMARY KEY(ts,symbol,strategy_version));
        CREATE TABLE IF NOT EXISTS crypto_marks(ts INTEGER NOT NULL, symbol TEXT NOT NULL, close REAL NOT NULL, PRIMARY KEY(ts,symbol));
        CREATE TABLE IF NOT EXISTS portfolio_days(day INTEGER PRIMARY KEY, start_equity REAL NOT NULL);
        """)
        if self.db.execute("SELECT 1 FROM portfolio_account").fetchone() is None:
            self.db.execute("INSERT INTO portfolio_account(id,cash) VALUES(1,?)", (initial_cash,))
            self.db.execute("INSERT INTO portfolio_ledger(ts,symbol,cash_delta,asset_delta,kind) VALUES(0,NULL,?,0,'initial')", (initial_cash,))
        self.fee_rate, self.per_asset_cap = fee_rate, per_asset_cap
        self.portfolio_cap, self.max_order_quote = portfolio_cap, max_order_quote
        if not 0 < max_daily_loss < 1: raise ValueError("max_daily_loss must be between 0 and 1")
        self.max_daily_loss = max_daily_loss

    def close(self): self.db.close()

    def signal(self, *, timestamp, symbol, target_weight, close, strategy_version="sma-24-72-candidate-v1"):
        symbol = symbol.upper()
        if symbol not in LIVE_SYMBOLS or close <= 0 or not 0 <= target_weight <= 1:
            raise ValueError("Invalid symbol, close, or target weight")
        self.db.execute("INSERT OR IGNORE INTO crypto_signals VALUES(?,?,?,?,?)",
                        (timestamp, symbol, strategy_version, target_weight, close))
        self.db.execute("INSERT OR IGNORE INTO crypto_marks VALUES(?,?,?)", (timestamp, symbol, close))
        return {"timestamp": timestamp, "symbol": symbol, "strategy_version": strategy_version,
                "target_weight": target_weight, "close": close, "order_sent": False}

    def observe_bars(self, bars_by_symbol, *, fast=24, slow=72, strategy_version="sma-24-72-candidate-v1"):
        out = []
        for symbol, bars in sorted(bars_by_symbol.items()):
            weights = target_weights(bars, fast, slow)
            for bar, weight in zip(bars, weights):
                out.append(self.signal(timestamp=int(bar["timestamp"]), symbol=symbol,
                    target_weight=weight, close=float(bar["close"]), strategy_version=strategy_version))
        return out

    def status(self):
        account = dict(self.db.execute("SELECT * FROM portfolio_account WHERE id=1").fetchone())
        assets = {r["symbol"]: r["quantity"] for r in self.db.execute("SELECT * FROM assets ORDER BY symbol")}
        marks = {r["symbol"]: r["close"] for r in self.db.execute("SELECT m.symbol,m.close FROM crypto_marks m JOIN (SELECT symbol,MAX(ts) ts FROM crypto_marks GROUP BY symbol) x ON m.symbol=x.symbol AND m.ts=x.ts")}
        equity = account["cash"] + sum(assets.get(s, 0) * marks.get(s, 0) for s in assets)
        return {**account, "assets": assets, "marks": marks, "equity": equity,
                "signal_count": self.db.execute("SELECT COUNT(*) FROM crypto_signals").fetchone()[0],
                "reconciliation": self.reconcile()}

    def reconcile(self):
        ledger_cash = self.db.execute("SELECT COALESCE(SUM(cash_delta),0) FROM portfolio_ledger").fetchone()[0]
        cash = self.db.execute("SELECT cash FROM portfolio_account WHERE id=1").fetchone()[0]
        ledger_assets = {r["symbol"]: r["quantity"] for r in self.db.execute("SELECT symbol,SUM(asset_delta) quantity FROM portfolio_ledger WHERE symbol IS NOT NULL GROUP BY symbol")}
        actual = {r["symbol"]: r["quantity"] for r in self.db.execute("SELECT * FROM assets")}
        ok = abs(ledger_cash-cash)<1e-7 and all(abs(ledger_assets.get(s,0)-actual.get(s,0))<1e-10 for s in set(ledger_assets)|set(actual))
        if not ok:
            self.db.execute("UPDATE portfolio_account SET locked=1,reason='ledger_reconciliation_failed' WHERE id=1")
        return {"ok": ok, "cash": cash, "ledger_cash": ledger_cash, "assets": actual, "ledger_assets": ledger_assets}

    def halt(self, reason="manual_stop"):
        self.db.execute("UPDATE portfolio_account SET locked=1,reason=? WHERE id=1", (reason,))

    def paper_rebalance(self, *, timestamp, prices, target_weights):
        """Deterministic close-price paper fills, with per-asset and aggregate exposure caps."""
        if not self.reconcile()["ok"]: raise ValueError("Reconciliation failed; account locked")
        account = self.db.execute("SELECT * FROM portfolio_account WHERE id=1").fetchone()
        if account["locked"]: raise ValueError(f"Kill switch active: {account['reason']}")
        prices = {k.upper(): float(v) for k, v in prices.items()}
        weights = {k.upper(): float(v) for k, v in target_weights.items()}
        if set(prices) != set(weights) or not set(prices).issubset(LIVE_SYMBOLS) or any(v <= 0 for v in prices.values()):
            raise ValueError("Provide matching positive BTCUSDT/ETHUSDT prices and weights")
        # Idempotency is per completed portfolio timestamp.
        if self.db.execute("SELECT 1 FROM portfolio_ledger WHERE ts=? AND kind='paper_batch'", (timestamp,)).fetchone():
            return {"timestamp": timestamp, "state": "already_processed", "orders": []}
        assets = {r["symbol"]: r["quantity"] for r in self.db.execute("SELECT * FROM assets")}
        equity = account["cash"] + sum(assets.get(s, 0) * p for s,p in prices.items())
        day = timestamp // 86_400_000
        self.db.execute("INSERT OR IGNORE INTO portfolio_days VALUES(?,?)", (day,equity))
        start_equity = self.db.execute("SELECT start_equity FROM portfolio_days WHERE day=?", (day,)).fetchone()[0]
        if equity-start_equity <= -self.max_daily_loss*start_equity:
            self.halt("daily_loss_limit")
            return {"timestamp":timestamp,"state":"HALTED","reason":"daily_loss_limit","order_sent":False}
        weights = {s: min(max(0, weights[s]), self.per_asset_cap) for s in prices}
        gross = sum(weights.values())
        if gross > self.portfolio_cap:
            weights = {s: w*self.portfolio_cap/gross for s,w in weights.items()}
        orders=[]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for symbol, price in prices.items():
                qty = assets.get(symbol, 0)
                delta = equity*weights[symbol]/price - qty
                if abs(delta*price) < 1e-9: continue
                delta = max(-qty, min(delta, self.max_order_quote/price))
                if delta > 0:
                    delta = min(delta, account["cash"]/(price*(1+self.fee_rate)))
                fee = abs(delta)*price*self.fee_rate
                cash_delta = -delta*price-fee if delta > 0 else -delta*price-fee
                self.db.execute("INSERT INTO assets(symbol,quantity) VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET quantity=quantity+excluded.quantity", (symbol,delta))
                self.db.execute("UPDATE portfolio_account SET cash=cash+? WHERE id=1", (cash_delta,))
                self.db.execute("INSERT INTO portfolio_ledger(ts,symbol,cash_delta,asset_delta,kind) VALUES(?,?,?,?,?)", (timestamp,symbol,cash_delta,delta,"paper_fill"))
                account = self.db.execute("SELECT * FROM portfolio_account WHERE id=1").fetchone()
                orders.append({"symbol":symbol,"side":"BUY" if delta>0 else "SELL","quantity":abs(delta),"price":price,"fee":fee,"status":"FILLED"})
            self.db.execute("INSERT INTO portfolio_ledger(ts,symbol,cash_delta,asset_delta,kind) VALUES(?,NULL,0,0,'paper_batch')", (timestamp,))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK"); raise
        return {"timestamp":timestamp,"orders":orders,"reconciliation":self.reconcile()}
