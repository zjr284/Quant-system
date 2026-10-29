"""Research-only multi-asset, multi-timeframe Binance Spot backtests.

This module has no account or order client and is deliberately disconnected from
the AI/Testnet profile allowlist.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .market import validate_bars

INITIAL_CANDIDATES = ("SOLUSDT", "XRPUSDT", "BNBUSDT", "NEARUSDT", "DOGEUSDT", "TRXUSDT", "SUIUSDT", "UNIUSDT")
ALTERNATES = ("LINKUSDT", "AAVEUSDT", "LTCUSDT", "BCHUSDT", "AVAXUSDT", "ADAUSDT")
ALL_CANDIDATES = INITIAL_CANDIDATES + ALTERNATES
ALLOWED_DATA_SYMBOLS = frozenset(ALL_CANDIDATES + ("BTCUSDT", "ETHUSDT"))
TIMEFRAME_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
PROFILES = {
    "cash": {"family": "baseline", "selector_eligible": False},
    "buy_hold_equal_weight": {"family": "baseline", "selector_eligible": False},
    "sma_24_72": {"family": "trend", "selector_eligible": False},
    "sma_48_144": {"family": "trend", "selector_eligible": False},
    "ema_24_72": {"family": "trend", "selector_eligible": False},
    "donchian_20_10": {"family": "trend", "selector_eligible": False},
    "donchian_55_20": {"family": "trend", "selector_eligible": False},
    "bollinger_20_2": {"family": "mean_reversion", "selector_eligible": False},
    "rsi_14_30_50": {"family": "mean_reversion", "selector_eligible": False},
    "cross_momentum_top2": {"family": "cross_sectional", "selector_eligible": False},
    "sma_24_72_invvol": {"family": "volatility_overlay", "selector_eligible": False},
    "grid_exploratory": {"family": "exploratory_grid", "selector_eligible": False,
                         "requires_inventory_execution_review": True},
}
PROFILE_PARAMETERS = {
    "cash": {}, "buy_hold_equal_weight": {"per_asset_cap": .25, "portfolio_cap": .50},
    "sma_24_72": {"fast": 24, "slow": 72}, "sma_48_144": {"fast": 48, "slow": 144},
    "ema_24_72": {"fast": 24, "slow": 72}, "donchian_20_10": {"entry": 20, "exit": 10},
    "donchian_55_20": {"entry": 55, "exit": 20},
    "bollinger_20_2": {"window": 20, "standard_deviations": 2, "trend_filter_sma": 200},
    "rsi_14_30_50": {"period": 14, "entry_below": 30, "exit_above": 50, "trend_filter_sma": 200},
    "cross_momentum_top2": {"ranking": "momentum_over_realized_volatility", "hold_count": 2},
    "sma_24_72_invvol": {"fast": 24, "slow": 72, "overlay": "inverse_realized_volatility"},
    "grid_exploratory": {"scored": False},
}


def _read_hour_csv(path):
    rows = []
    latest_open_hour = (int(datetime.now(timezone.utc).timestamp()*1000) // TIMEFRAME_MS["1h"]) * TIMEFRAME_MS["1h"]
    with Path(path).open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            ts = row.get("timestamp") or row.get("open_time") or row.get("time")
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = int(dt.astimezone(timezone.utc).timestamp() * 1000)
            if ts >= latest_open_hour:
                continue
            rows.append({"timestamp": ts, **{k: float(row[k]) for k in ("open", "high", "low", "close")},
                         "volume": float(row.get("volume", 0) or 0),
                         "quote_volume": float(row.get("quote_volume", 0) or 0), "closed": True})
    validate_bars(rows)
    return rows


def _aggregate(rows, timeframe):
    """UTC anchored aggregation; reject missing or partial timeframe buckets."""
    width = TIMEFRAME_MS[timeframe]
    if width == TIMEFRAME_MS["1h"]:
        return rows
    out, bucket = [], []
    current = None
    for row in rows:
        key = row["timestamp"] // width
        if current is not None and key != current:
            if len(bucket) == width // TIMEFRAME_MS["1h"]:
                out.append({"timestamp": current * width, "open": bucket[0]["open"],
                            "high": max(x["high"] for x in bucket), "low": min(x["low"] for x in bucket),
                            "close": bucket[-1]["close"], "volume": sum(x["volume"] for x in bucket),
                            "quote_volume": sum(x.get("quote_volume", 0) for x in bucket), "closed": True})
            bucket = []
        current = key
        bucket.append(row)
    if len(bucket) == width // TIMEFRAME_MS["1h"]:
        out.append({"timestamp": current * width, "open": bucket[0]["open"],
                    "high": max(x["high"] for x in bucket), "low": min(x["low"] for x in bucket),
                    "close": bucket[-1]["close"], "volume": sum(x["volume"] for x in bucket),
                    "quote_volume": sum(x.get("quote_volume", 0) for x in bucket), "closed": True})
    if not out:
        raise ValueError(f"No complete {timeframe} candles")
    return out


def _ema(values, period):
    out, alpha = [None] * len(values), 2 / (period + 1)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * alpha + out[i - 1] * (1 - alpha)
    return out


def _rsi(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains = [max(0, values[i] - values[i - 1]) for i in range(1, period + 1)]
    losses = [max(0, values[i - 1] - values[i]) for i in range(1, period + 1)]
    avg_g, avg_l = sum(gains) / period, sum(losses) / period
    def value(g, l): return 100 if l == 0 else 100 - 100 / (1 + g / l)
    out[period] = value(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        avg_g = (avg_g * (period - 1) + max(0, values[i] - values[i - 1])) / period
        avg_l = (avg_l * (period - 1) + max(0, values[i - 1] - values[i])) / period
        out[i] = value(avg_g, avg_l)
    return out


def _signals(profile, bars):
    close = [float(x["close"]) for x in bars]
    n = len(close)
    if profile in ("cash", "buy_hold_equal_weight", "grid_exploratory"):
        return [0.0 if profile != "buy_hold_equal_weight" else 1.0] * n
    if profile in ("benchmark_btc_hold", "benchmark_eth_hold"):
        wanted = "BTCUSDT" if profile == "benchmark_btc_hold" else "ETHUSDT"
        return [1.0 if bars and bars[0].get("symbol") == wanted else 0.0] * n
    if profile.startswith("sma_") or profile == "ema_24_72":
        fast, slow = ((24, 72) if profile in ("sma_24_72", "sma_24_72_invvol", "ema_24_72") else (48, 144))
        a, b = _ema(close, fast), _ema(close, slow)
        if profile != "ema_24_72":
            a = [None] * n
            for i in range(fast - 1, n): a[i] = sum(close[i-fast+1:i+1]) / fast
            b = [None] * n
            for i in range(slow - 1, n): b[i] = sum(close[i-slow+1:i+1]) / slow
        return [1.0 if a[i] is not None and b[i] is not None and a[i] > b[i] else 0.0 for i in range(n)]
    if profile.startswith("donchian"):
        entry, exit_period = ((20, 10) if profile == "donchian_20_10" else (55, 20))
        active, out = False, []
        for i in range(n):
            if i >= entry and bars[i]["close"] > max(float(x["high"]) for x in bars[i-entry:i]): active = True
            if i >= exit_period and bars[i]["close"] < min(float(x["low"]) for x in bars[i-exit_period:i]): active = False
            out.append(float(active))
        return out
    if profile == "bollinger_20_2":
        out = [0.0] * n
        for i in range(200, n):
            window = close[i-19:i+1]
            mean, std = statistics.mean(window), statistics.pstdev(window)
            prior = out[i-1]
            trend = close[i] > sum(close[i-199:i+1])/200
            if prior:
                out[i] = 0.0 if close[i] >= mean or not trend else 1.0
            else:
                out[i] = float(close[i] < mean-2*std and trend)
        return out
    if profile == "rsi_14_30_50":
        rsi = _rsi(close)
        out = [0.0] * n
        for i in range(200, n):
            trend = close[i] > sum(close[i-199:i+1])/200
            out[i] = (1.0 if rsi[i] is not None and rsi[i] < 30 and trend else
                      (out[i-1] if rsi[i] is not None and rsi[i] < 50 and trend else 0.0))
        return out
    if profile == "cross_momentum_top2":
        return [0.0] * n  # Computed jointly in the portfolio loop.
    raise ValueError(f"Unknown research profile: {profile}")


def _risk_metrics(equity, periods_per_year):
    if not equity: return {"return": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "sortino": 0.0}
    rets = [b/a-1 for a, b in zip(equity, equity[1:]) if a > 0]
    mean = statistics.mean(rets) if rets else 0.0
    std = statistics.pstdev(rets) if rets else 0.0
    downside = math.sqrt(sum(min(0, x)**2 for x in rets)/len(rets)) if rets else 0.0
    peak, dd = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        if peak: dd = max(dd, (peak-value)/peak)
    return {"return": equity[-1]/equity[0]-1 if equity[0] else 0.0,
            "final_equity": equity[-1], "max_drawdown": dd,
            "sharpe": mean/std*math.sqrt(periods_per_year) if std else 0.0,
            "sortino": mean/downside*math.sqrt(periods_per_year) if downside else 0.0}


def _warmup_bars(profile, timeframe):
    if profile in ("bollinger_20_2", "rsi_14_30_50"):
        return 200
    if profile in ("cross_momentum_top2", "sma_24_72_invvol"):
        return 30 if timeframe == "1d" else (180 if timeframe == "4h" else 720)
    return 144 if profile == "sma_48_144" else 72


def _simulate(bars_by_symbol, timeframe, profile, *, initial_cash, fee, slippage,
              nonfill, partial, partial_fraction, seed, max_order_quote=25.0, max_daily_loss=10.0,
              warmup_bars=None):
    symbols = list(bars_by_symbol)
    timestamps = [r["timestamp"] for r in bars_by_symbol[symbols[0]]]
    if any([r["timestamp"] for r in bars_by_symbol[s]] != timestamps for s in symbols[1:]):
        raise ValueError("All selected symbols must have identical closed-candle timestamps")
    histories = bars_by_symbol
    closes_by_symbol = {s: [float(x["close"]) for x in histories[s]] for s in symbols}
    signals = {s: _signals(profile, histories[s]) for s in symbols}
    cash, units = float(initial_cash), {s: 0.0 for s in symbols}
    equity_curve, fee_total, turnover = [], 0.0, 0.0
    submitted = fills = missed = partials = 0
    exposure_sum, max_asset_exposure = 0.0, 0.0
    current_day, day_start_equity, locked = None, float(initial_cash), False
    width = TIMEFRAME_MS[timeframe]
    warmup = _warmup_bars(profile, timeframe) if warmup_bars is None else warmup_bars
    hold_units = None
    lock_timestamp = None
    for i in range(warmup, len(timestamps)-1):
        prices = {s: float(histories[s][i]["close"]) for s in symbols}
        equity = cash + sum(units[s]*prices[s] for s in symbols)
        equity_curve.append(equity)
        day = timestamps[i] // 86_400_000
        if day != current_day:
            # The previous close is the UTC day boundary. Resetting to the
            # current close would erase each daily candle's loss entirely.
            current_day = day
            day_start_equity = equity_curve[-2] if len(equity_curve) > 1 else float(initial_cash)
        if day_start_equity - equity >= max_daily_loss:
            if not locked: lock_timestamp = timestamps[i]
            locked = True
        exposure = sum(units[s]*prices[s] for s in symbols)/equity if equity else 0
        exposure_sum += exposure
        max_asset_exposure = max(max_asset_exposure, *(units[s]*prices[s]/equity for s in symbols))
        desired = {s: signals[s][i] for s in symbols}
        if profile == "cross_momentum_top2":
            lb = 7 if timeframe == "1d" else (42 if timeframe == "4h" else 168)
            vol_n = 30 if timeframe == "1d" else (180 if timeframe == "4h" else 720)
            ranked = []
            for s in symbols:
                closes = closes_by_symbol[s]
                if i >= max(lb, vol_n):
                    returns = [closes[j]/closes[j-1]-1 for j in range(i-vol_n+1, i+1)]
                    vol = statistics.pstdev(returns)
                    momentum = closes[i]/closes[i-lb]-1
                    if momentum > 0 and vol > 0: ranked.append((momentum/vol, s))
            desired = {s: 0.0 for s in symbols}
            for _, s in sorted(ranked, reverse=True)[:2]: desired[s] = 1.0
        if profile == "buy_hold_equal_weight": desired = {s: 1.0 for s in symbols}
        weights = {s: min(.25, desired[s] * (.5 / max(1, sum(desired.values())) if profile == "cross_momentum_top2" else .25)) for s in symbols}
        if profile == "sma_24_72_invvol":
            inverse = {}
            lookback = 30 if timeframe == "1d" else (180 if timeframe == "4h" else 720)
            for s in symbols:
                closes = closes_by_symbol[s]
                rets = [closes[j]/closes[j-1]-1 for j in range(max(1, i-lookback+1), i+1)]
                vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
                inverse[s] = (1/vol if vol > 0 else 0.0) * desired[s]
            inverse_sum = sum(inverse.values())
            weights = {s: min(.25, .5*inverse[s]/inverse_sum) if inverse_sum else 0.0 for s in symbols}
        # Scale total target to the hard 50% portfolio ceiling.
        total = sum(weights.values())
        if total > .5:
            weights = {s: w*.5/total for s, w in weights.items()}
        targets = {s: equity * weights[s] for s in symbols}
        if profile in ("buy_hold_equal_weight", "benchmark_btc_hold", "benchmark_eth_hold"):
            if hold_units is None:
                hold_units = {s: initial_cash * weights[s] / prices[s] for s in symbols}
            # Accumulate the frozen initial allocation under the per-order cap,
            # then retain units instead of silently rebalancing buy-and-hold.
            targets = {s: max(units[s], hold_units[s]) * prices[s] for s in symbols}
        next_prices = {s: float(histories[s][i+1]["open"]) for s in symbols}
        # Release cash before considering purchases when a strategy rotates.
        for s in sorted(symbols, key=lambda s: targets[s] - units[s]*prices[s]):
            px = prices[s]
            target = targets[s]
            if locked:
                target = min(target, units[s]*px)
            delta_value = target - units[s]*px
            if abs(delta_value) < 5: continue
            submitted += 1
            digest = hashlib.sha256(f"{seed}:{profile}:{timeframe}:{s}:{timestamps[i]}".encode()).digest()
            draw = int.from_bytes(digest[:8], "big") / 2**64
            side = 1 if delta_value > 0 else -1
            next_bar = histories[s][i+1]
            limit_px = px * (1.005 if side > 0 else .995)
            ref = float(next_bar["open"])
            exec_px = ref * (1+slippage if side > 0 else 1-slippage)
            # IOC is an immediate attempt at next open. Later intrabar highs
            # and lows cannot rescue an order that was not marketable then.
            crossing = exec_px <= limit_px if side > 0 else exec_px >= limit_px
            if draw < nonfill or not crossing:
                missed += 1; continue
            fraction = partial_fraction if nonfill <= draw < min(1, nonfill+partial) else 1.0
            if fraction < 1: partials += 1
            qty = min(abs(delta_value)/px, max_order_quote/exec_px) * fraction
            if side > 0:
                invested = sum(units[t]*next_prices[t] for t in symbols)
                open_equity = cash + invested
                asset_room = max(0.0, .25*open_equity - units[s]*next_prices[s])
                total_room = max(0.0, .50*open_equity - invested)
                qty = min(qty, cash/(exec_px*(1+fee)),
                          asset_room/(exec_px*(1+fee)), total_room/(exec_px*(1+fee)))
                cash -= qty*exec_px*(1+fee); units[s] += qty
            else:
                qty = min(qty, units[s]); cash += qty*exec_px*(1-fee); units[s] -= qty
            fee_total += qty*exec_px*fee; turnover += qty*exec_px; fills += int(qty > 0)
    if not equity_curve: raise ValueError(f"Insufficient candles for {profile} on {timeframe}")
    final_prices = {s: float(histories[s][-1]["close"]) for s in symbols}
    equity_curve.append(cash + sum(units[s]*final_prices[s] for s in symbols))
    metrics = _risk_metrics(equity_curve, 24*365 if timeframe == "1h" else (6*365 if timeframe == "4h" else 365))
    n = len(equity_curve); stages = []
    cut1, cut2 = int(n*.6), int(n*.8)
    for label, start, end in (("research", 0, cut1), ("validation", cut1, cut2), ("holdout", cut2, n)):
        stages.append({"stage": label, "observations": end-start,
                       "start_ms": timestamps[warmup+start] if end > start else None,
                       "end_ms": timestamps[warmup+end-1] if end > start else None,
                       **_risk_metrics(equity_curve[start:end], 24*365 if timeframe == "1h" else (6*365 if timeframe == "4h" else 365))})
    avg_equity = statistics.mean(equity_curve)
    return {**metrics, "stages": stages, "fees_quote": fee_total,
            "turnover_over_average_equity": turnover/avg_equity if avg_equity else 0,
            "orders_submitted": submitted, "orders_filled": fills,
            "orders_not_filled": missed, "partial_fills": partials,
            "stopped_on_daily_loss": locked,
            "daily_loss_lock_timestamp_ms": lock_timestamp,
            "evaluation_start_ms": timestamps[warmup], "evaluation_end_ms": timestamps[-1],
            "warmup_bars": warmup, "average_exposure": exposure_sum/(n-1),
            "max_single_asset_exposure": max_asset_exposure}


def strategy_backtest(data_dir, universe_file, timeframes=("1h", "4h", "1d"), *,
                      initial_cash=1000, max_order_quote=25, max_daily_loss=10,
                      partial_fill_fraction=.5, base_costs=None, stress_costs=None, seed="research-v1"):
    base_costs = base_costs or {"fee_rate": .001, "slippage": .0005, "ioc_nonfill_rate": .1, "partial_fill_rate": .05}
    stress_costs = stress_costs or {"fee_rate": .0015, "slippage": .001, "ioc_nonfill_rate": .2, "partial_fill_rate": .1}
    if min(initial_cash, max_order_quote, max_daily_loss) <= 0:
        raise ValueError("Cash and risk limits must be positive")
    for scenario in (base_costs, stress_costs):
        if (scenario["fee_rate"] < 0 or scenario["slippage"] < 0 or
                not 0 <= scenario["ioc_nonfill_rate"] <= 1 or
                not 0 <= scenario["partial_fill_rate"] <= 1):
            raise ValueError("Invalid fee, slippage, or fill scenario")
    if not 0 < partial_fill_fraction <= 1:
        raise ValueError("partial_fill_fraction must be in (0, 1]")
    manifest_path = Path(universe_file)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    symbols = manifest.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 8 or len(set(symbols)) != len(symbols):
        raise ValueError("Universe file must freeze 1 to 8 unique symbols")
    if any(symbol not in ALL_CANDIDATES for symbol in symbols):
        raise ValueError("Universe contains a symbol outside the documented research candidates")
    if any(tf not in TIMEFRAME_MS for tf in timeframes): raise ValueError("Timeframes must be 1h, 4h, or 1d")
    root = Path(data_dir)
    hourly, hashes = {}, {}
    for s in symbols:
        path = root / f"{s}-1h.csv"
        if not path.is_file(): raise ValueError(f"Missing hourly CSV: {path}")
        hourly[s] = _read_hour_csv(path)
        span_days = (hourly[s][-1]["timestamp"] - hourly[s][0]["timestamp"] + 3_600_000) / 86_400_000
        if span_days < 365:
            raise ValueError(f"{s} has only {span_days:.1f} days of hourly data; at least 365 are required")
        hashes[s] = hashlib.sha256(path.read_bytes()).hexdigest()
    reports = {}
    # Grid is intentionally listed but not scored by this portfolio engine: a
    # meaningful result needs a separate rung/inventory/order-book fill model.
    selected_profiles = [name for name in PROFILES if name != "grid_exploratory"]
    aligned_ranges = {}
    warmups = {tf: max(_warmup_bars(profile, tf) for profile in selected_profiles) for tf in timeframes}
    for tf in timeframes:
        sliced = {s: _aggregate(rows, tf) for s, rows in hourly.items()}
        common = sorted(set.intersection(*(set(r["timestamp"] for r in rows) for rows in sliced.values())))
        if not common: raise ValueError(f"No aligned candles for {tf}")
        aligned_ranges[tf] = {"start_ms": common[0], "end_ms": common[-1], "candles": len(common)}
        common_set = set(common)
        aligned = {s: [r for r in sliced[s] if r["timestamp"] in common_set] for s in symbols}
        reports[tf] = {}
        for profile in selected_profiles:
            reports[tf][profile] = {"metadata": PROFILES[profile], "parameters": PROFILE_PARAMETERS[profile], "base": _simulate(aligned, tf, profile,
                initial_cash=initial_cash, fee=base_costs["fee_rate"], slippage=base_costs["slippage"],
                nonfill=base_costs["ioc_nonfill_rate"], partial=base_costs["partial_fill_rate"],
                partial_fraction=partial_fill_fraction, seed=seed,
                max_order_quote=max_order_quote, max_daily_loss=max_daily_loss, warmup_bars=warmups[tf]),
                "stress": _simulate(aligned, tf, profile, initial_cash=initial_cash,
                fee=stress_costs["fee_rate"], slippage=stress_costs["slippage"],
                nonfill=stress_costs["ioc_nonfill_rate"], partial=stress_costs["partial_fill_rate"],
                partial_fraction=partial_fill_fraction, seed=seed,
                max_order_quote=max_order_quote, max_daily_loss=max_daily_loss, warmup_bars=warmups[tf])}
    benchmark_results, benchmark_hashes = {}, {}
    for tf in timeframes:
        benchmark_results[tf] = {}
        for symbol, profile in (("BTCUSDT", "benchmark_btc_hold"), ("ETHUSDT", "benchmark_eth_hold")):
            path = root / f"{symbol}-1h.csv"
            if not path.is_file():
                benchmark_results[tf][profile] = {"available": False, "reason": "benchmark_csv_missing"}
                continue
            raw = _read_hour_csv(path)
            span_days = (raw[-1]["timestamp"] - raw[0]["timestamp"] + TIMEFRAME_MS["1h"]) / 86_400_000
            if span_days < 365:
                benchmark_results[tf][profile] = {"available": False, "reason": "benchmark_history_under_365_days"}
                continue
            benchmark_hashes[symbol] = hashlib.sha256(path.read_bytes()).hexdigest()
            benchmark_bars = _aggregate(raw, tf)
            tf_range = aligned_ranges[tf]
            benchmark_bars = [r for r in benchmark_bars if tf_range["start_ms"] <= r["timestamp"] <= tf_range["end_ms"]]
            for row in benchmark_bars: row["symbol"] = symbol
            benchmark_results[tf][profile] = {"available": True,
                **_simulate({symbol: benchmark_bars}, tf, profile, initial_cash=initial_cash,
                    fee=base_costs["fee_rate"], slippage=base_costs["slippage"],
                    nonfill=base_costs["ioc_nonfill_rate"], partial=base_costs["partial_fill_rate"],
                    partial_fraction=partial_fill_fraction, seed=seed, max_order_quote=max_order_quote,
                    max_daily_loss=max_daily_loss, warmup_bars=warmups[tf])}
    return {"mode": "research_only_multi_asset_multi_timeframe", "execution_enabled": False,
            "universe": symbols, "universe_manifest": manifest, "timeframes": list(timeframes),
            "data": {"sha256_by_symbol": {**hashes, **benchmark_hashes},
                     "universe_file_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                     "research_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                     "start_ms": min(x["timestamp"] for x in hourly[symbols[0]]),
                     "end_ms": max(x["timestamp"] for x in hourly[symbols[0]]),
                     "hourly_rows_by_symbol": {s: len(rows) for s, rows in hourly.items()}},
            "assumptions": {"initial_cash": initial_cash, "base_costs": base_costs,
                            "stress_costs": stress_costs, "per_asset_cap": .25, "portfolio_cap": .50,
                            "max_order_quote": max_order_quote, "max_daily_loss_quote": max_daily_loss,
                            "partial_fill_fraction": partial_fill_fraction,
                            "common_warmup_bars_by_timeframe": warmups,
                            "ioc_fill_model": "next-open attempt only; intrabar high/low ignored",
                            "buy_hold_model": "fixed initial units accumulated under order cap; no periodic rebalance",
                            "stage_method": "fixed-parameter chronological 60/20/20 segmentation, not walk-forward selection",
                            "seed": seed}, "results": reports,
            "benchmarks": {"portfolio": ["cash", "buy_hold_equal_weight"],
                           "btc_eth": benchmark_results},
            "exploratory_not_scored": {"grid_exploratory": "Requires a separate rung, inventory, fee, and order-book fill model."},
            "aligned_ranges_by_timeframe": aligned_ranges,
            "warning": "Research simulation only; next-open cost model is approximate, survivorship and delisting bias require point-in-time datasets. No profile is AI-approved or executable."}


def _binance_json(path, params=None, timeout=15):
    url = "https://api.binance.com" + path
    if params: url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "QuantCryptoResearch/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response: return json.loads(response.read())


def download_hourly_history(symbols, start, end, data_dir):
    """Download closed 1h public candles, paginated and refusing to overwrite files."""
    def epoch_ms(value):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).timestamp()*1000)
    start_ms, end_ms = epoch_ms(start), epoch_ms(end)
    if start_ms % TIMEFRAME_MS["1h"] or end_ms % TIMEFRAME_MS["1h"] or start_ms >= end_ms:
        raise ValueError("Start/end must be UTC hour-aligned and start must precede end")
    normalized = list(dict.fromkeys(s.upper() for s in symbols))
    if not normalized or any(s not in ALLOWED_DATA_SYMBOLS for s in normalized):
        raise ValueError("Only the documented BTC/ETH baseline and candidate symbols are allowed")
    root = Path(data_dir); root.mkdir(parents=True, exist_ok=True)
    outputs, pending = [], []
    existing = [str(root / f"{symbol}-1h.csv") for symbol in normalized
                if (root / f"{symbol}-1h.csv").exists()]
    if existing: raise ValueError(f"Refusing to overwrite existing data: {existing}")
    now_ms = int(datetime.now(timezone.utc).timestamp()*1000)
    for symbol in normalized:
        path = root / f"{symbol}-1h.csv"
        rows, cursor = [], start_ms
        while cursor < min(end_ms, now_ms):
            batch = _binance_json("/api/v3/klines", {"symbol": symbol, "interval": "1h",
                "startTime": cursor, "endTime": min(end_ms-1, now_ms-1), "limit": 1000})
            closed = [r for r in batch if int(r[6]) < now_ms and int(r[0]) < end_ms]
            if not closed: break
            rows.extend(closed)
            next_cursor = int(closed[-1][0]) + TIMEFRAME_MS["1h"]
            if next_cursor <= cursor: raise RuntimeError("Binance pagination did not advance")
            cursor = next_cursor
            if cursor < min(end_ms, now_ms): time.sleep(.1)
        bars = [{"timestamp": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                 "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                 "quote_volume": float(r[7])} for r in rows]
        if bars: validate_bars(bars)
        if not bars or bars[0]["timestamp"] != start_ms:
            raise ValueError(f"{symbol} history does not begin at requested start; got {bars[0]['timestamp'] if bars else 'no bars'}")
        expected_last = min(end_ms, (now_ms // TIMEFRAME_MS["1h"]) * TIMEFRAME_MS["1h"]) - TIMEFRAME_MS["1h"]
        if bars[-1]["timestamp"] != expected_last:
            raise ValueError(f"{symbol} history ends early at {bars[-1]['timestamp']}; expected {expected_last}")
        outputs.append({"symbol": symbol, "candles": len(bars), "output": str(path),
                        "start_ms": bars[0]["timestamp"], "end_ms": bars[-1]["timestamp"]})
        pending.append((path, bars))
    for path, bars in pending:
        with path.open("x", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["timestamp", "open", "high", "low", "close", "volume", "quote_volume"])
            writer.writeheader(); writer.writerows(bars)
    return {"source": "Binance public Spot REST", "interval": "1h", "outputs": outputs,
            "account_access": False, "order_submission": False}


def screen_universe(data_dir, *, as_of=None, limit=8, market_snapshot=None):
    """Rank candidates from live REST or an auditable public-market snapshot."""
    if market_snapshot is None:
        info = _binance_json("/api/v3/exchangeInfo")
        rules = {x["symbol"]: x for x in info.get("symbols", [])}
        snapshot = None
    else:
        snapshot = json.loads(Path(market_snapshot).read_text(encoding="utf-8")) if isinstance(market_snapshot, (str, Path)) else market_snapshot
        rules = {s: {"symbol": s, "status": v.get("status"),
                     "isSpotTradingAllowed": v.get("spot_allowed", False)}
                 for s, v in snapshot.get("symbols", {}).items()}
    ranked = []
    for symbol in ALL_CANDIDATES:
        item = rules.get(symbol)
        row = {"symbol": symbol, "eligible": False}
        if not item or item.get("status") != "TRADING" or not item.get("isSpotTradingAllowed", True):
            row["reason"] = "not_trading_spot"; ranked.append(row); continue
        try:
            if snapshot is None:
                daily = _binance_json("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": 100})
                book = _binance_json("/api/v3/ticker/bookTicker", {"symbol": symbol})
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                quotes = [float(x[7]) for x in daily if len(x) > 7 and int(x[6]) < now_ms][-90:]
                bid, ask = float(book["bidPrice"]), float(book["askPrice"])
            else:
                item_snapshot = snapshot.get("symbols", {}).get(symbol, {})
                quotes = [float(x) for x in item_snapshot.get("closed_daily_quote_volumes", [])][-90:]
                bid, ask = float(item_snapshot["bid_price"]), float(item_snapshot["ask_price"])
            spread = (ask-bid)/((ask+bid)/2) if bid > 0 and ask >= bid else math.inf
            path = Path(data_dir) / f"{symbol}-1h.csv"
            bars = _read_hour_csv(path) if path.is_file() else []
            days = (bars[-1]["timestamp"]-bars[0]["timestamp"]+3_600_000)/86_400_000 if bars else 0
            complete = len(bars) >= 365*24 and days >= 365
            row.update({"median_90d_quote_volume": statistics.median(quotes) if len(quotes) == 90 else 0,
                        "current_top_book_spread": spread, "history_days": days,
                        "hourly_rows": len(bars), "hourly_history_365d": complete,
                        "daily_bars_available": len(quotes),
                        "eligible": len(quotes) == 90 and complete and math.isfinite(spread),
                        "reason": "eligible" if len(quotes) == 90 and complete and math.isfinite(spread)
                                 else "requires_365d_complete_hourly_history_and_90d_liquidity"})
        except Exception as exc:
            row["reason"] = type(exc).__name__
        ranked.append(row)
    eligible = sorted((r for r in ranked if r["eligible"]),
                      key=lambda r: (-r["median_90d_quote_volume"], r["current_top_book_spread"], r["symbol"]))
    chosen = eligible[:limit]
    ranked = eligible + sorted((r for r in ranked if not r["eligible"]), key=lambda r: r["symbol"])
    return {"as_of": as_of or (snapshot.get("as_of") if snapshot else None) or datetime.now(timezone.utc).isoformat(), "quote_asset": "USDT",
            "screening": {"history_min_days": 365, "liquidity_window_days": 90,
                          "ranking": "median daily quote volume descending, then top-book spread ascending",
                          "universe_size_requested": limit, "eligible_count": len(eligible)},
            "symbols": [r["symbol"] for r in chosen], "ranked_candidates": ranked,
            "alternate_candidates": list(ALTERNATES), "snapshot_only": True,
            "warning": "Current exchange status and top-book spread do not reconstruct historical listings/status. Use survivorship-free point-in-time universes for performance claims."}
