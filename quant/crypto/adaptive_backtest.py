"""Deterministic cost-aware comparisons for the compiled adaptive profiles."""
from __future__ import annotations

import hashlib
import math

from .market import validate_bars
from .observer import HOUR_MS, SYMBOLS
from .strategy import target_weights


def _metric(equities):
    if not equities:
        return {"return": 0.0, "max_drawdown": 0.0, "annualized_sharpe": 0.0}
    start, end = equities[0], equities[-1]
    peak, drawdown = start, 0.0
    returns = []
    for prior, current in zip(equities, equities[1:]):
        if prior > 0:
            returns.append(current / prior - 1)
        peak = max(peak, current)
        if peak:
            drawdown = max(drawdown, (peak-current)/peak)
    mean = sum(returns)/len(returns) if returns else 0.0
    variance = (sum((x-mean)**2 for x in returns)/len(returns)) if returns else 0.0
    std = math.sqrt(variance)
    sharpe = mean/std*math.sqrt(24*365) if std else 0.0
    return {"return": end/start-1 if start else 0.0,
            "max_drawdown": drawdown, "annualized_sharpe": sharpe}


def backtest_profiles(bars_by_symbol, *, initial_cash=1000.0, fee_rate=0.001,
                      slippage=0.0005, max_order_quote=25.0,
                      max_daily_loss_quote=10.0,
                      nonfill_rate=0.1, partial_fill_rate=0.05,
                      partial_fill_fraction=0.5, seed="adaptive-v1"):
    """Replay only the approved profiles; never treats simulated fills as evidence of live fills."""
    if set(bars_by_symbol) != set(SYMBOLS):
        raise ValueError("Backtest requires exactly BTCUSDT and ETHUSDT")
    if (initial_cash <= 0 or fee_rate < 0 or slippage < 0 or max_order_quote <= 0 or max_daily_loss_quote <= 0 or
            not 0 <= nonfill_rate <= 1 or not 0 <= partial_fill_rate <= 1 or
            not 0 < partial_fill_fraction <= 1):
        raise ValueError("Invalid backtest assumptions")
    for rows in bars_by_symbol.values():
        validate_bars(rows)
    timestamps = [r["timestamp"] for r in bars_by_symbol[SYMBOLS[0]]]
    if any([r["timestamp"] for r in bars_by_symbol[s]] != timestamps for s in SYMBOLS[1:]):
        raise ValueError("BTC and ETH backtest candles must be aligned")
    if len(timestamps) < 74:
        raise ValueError("Need at least 74 aligned hourly candles")
    n = len(timestamps)
    targets = {
        symbol: target_weights(bars_by_symbol[symbol], 24, 72)
        for symbol in SYMBOLS
    }
    results = {}
    for profile in ("sma_24_72", "cash"):
        cash = float(initial_cash)
        assets = {symbol: 0.0 for symbol in SYMBOLS}
        equity_curve = []
        fees = turnover = 0.0
        submitted = filled = nonfilled = partials = 0
        prior_day, day_start_equity, locked = None, float(initial_cash), False
        segment_marks = [[], [], []]
        for i in range(72, n):
            close_prices = {s: float(bars_by_symbol[s][i]["close"]) for s in SYMBOLS}
            equity = cash + sum(assets[s]*close_prices[s] for s in SYMBOLS)
            equity_curve.append(equity)
            segment_marks[min(2, (i-72)*3 // max(1, n-72))].append(equity)
            day = timestamps[i] // 86_400_000
            if day != prior_day:
                prior_day, day_start_equity = day, equity
            if day_start_equity - equity >= max_daily_loss_quote:
                locked = True
            if i == n-1:
                break
            if locked:
                continue
            for symbol in SYMBOLS:
                price = close_prices[symbol]
                equity = cash + sum(assets[s]*close_prices[s] for s in SYMBOLS)
                active = profile == "sma_24_72" and targets[symbol][i] > 0
                target_value = min(equity*.25, max(0.0, equity*.50 -
                    sum(assets[s]*close_prices[s] for s in SYMBOLS if s != symbol))) if active else 0.0
                delta = target_value/price - assets[symbol]
                if abs(delta*price) < 5.0:
                    continue
                side = "BUY" if delta > 0 else "SELL"
                quantity = min(abs(delta), max_order_quote/price)
                if side == "SELL":
                    quantity = min(quantity, assets[symbol])
                elif side == "BUY":
                    quantity = min(quantity, cash/(price*(1+fee_rate)),
                                   max(0.0, equity*.25-assets[symbol]*price)/price)
                if quantity <= 0:
                    continue
                submitted += 1
                next_bar = bars_by_symbol[symbol][i+1]
                crossing = (float(next_bar["low"]) <= price*1.005 if side == "BUY"
                            else float(next_bar["high"]) >= price*.995)
                digest = hashlib.sha256(f"{seed}:{profile}:{symbol}:{timestamps[i]}".encode()).digest()
                draw = int.from_bytes(digest[:8], "big") / 2**64
                if draw < nonfill_rate or not crossing:
                    nonfilled += 1
                    continue
                if nonfill_rate < draw < min(1.0, nonfill_rate+partial_fill_rate):
                    quantity *= partial_fill_fraction
                    partials += 1
                reference = float(next_bar["open"])
                execution = min(price*1.005, reference*(1+slippage)) if side == "BUY" else max(price*.995, reference*(1-slippage))
                fee = quantity*execution*fee_rate
                if side == "BUY":
                    spend = quantity*execution+fee
                    if spend > cash:
                        quantity *= cash/spend
                        fee = quantity*execution*fee_rate
                        spend = quantity*execution+fee
                    cash -= spend
                    assets[symbol] += quantity
                else:
                    quantity = min(quantity, assets[symbol])
                    cash += quantity*execution-fee
                    assets[symbol] -= quantity
                fees += fee
                turnover += quantity*execution
                filled += 1
        metrics = _metric(equity_curve)
        stages = []
        for marks in segment_marks:
            stages.append({"observations": len(marks), **_metric(marks)})
        results[profile] = {**metrics, "final_equity": equity_curve[-1],
                            "stopped_on_daily_loss": locked,
                            "fees_quote": fees, "turnover_over_average_equity":
                                turnover/(sum(equity_curve)/len(equity_curve)) if equity_curve else 0,
                            "orders_submitted": submitted, "orders_filled": filled,
                            "orders_not_filled": nonfilled, "partial_fills": partials,
                            "stages": stages}
    return {"mode": "historical_profile_comparison", "symbols": list(SYMBOLS),
            "candles": n, "assumptions": {"initial_cash": initial_cash,
                "fee_rate": fee_rate, "slippage": slippage,
                "max_order_quote": max_order_quote, "ioc_nonfill_rate": nonfill_rate,
                "max_daily_loss_quote": max_daily_loss_quote,
                "partial_fill_rate": partial_fill_rate,
                "partial_fill_fraction": partial_fill_fraction, "seed": seed},
            "profiles": results,
            "warning": "Scenario simulation with explicit fill assumptions; not observed execution or a return forecast. It evaluates profiles, not the AI selector's historical judgment."}
