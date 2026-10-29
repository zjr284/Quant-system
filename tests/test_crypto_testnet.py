from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from quant.crypto.orders import BinanceTestnetOrderClient
from quant.crypto.testnet import REST, TestnetTrader


class Response:
    def __init__(self, data): self._data = data
    def data(self): return self._data


class FakeApi:
    def __init__(self):
        self.orders = []
        self.balances = [{"asset": "USDT", "free": "1000", "locked": "0"}]
    def get_open_orders(self): return Response([])
    def get_account(self, **kwargs):
        return Response({"balances": self.balances})
    def ticker_price(self, *, symbol): return Response({"symbol": symbol, "price": "100"})
    def exchange_info(self, *, symbol):
        return Response({"symbols": [{"symbol": symbol, "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
        ]}]})
    def new_order(self, **kwargs):
        self.orders.append(kwargs)
        return Response({"symbol": kwargs["symbol"], "status": "FILLED", "executedQty": kwargs["quantity"],
                         "clientOrderId": kwargs["new_client_order_id"]})
    def get_order(self, **kwargs): return Response({"status": "FILLED"})


class FakeClient:
    def __init__(self): self.rest_api = FakeApi()


def bars(now_ms, n=72):
    end = (now_ms // 3_600_000) * 3_600_000 - 3_600_000
    return [{"timestamp": end - (n-i-1)*3_600_000, "close": 100+i,
             "open": 100+i, "high": 100+i, "low": 100+i, "volume": 1}
            for i in range(n)]


def trader(tmp_path, client=None, now=1_000_000_000_000, **kwargs):
    client = client or FakeClient()
    instance = TestnetTrader(tmp_path / "testnet.sqlite", client, fast=24, slow=72,
        now_ms=lambda: now, enabled=True, confirmation="I_ACCEPT_TESTNET_ORDERS", **kwargs)
    return instance, client


def test_requires_explicit_testnet_opt_in(tmp_path):
    with pytest.raises(ValueError, match="explicit confirmation"):
        TestnetTrader(tmp_path / "a.sqlite", FakeClient(), now_ms=lambda: 1_000_000_000_000)


def test_testnet_host_cannot_be_live(tmp_path):
    with pytest.raises(ValueError, match="restricted to Binance Spot Testnet"):
        BinanceTestnetOrderClient(FakeClient(), "https://api.binance.com")


def test_closed_signal_sends_one_capped_ioc_and_dedupes(tmp_path):
    now = 1_000_000_000_000
    bot, client = trader(tmp_path, now=now, max_order_quote=25)
    try:
        history = bars(now)
        result = bot.process_closed_bar("ETHUSDT", history)
        assert result["order_sent"] is True
        assert result["request"]["type"] == "LIMIT"
        assert result["request"]["time_in_force"] == "IOC"
        assert Decimal(result["request"]["quantity"]) * Decimal(result["request"]["price"]) <= 25
        assert bot.process_closed_bar("ETHUSDT", history)["state"] == "duplicate_ignored"
        assert len(client.rest_api.orders) == 1
    finally:
        bot.close()


def test_uncertain_order_state_persists_lock(tmp_path):
    now = 1_000_000_000_000
    client = FakeClient()
    client.rest_api.new_order = lambda **kwargs: (_ for _ in ()).throw(TimeoutError())
    bot, _ = trader(tmp_path, client=client, now=now)
    try:
        with pytest.raises(RuntimeError, match="submit_result_unknown"):
            bot.process_closed_bar("ETHUSDT", bars(now))
        assert bot.status()["locked"] is True
    finally:
        bot.close()
    with sqlite3.connect(tmp_path / "testnet.sqlite") as db:
        assert db.execute("SELECT status FROM bot_orders").fetchone()[0] == "UNKNOWN"
        assert db.execute("SELECT value FROM bot_state WHERE key='lock'").fetchone()[0] == "submit_result_unknown"


def test_startup_open_order_locks(tmp_path):
    client = FakeClient()
    client.rest_api.get_open_orders = lambda: Response([{"symbol": "ETHUSDT"}])
    with pytest.raises(RuntimeError, match="open_orders_at_startup"):
        trader(tmp_path, client=client)


def test_adaptive_profiles_only_accept_compiled_profiles_and_cash_sends_no_order(tmp_path):
    now = 1_000_000_000_000
    client = FakeClient()
    bot = TestnetTrader(tmp_path / "adaptive.sqlite", client, fast=24, slow=72,
        now_ms=lambda: now, enabled=True, confirmation="I_ACCEPT_TESTNET_ORDERS",
        adaptive_profiles=True)
    try:
        result = bot.process_closed_bar("BTCUSDT", bars(now), profile_id="cash")
        assert result["weight"] == 0
        assert result["order_sent"] is False
        assert client.rest_api.orders == []
        with pytest.raises(RuntimeError, match="invalid_adaptive_profile"):
            bot.process_closed_bar("ETHUSDT", bars(now), profile_id="arbitrary_code")
    finally:
        bot.close()


def test_manual_halt_is_persistent_across_restart(tmp_path):
    now = 1_000_000_000_000
    path = tmp_path / "halt.sqlite"
    client = FakeClient()
    bot = TestnetTrader(path, client, fast=24, slow=72, now_ms=lambda: now,
        enabled=True, confirmation="I_ACCEPT_TESTNET_ORDERS")
    bot.halt("manual_shutdown")
    bot.close()
    restarted = TestnetTrader(path, client, fast=24, slow=72, now_ms=lambda: now,
        enabled=True, confirmation="I_ACCEPT_TESTNET_ORDERS")
    try:
        assert restarted.status()["locked"] is True
        assert restarted.status()["lock_reason"] == "manual_shutdown"
        with pytest.raises(RuntimeError, match="manual_or_prior_lock"):
            restarted.process_closed_bar("BTCUSDT", bars(now))
        assert client.rest_api.orders == []
    finally:
        restarted.close()


def test_manual_halt_reset_requires_no_open_orders(tmp_path):
    now = 1_000_000_000_000
    path = tmp_path / "halt.sqlite"
    client = FakeClient()
    bot = TestnetTrader(path, client, fast=24, slow=72, now_ms=lambda: now,
        enabled=True, confirmation="I_ACCEPT_TESTNET_ORDERS")
    bot.halt("manual_shutdown")
    with pytest.raises(ValueError, match="Open orders exist"):
        client.rest_api.get_open_orders = lambda: Response([{"symbol":"BTCUSDT"}])
        bot.reset_manual_halt()
    assert bot.status()["locked"] is True
    client.rest_api.get_open_orders = lambda: Response([])
    result = bot.reset_manual_halt()
    assert result["locked"] is False
    assert result["orders_submitted"] is False
    assert bot.status()["locked"] is False
    bot.close()


def test_daily_loss_lock_persists_and_rejects_exposure_increase(tmp_path):
    now = 1_000_000_000_000
    bot, client = trader(tmp_path, now=now, max_daily_loss_quote=10)
    day_key = f"day:{now // 86_400_000}"
    bot.db.execute("INSERT INTO bot_state VALUES(?,?)", (day_key, "1010"))
    try:
        result = bot.process_closed_bar("BTCUSDT", bars(now))
        assert result["order_sent"] is False
        assert bot.status()["locked"] is True
        assert bot.status()["lock_reason"] == "daily_loss_limit"
        assert client.rest_api.orders == []
        next_now = now + 3_600_000
        bot.now_ms = lambda: next_now
        result = bot.process_closed_bar("ETHUSDT", bars(next_now))
        assert result["order_sent"] is False
        assert bot.status()["lock_reason"] == "daily_loss_limit"
        assert client.rest_api.orders == []
    finally:
        bot.close()
    restarted, _ = trader(tmp_path, client=client, now=next_now)
    try:
        assert restarted.status()["lock_reason"] == "daily_loss_limit"
        restarted.now_ms = lambda: next_now + 3_600_000
        result = restarted.process_closed_bar("BTCUSDT", bars(next_now + 3_600_000))
        assert result["order_sent"] is False
        assert restarted.status()["lock_reason"] == "daily_loss_limit"
    finally:
        restarted.close()


def test_daily_loss_lock_allows_only_existing_position_reduction(tmp_path):
    now = 1_000_000_000_000
    client = FakeClient()
    client.rest_api.balances = [
        {"asset": "USDT", "free": "1000", "locked": "0"},
        {"asset": "BTC", "free": "0.5", "locked": "0"},
    ]
    bot = TestnetTrader(tmp_path / "reduce-only.sqlite", client, fast=24, slow=72,
        now_ms=lambda: now, enabled=True, confirmation="I_ACCEPT_TESTNET_ORDERS",
        max_daily_loss_quote=10, adaptive_profiles=True)
    day_key = f"day:{now // 86_400_000}"
    bot.db.execute("INSERT INTO bot_state VALUES(?,?)", (day_key, "1061"))
    try:
        result = bot.process_closed_bar("BTCUSDT", bars(now), profile_id="cash")
        assert bot.status()["lock_reason"] == "daily_loss_limit"
        assert result["order_sent"] is True
        assert result["request"]["side"] == "SELL"
        assert Decimal(result["request"]["quantity"]) <= Decimal("0.5")
        assert client.rest_api.orders[0]["side"] == "SELL"
    finally:
        bot.close()


def test_daily_loss_lock_requires_explicit_human_review_to_reset(tmp_path):
    now = 1_000_000_000_000
    client = FakeClient()
    bot, _ = trader(tmp_path, client=client, now=now)
    bot.halt("daily_loss_limit")
    try:
        client.rest_api.get_open_orders = lambda: Response([{"symbol": "BTCUSDT"}])
        with pytest.raises(ValueError, match="Open orders exist"):
            bot.reset_daily_loss_lock_after_review()
        assert bot.status()["locked"] is True
        client.rest_api.get_open_orders = lambda: Response([])
        result = bot.reset_daily_loss_lock_after_review()
        assert result["lock_reset"] == "daily_loss_limit"
        assert bot.status()["locked"] is False
        assert result["orders_submitted"] is False
    finally:
        bot.close()

