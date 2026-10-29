from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .risk import check_order
from .strategy import target_weights


class CryptoPaperBroker:
    def __init__(self, path, *, initial_cash=1000.0, fee_rate=0.001, slippage=0.0005,
                 max_order_quote=250.0, max_position_weight=0.25, max_daily_loss=0.03,
                 max_price_deviation=0.02):
        if initial_cash <= 0 or min(fee_rate, slippage) < 0 or not 0 < max_position_weight <= 1:
            raise ValueError("Invalid paper broker settings")
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1), cash REAL NOT NULL, asset REAL NOT NULL DEFAULT 0, locked INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, timestamp INTEGER, symbol TEXT, side TEXT, quantity REAL, price REAL, fee REAL, status TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS fills(order_id TEXT PRIMARY KEY REFERENCES orders(id), timestamp INTEGER, symbol TEXT, side TEXT, quantity REAL, price REAL, fee REAL);
        CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY, timestamp INTEGER, cash_delta REAL, asset_delta REAL, kind TEXT);
        CREATE TABLE IF NOT EXISTS marks(timestamp INTEGER PRIMARY KEY, price REAL, equity REAL, target_weight REAL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if self.db.execute("SELECT 1 FROM account").fetchone() is None:
            self.db.execute("INSERT INTO account(id,cash) VALUES(1,?)", (initial_cash,))
            self.db.execute("INSERT INTO ledger VALUES('initial',0,?,0,'initial')", (initial_cash,))
            self.db.execute("INSERT INTO meta VALUES('initial_cash',?)", (str(initial_cash),))
        self.fee_rate, self.slippage = fee_rate, slippage
        self.max_order_quote, self.max_position_weight = max_order_quote, max_position_weight
        self.max_daily_loss, self.max_price_deviation = max_daily_loss, max_price_deviation

    def close(self): self.db.close()
    def status(self, price=0):
        a = dict(self.db.execute("SELECT * FROM account WHERE id=1").fetchone())
        a["equity"] = a["cash"] + a["asset"] * price
        a["reconciliation"] = self.reconcile()
        a["orders"] = self.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        return a

    def reconcile(self):
        cash = self.db.execute("SELECT COALESCE(SUM(cash_delta),0) FROM ledger").fetchone()[0]
        asset = self.db.execute("SELECT COALESCE(SUM(asset_delta),0) FROM ledger").fetchone()[0]
        a = self.db.execute("SELECT cash,asset FROM account WHERE id=1").fetchone()
        bad = self.db.execute("""SELECT COUNT(*) FROM orders o LEFT JOIN fills f ON o.id=f.order_id
             WHERE (o.status='FILLED' AND f.order_id IS NULL) OR (o.status!='FILLED' AND f.order_id IS NOT NULL)""").fetchone()[0]
        ok = abs(a[0]-cash)<1e-7 and abs(a[1]-asset)<1e-10 and not bad
        if not ok: self.db.execute("UPDATE account SET locked=1, reason='ledger_reconciliation_failed' WHERE id=1")
        return {"ok": ok, "cash": a[0], "ledger_cash": cash, "asset": a[1], "ledger_asset": asset, "unmatched_orders": bad}

    def halt(self, reason="manual_stop"):
        self.db.execute("UPDATE account SET locked=1,reason=? WHERE id=1", (reason,))

    def step(self, *, timestamp, symbol, target_weight, price, reference_price, daily_start_equity):
        policy = json.dumps({"fee_rate": self.fee_rate, "slippage": self.slippage,
            "max_order_quote": self.max_order_quote, "max_position_weight": self.max_position_weight,
            "max_daily_loss": self.max_daily_loss, "max_price_deviation": self.max_price_deviation}, sort_keys=True)
        saved_policy = self.db.execute("SELECT value FROM meta WHERE key='policy'").fetchone()
        if saved_policy and saved_policy[0] != policy:
            raise ValueError("Paper account is bound to different execution/risk settings; use a new database")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('policy',?)", (policy,))
        saved_symbol = self.db.execute("SELECT value FROM meta WHERE key='symbol'").fetchone()
        if saved_symbol and saved_symbol[0] != symbol:
            raise ValueError("Paper account is bound to a different symbol; use a new database")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('symbol',?)", (symbol,))
        if not self.reconcile()["ok"]: raise ValueError("Reconciliation failed; account locked")
        account = self.db.execute("SELECT * FROM account WHERE id=1").fetchone()
        if account["locked"]: raise ValueError(f"Kill switch active: {account['reason']}")
        exists = self.db.execute("SELECT 1 FROM marks WHERE timestamp=?", (timestamp,)).fetchone()
        if exists: return {"state": "already_processed", "timestamp": timestamp}
        equity = account["cash"] + account["asset"] * price
        daily_pnl = equity - daily_start_equity
        if daily_pnl <= -self.max_daily_loss * daily_start_equity:
            self.halt("daily_loss_limit")
            return {"timestamp": timestamp, "symbol": symbol, "status": "HALTED", "reason": "daily_loss_limit", "equity": equity}
        desired = max(0.0, equity * target_weight / price)
        delta = desired - account["asset"]
        side = "BUY" if delta > 1e-12 else "SELL" if delta < -1e-12 else None
        event = {"timestamp": timestamp, "symbol": symbol, "signal_weight": target_weight,
                 "equity": equity, "side": side, "status": "NOOP"}
        if side:
            execution_price = price * (1 + self.slippage if side == "BUY" else 1 - self.slippage)
            quantity = min(abs(delta), self.max_order_quote / execution_price)
            if side == "BUY":
                quantity = min(quantity, max(0.0, equity * self.max_position_weight / execution_price - account["asset"]))
            reason = check_order(side=side, quantity=quantity, price=execution_price,
                cash=account["cash"], position=account["asset"], equity=equity,
                max_order_quote=self.max_order_quote, max_position_weight=self.max_position_weight,
                max_daily_loss=self.max_daily_loss, daily_pnl=daily_pnl,
                reference_price=reference_price, max_price_deviation=self.max_price_deviation)
            oid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{symbol}:{timestamp}:{side}"))
            if reason:
                self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)", (oid,timestamp,symbol,side,0,0,0,"REJECTED",reason))
                event.update(status="REJECTED", reason=reason)
            else:
                fee = quantity * execution_price * self.fee_rate
                cash_delta = -(quantity * execution_price + fee) if side == "BUY" else quantity * execution_price - fee
                asset_delta = quantity if side == "BUY" else -quantity
                if side == "BUY" and -cash_delta > account["cash"]:
                    quantity = max(0.0, min(quantity, account["cash"] / (execution_price * (1 + self.fee_rate))))
                    fee = quantity * execution_price * self.fee_rate
                    cash_delta = -(quantity * execution_price + fee)
                    asset_delta = quantity
                if side == "BUY" and quantity <= 1e-12:
                    event.update(status="REJECTED", reason="insufficient_cash_after_fee")
                else:
                    self.db.execute("BEGIN IMMEDIATE")
                    try:
                        self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)", (oid,timestamp,symbol,side,quantity,execution_price,fee,"FILLED",""))
                        self.db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)", (oid,timestamp,symbol,side,quantity,execution_price,fee))
                        self.db.execute("INSERT INTO ledger VALUES(?,?,?,?,?)", (oid,timestamp,cash_delta,asset_delta,"fill"))
                        self.db.execute("UPDATE account SET cash=cash+?,asset=asset+? WHERE id=1", (cash_delta,asset_delta))
                        self.db.execute("COMMIT")
                    except BaseException:
                        self.db.execute("ROLLBACK"); raise
                    event.update(status="FILLED", quantity=quantity, price=execution_price, fee=fee)
        equity = self.db.execute("SELECT cash+asset*? FROM account WHERE id=1", (price,)).fetchone()[0]
        self.db.execute("INSERT INTO marks VALUES(?,?,?,?)", (timestamp,price,equity,target_weight))
        event["equity"] = equity
        return event


