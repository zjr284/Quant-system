import csv
import json
from datetime import timedelta

from quant.crypto.research import _aggregate, screen_universe, strategy_backtest


def test_bollinger_exits_when_price_recovers_to_mean():
    from quant.crypto.research import _signals

    closes = [80.0]*181 + [120.0]*19 + [100.0, 120.0]
    signals = _signals("bollinger_20_2", [{"close": x} for x in closes])
    assert signals[200] == 1.0
    assert signals[201] == 0.0


def test_ioc_cannot_fill_from_a_later_intrabar_price():
    from quant.crypto.research import _simulate

    bars = [{"timestamp": i*3_600_000, "open": 102.0, "high": 105.0,
             "low": 99.0, "close": 100.0} for i in range(3)]
    result = _simulate({"SOLUSDT": bars}, "1h", "buy_hold_equal_weight",
                       initial_cash=1000, fee=.001, slippage=0, nonfill=0,
                       partial=0, partial_fraction=.5, seed="test", warmup_bars=0)
    assert result["orders_filled"] == 0
    assert result["orders_not_filled"] == 2
    assert result["final_equity"] == 1000


def test_partial_fill_applies_to_the_capped_order_and_final_mark_includes_fees():
    from quant.crypto.research import _simulate
    import pytest

    bars = [{"timestamp": i*3_600_000, "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.0} for i in range(2)]
    result = _simulate({"SOLUSDT": bars}, "1h", "buy_hold_equal_weight",
                       initial_cash=1000, fee=.001, slippage=0, nonfill=0,
                       partial=1, partial_fraction=.5, seed="test", warmup_bars=0)
    assert result["partial_fills"] == 1
    assert result["fees_quote"] == pytest.approx(12.5*.001)
    assert result["final_equity"] == pytest.approx(1000-12.5*.001)


def test_daily_timeframe_loss_lock_uses_previous_close_and_persists():
    from quant.crypto.research import _simulate

    bars = [{"timestamp": i*86_400_000, "open": 100.0, "high": 100.0,
             "low": close, "close": close}
            for i, close in enumerate((100.0, 80.0, 100.0, 100.0))]
    result = _simulate({"SOLUSDT": bars}, "1d", "buy_hold_equal_weight",
                       initial_cash=1000, fee=0, slippage=0, nonfill=0,
                       partial=0, partial_fraction=.5, seed="test", warmup_bars=0,
                       max_order_quote=100, max_daily_loss=10)
    assert result["stopped_on_daily_loss"] is True
    assert result["daily_loss_lock_timestamp_ms"] == 86_400_000
    assert result["orders_filled"] == 1


def test_benchmarks_share_each_timeframe_range_and_warmup(tmp_path, monkeypatch):
    import quant.crypto.research as research

    rows = [{"timestamp": i*3_600_000, "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.0, "volume": 1.0} for i in range(8775)]
    for symbol in ("SOLUSDT", "BTCUSDT", "ETHUSDT"):
        (tmp_path / f"{symbol}-1h.csv").write_text("fixture")
    manifest = tmp_path / "universe.json"
    manifest.write_text(json.dumps({"symbols": ["SOLUSDT"]}))
    monkeypatch.setattr(research, "_read_hour_csv", lambda path: [dict(row) for row in rows])
    calls = []

    def simulate(bars, tf, profile, **kwargs):
        candles = next(iter(bars.values()))
        calls.append((tf, profile, len(candles), kwargs["warmup_bars"]))
        return {}

    monkeypatch.setattr(research, "_simulate", simulate)
    research.strategy_backtest(tmp_path, manifest, ("1h", "4h", "1d"))
    for tf in ("1h", "4h", "1d"):
        assert len({(length, warmup) for t, _, length, warmup in calls if t == tf}) == 1


def test_aggregate_only_emits_full_utc_buckets():
    rows = [{"timestamp": i*3_600_000, "open": 10+i, "high": 12+i,
             "low": 9+i, "close": 11+i, "volume": 1, "closed": True}
            for i in range(9)]
    four_hour = _aggregate(rows, "4h")
    assert [r["timestamp"] for r in four_hour] == [0, 14_400_000]
    assert four_hour[0]["open"] == 10
    assert four_hour[0]["close"] == 14
    assert four_hour[0]["volume"] == 4


def test_strategy_research_report_is_frozen_and_research_only(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "SOLUSDT-1h.csv"
    fields = ["timestamp", "open", "high", "low", "close", "volume"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i in range(365*24):
            close = 100 + i*.01 + (i % 48)*.05
            writer.writerow({"timestamp": i*3_600_000, "open": close,
                             "high": close*1.002, "low": close*.998,
                             "close": close, "volume": 10})
    universe = tmp_path / "universe.json"
    universe.write_text(json.dumps({"as_of": "frozen", "symbols": ["SOLUSDT"]}))
    report = strategy_backtest(data, universe, ("1d",))
    assert report["execution_enabled"] is False
    assert report["data"]["sha256_by_symbol"]["SOLUSDT"]
    assert report["assumptions"]["base_costs"]["fee_rate"] == .001
    assert "sma_24_72" in report["results"]["1d"]
    assert "grid_exploratory" in report["exploratory_not_scored"]
    assert report["benchmarks"]["btc_eth"]["1d"]["benchmark_btc_hold"]["available"] is False


def test_universe_screen_does_not_pad_unqualified_candidates(tmp_path, monkeypatch):
    import quant.crypto.research as research

    def fake_json(path, params=None, timeout=15):
        if path.endswith("exchangeInfo"):
            return {"symbols": [{"symbol": "SOLUSDT", "status": "TRADING",
                                 "isSpotTradingAllowed": True}]}
        if path.endswith("klines"):
            return [[i, "1", "1", "1", "1", "1", i+1, "100", 1] for i in range(90)]
        return {"bidPrice": "99.9", "askPrice": "100.1"}

    monkeypatch.setattr(research, "_binance_json", fake_json)
    result = screen_universe(tmp_path, as_of="test", limit=8)
    assert result["symbols"] == []
    assert result["screening"]["eligible_count"] == 0


def test_history_downloader_paginates_and_refuses_overwrite(tmp_path, monkeypatch):
    import quant.crypto.research as research

    cursor = []
    now = research.datetime.now(research.timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    start_ms = int(start.timestamp()*1000)
    end_ms = int(end.timestamp()*1000)
    rows = []
    ts = start_ms
    while ts < end_ms:
        rows.append([ts, "100", "101", "99", "100", "5", ts+3_599_999, "500", 1])
        ts += 3_600_000

    def fake_json(path, params=None, timeout=15):
        cursor.append(params["startTime"])
        offset = (params["startTime"]-start_ms)//3_600_000
        return rows[offset:min(offset+7, len(rows))]

    monkeypatch.setattr(research, "_binance_json", fake_json)
    monkeypatch.setattr(research.time, "sleep", lambda _: None)
    result = research.download_hourly_history(["SOLUSDT"], start.isoformat(), end.isoformat(), tmp_path)
    assert result["outputs"][0]["candles"] == len(rows)
    assert len(cursor) > 1
    try:
        research.download_hourly_history(["SOLUSDT"], start.isoformat(), end.isoformat(), tmp_path)
    except ValueError as exc:
        assert "Refusing to overwrite" in str(exc)
    else:
        raise AssertionError("Downloader overwrote an existing history file")
