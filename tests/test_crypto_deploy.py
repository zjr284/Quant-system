import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import tarfile
import urllib.error
from pathlib import Path

import pytest

from quant.crypto.monitor import watch_symbols
from quant.crypto.observer import SignalObserver

def load_script(name):
    path=Path(__file__).resolve().parents[1]/"scripts"/(name+".py")
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


crypto_market_check=load_script("crypto_market_check")
backup=load_script("backup_crypto_observer").backup
build=load_script("package_crypto_observer").build


def test_live_observer_stops_on_access_restriction_without_retrying(tmp_path, monkeypatch, capsys):
    observer=SignalObserver(tmp_path/"observe.sqlite")
    calls=[]
    def denied(*args,**kwargs):
        calls.append(args)
        raise urllib.error.HTTPError("https://private-request.example/secret",451,"restricted",{},None)
    monkeypatch.setattr("websockets.connect",denied)
    with pytest.raises(ValueError,match="HTTP 451"):
        asyncio.run(watch_symbols(observer,reconnect_seconds=0))
    assert len(calls)==2  # One initial request per symbol; neither retries.
    assert observer.status()["service_running"] is False
    assert all(row["health"]["state"]=="blocked" for row in observer.status()["symbols"])
    captured=capsys.readouterr().out
    assert "451" in captured and "secret" not in captured
    observer.close()


def test_preflight_restriction_skips_websocket_and_fails_closed(monkeypatch,capsys):
    def denied(*args,**kwargs):
        raise urllib.error.HTTPError("https://private.example/secret",451,"restricted",{},None)
    def unexpected(*args,**kwargs):
        pytest.fail("Must not try a second channel after a restricted response")
    monkeypatch.setattr(crypto_market_check,"fetch_binance",denied)
    monkeypatch.setattr("websockets.connect",unexpected)
    assert asyncio.run(crypto_market_check.main())==2
    result=json.loads(capsys.readouterr().out)
    assert result["access_blocked"] is True and result["ok"] is False
    assert len(result["checks"])==2 and result["order_submission"] is False


def test_sqlite_backup_includes_committed_wal_and_refuses_overwrite(tmp_path):
    source=tmp_path/"live.sqlite"
    con=sqlite3.connect(source)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE evidence(value INTEGER)")
    con.execute("INSERT INTO evidence VALUES(42)")
    con.commit()
    output=tmp_path/"backup.sqlite"
    backup(source,output)
    with sqlite3.connect(output) as saved:
        assert saved.execute("SELECT value FROM evidence").fetchone()[0]==42
    with pytest.raises(ValueError,match="destination must be new"):
        backup(source,output)
    con.close()


def test_cloud_archive_has_no_runtime_state_and_hashes_match(tmp_path):
    output=tmp_path/"observer.tar.gz"
    build(output)
    with tarfile.open(output) as bundle:
        names=bundle.getnames()
        assert "deploy/tencent/quant-observer.service" in names
        assert not any(name.startswith((".venv/","runs/","data/",".git/",".codex/")) or name.endswith((".pem",".sqlite",".env")) for name in names)
        manifest=json.load(bundle.extractfile("DEPLOY_MANIFEST.json"))
        for path,digest in manifest["files"].items():
            assert hashlib.sha256(bundle.extractfile(path).read()).hexdigest()==digest


def test_testnet_archive_includes_only_deployment_sources_and_no_secrets(tmp_path):
    output=tmp_path/"testnet.tar.gz"
    build(output, mode="testnet")
    with tarfile.open(output) as bundle:
        names=bundle.getnames()
        assert "quant/crypto/testnet.py" in names
        assert "deploy/tencent/quant-testnet.service" in names
        assert "deploy/tencent/install-testnet.sh" in names
        assert "scripts/testnet_market_check.py" in names
        assert "docs/TENCENT_TESTNET.md" in names
        assert "quant/crypto/adaptive.py" in names
        assert "deploy/tencent/quant-adaptive-shadow.service" in names
        assert "deploy/tencent/quant-adaptive-testnet.service" in names
        assert "docs/CRYPTO_ADAPTIVE.md" in names
        assert not any(name.startswith((".venv/","runs/","data/",".git/",".codex/")) or name.endswith((".pem",".sqlite",".env")) for name in names)
        manifest=json.load(bundle.extractfile("DEPLOY_MANIFEST.json"))
        assert manifest["mode"]=="spot_testnet_shadow_and_gated_auto_orders"
        assert manifest["contains_credentials"] is False
        assert manifest["contains_database"] is False
        for path,digest in manifest["files"].items():
            assert hashlib.sha256(bundle.extractfile(path).read()).hexdigest()==digest
