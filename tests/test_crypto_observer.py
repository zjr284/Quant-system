import asyncio

import pytest

from quant.crypto.observer import HOUR_MS, SignalObserver


def bars(start, count):
    return [{"timestamp": i*HOUR_MS, "open":100+i, "high":101+i, "low":99+i,
             "close":100+i, "volume":1, "closed":True} for i in range(start, start+count)]


def test_restart_and_duplicate_replay_preserve_signals_and_hashes(tmp_path):
    path = tmp_path / "watch.sqlite"
    observer = SignalObserver(path, fast=2, slow=3, now_ms=lambda: 5*HOUR_MS)
    result = observer.ingest("BTCUSDT", bars(0,5))
    assert result["new_signals"] == 3
    expected = [tuple(r) for r in observer.db.execute("SELECT ts,target_weight,input_hash FROM observer_signals ORDER BY ts")]
    observer.close()
    restarted = SignalObserver(path, fast=2, slow=3, now_ms=lambda: 5*HOUR_MS)
    assert restarted.ingest("BTCUSDT", bars(0,5))["new_candles"] == 0
    assert [tuple(r) for r in restarted.db.execute("SELECT ts,target_weight,input_hash FROM observer_signals ORDER BY ts")] == expected
    assert all(r[1] == 0.25 for r in expected)
    restarted.close()


def test_long_outage_backfill_pages_and_deduplicates_after_recovery(tmp_path):
    observer = SignalObserver(tmp_path/"watch.sqlite", fast=2, slow=3, now_ms=lambda: 1006*HOUR_MS)
    observer.ingest("BTCUSDT", bars(0,3))
    calls=[]
    def fetch(symbol, *, limit, start, end, allow_empty):
        calls.append(start)
        index=start//HOUR_MS
        return bars(index,min(limit,1006-index))
    result=asyncio.run(observer.sync_symbol("BTCUSDT", fetch=fetch))
    assert result["new_candles"]==1003 and len(calls)==2
    assert observer.last_timestamp("BTCUSDT")==1005*HOUR_MS
    assert observer.status()["symbols"][0]["data_current"] is True
    assert asyncio.run(observer.sync_symbol("BTCUSDT", fetch=fetch))["new_candles"]==0
    observer.close()


def test_conflicting_replay_and_missing_hour_roll_back_entire_batch(tmp_path):
    observer=SignalObserver(tmp_path/"watch.sqlite",fast=2,slow=3,now_ms=lambda:10*HOUR_MS)
    observer.ingest("ETHUSDT", bars(0,3))
    with pytest.raises(ValueError,match="Conflicting"):
        observer.ingest("ETHUSDT",[{**bars(2,1)[0], "volume":2}, *bars(3,1)])
    assert observer.last_timestamp("ETHUSDT")==2*HOUR_MS
    with pytest.raises(ValueError,match="Missing"):
        observer.ingest("ETHUSDT",bars(4,1))
    assert observer.last_timestamp("ETHUSDT")==2*HOUR_MS
    observer.close()


def test_missing_latest_candle_is_blocked_and_stale(tmp_path):
    observer=SignalObserver(tmp_path/"watch.sqlite",fast=2,slow=3,now_ms=lambda:5*HOUR_MS)
    def fetch(*args,**kwargs): return []
    with pytest.raises(ValueError,match="latest closed"):
        asyncio.run(observer.sync_symbol("BTCUSDT",fetch=fetch))
    s=observer.status()["symbols"][0]
    assert s["data_current"] is False and s["health"]["state"]=="blocked"
    observer.close()


def test_unclosed_nonfinite_and_strategy_change_are_rejected(tmp_path):
    path=tmp_path/"watch.sqlite"
    observer=SignalObserver(path,fast=2,slow=3,now_ms=lambda:5*HOUR_MS)
    with pytest.raises(ValueError,match="Unclosed"):
        observer.ingest("BTCUSDT",bars(5,1))
    with pytest.raises(ValueError,match="numeric"):
        observer.ingest("BTCUSDT",[{**bars(0,1)[0],"close":float("nan")}])
    observer.close()
    with pytest.raises(ValueError,match="strategy/code changed"):
        SignalObserver(path,fast=2,slow=4)


def test_status_does_not_create_missing_database(tmp_path):
    path=tmp_path/"missing.sqlite"
    with pytest.raises(ValueError,match="does not exist"):
        SignalObserver.read_status(path)
    assert not path.exists()


def test_recent_data_alone_does_not_report_a_running_service(tmp_path):
    clock=[5*HOUR_MS]
    observer=SignalObserver(tmp_path/"watch.sqlite",fast=2,slow=3,now_ms=lambda:clock[0])
    for symbol in ("BTCUSDT","ETHUSDT"):
        observer.ingest(symbol,bars(0,5))
        observer.health(symbol,"synced")
    assert all(s["data_current"] for s in observer.status()["symbols"])
    assert observer.status()["service_running"] is False
    for symbol in ("BTCUSDT","ETHUSDT"):
        observer.health(symbol,"streaming")
    assert observer.status()["service_running"] is True
    clock[0]+=90_001
    assert observer.status()["service_running"] is False
    observer.close()