def run(bars, broker, symbol="BTCUSDT", fast=24, slow=72):
    strategy_binding = json.dumps({"name": "sma_trend_long_cash", "fast": fast, "slow": slow}, sort_keys=True)
    saved_strategy = broker.db.execute("SELECT value FROM meta WHERE key='strategy'").fetchone()
    if saved_strategy and saved_strategy[0] != strategy_binding:
        raise ValueError("Paper account is bound to a different strategy; use a new database")
    broker.db.execute("INSERT OR IGNORE INTO meta VALUES('strategy',?)", (strategy_binding,))
    weights = target_weights(bars, fast, slow)
    events=[]; day_equity=None; prior_day=None
    for bar, weight in zip(bars, weights):
        day = bar["timestamp"] // 86_400_000
        price = float(bar["close"])
        if day != prior_day:
            day_equity = broker.status(price)["equity"]
            prior_day = day
        events.append(broker.step(timestamp=bar["timestamp"], symbol=symbol, target_weight=weight,
            price=price, reference_price=bar["close"], daily_start_equity=day_equity))
    end=broker.status(float(bars[-1]["close"]))
    first_equity=float(broker.db.execute("SELECT value FROM meta WHERE key='initial_cash'").fetchone()[0])
    marks=[r[0] for r in broker.db.execute("SELECT equity FROM marks ORDER BY timestamp")]
    peak=first_equity; max_dd=0.0
    for value in marks:
        peak=max(peak,value); max_dd=max(max_dd,(peak-value)/peak if peak else 0)
    return {"symbol":symbol,"candles":len(bars),"initial_equity":first_equity,"final_equity":end["equity"],
        "return":end["equity"]/first_equity-1,"max_drawdown":max_dd,
        "fees":broker.db.execute("SELECT COALESCE(SUM(fee),0) FROM fills").fetchone()[0],
        "filled_orders":broker.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
        "rejected_orders":broker.db.execute("SELECT COUNT(*) FROM orders WHERE status='REJECTED'").fetchone()[0],
        "reconciliation":end["reconciliation"],"warning":"Historical simulation only; not a return forecast"}

