from __future__ import annotations


def sma(values, window):
    out = [None] * len(values)
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= window:
            running -= values[i - window]
        if i + 1 >= window:
            out[i] = running / window
    return out


def target_weights(bars, fast=24, slow=72):
    """Long-only hourly trend rule: 100% target when fast SMA exceeds slow SMA, else cash."""
    if fast < 2 or slow <= fast:
        raise ValueError("Require 2 <= fast < slow")
    closes = [float(x["close"]) for x in bars]
    f, s = sma(closes, fast), sma(closes, slow)
    return [1.0 if f[i] is not None and s[i] is not None and f[i] > s[i] else 0.0
            for i in range(len(bars))]

