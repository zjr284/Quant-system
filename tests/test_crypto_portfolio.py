from decimal import Decimal

import pytest

from quant.crypto.orders import SymbolRules, BinanceTestnetOrderClient
from quant.crypto.portfolio import CryptoPortfolioPaper
from quant.crypto.safety import live_gate


def test_signal_journal_deduplicates_and_never_sends_order(tmp_path):
    p = CryptoPortfolioPaper(tmp_path / "portfolio.sqlite")
    e1 = p.signal(timestamp=3600000,symbol="BTCUSDT",target_weight=1,close=60000)
    e2 = p.signal(timestamp=3600000,symbol="BTCUSDT",target_weight=1,close=60000)
    assert e1["order_sent"] is False and e2["order_sent"] is False
    assert p.status()["signal_count"] == 1
    p.close()


def test_dual_asset_paper_portfolio_respects_caps_and_reconciles(tmp_path):
    p = CryptoPortfolioPaper(tmp_path / "portfolio.sqlite",initial_cash=10000,max_order_quote=10000)
    result = p.paper_rebalance(timestamp=1,prices={"BTCUSDT":50000,"ETHUSDT":2500},
                               target_weights={"BTCUSDT":0.25,"ETHUSDT":0.25})
    assert sum(o["quantity"]*o["price"] for o in result["orders"]) <= 5000
    assert result["reconciliation"]["ok"]
    assert p.paper_rebalance(timestamp=1,prices={"BTCUSDT":50000,"ETHUSDT":2500},
                              target_weights={"BTCUSDT":0.25,"ETHUSDT":0.25})["state"] == "already_processed"
    p.close()


def test_portfolio_daily_loss_trips_persistent_lock(tmp_path):
    p=CryptoPortfolioPaper(tmp_path/"loss.sqlite",initial_cash=10000,max_order_quote=10000,max_daily_loss=0.03)
    p.paper_rebalance(timestamp=3600000,prices={"BTCUSDT":50000,"ETHUSDT":2500},
                      target_weights={"BTCUSDT":0.25,"ETHUSDT":0.25})
    result=p.paper_rebalance(timestamp=7200000,prices={"BTCUSDT":40000,"ETHUSDT":2000},
                             target_weights={"BTCUSDT":0,"ETHUSDT":0})
    assert result["state"]=="HALTED" and p.status()["locked"]==1
    p.close()


def test_exchange_filters_and_ioc_payload():
    rules=SymbolRules.from_exchange_info({"symbols":[{"symbol":"BTCUSDT","filters":[
        {"filterType":"PRICE_FILTER","tickSize":"0.1"},
        {"filterType":"LOT_SIZE","stepSize":"0.00001","minQty":"0.00001"},
        {"filterType":"MIN_NOTIONAL","minNotional":"5"}]}]},"BTCUSDT")
    order=rules.limit_ioc(symbol="BTCUSDT",side="BUY",quantity="0.001239",limit_price="60000.09",
        reference_price="60000",max_order_quote=100,max_price_deviation=0.01)
    assert order == {"symbol":"BTCUSDT","side":"BUY","type":"LIMIT","time_in_force":"IOC",
                     "quantity":"0.00123","price":"60000.0"}
    with pytest.raises(ValueError,match="max_order_quote"):
        rules.limit_ioc(symbol="BTCUSDT",side="BUY",quantity="0.01",limit_price="60000",
            reference_price="60000",max_order_quote=100,max_price_deviation=0.01)


def test_testnet_client_rejects_production_and_uses_deterministic_id():
    class Rest:
        def new_order(self,**kwargs): return kwargs
    class Client: rest_api=Rest()
    with pytest.raises(ValueError,match="Testnet"):
        BinanceTestnetOrderClient(Client(),"https://api.binance.com")
    client=BinanceTestnetOrderClient(Client(),"https://testnet.binance.vision")
    order={"symbol":"ETHUSDT","side":"BUY","type":"LIMIT","time_in_force":"IOC","quantity":"0.1","price":"2000"}
    a=client.submit_ioc(order,strategy_version="v1",timestamp=123)
    b=client.submit_ioc(order,strategy_version="v1",timestamp=123)
    assert a["new_client_order_id"]==b["new_client_order_id"]


def test_live_gate_fails_closed_without_explicit_30_day_acceptance(tmp_path):
    cfg={"max_order_quote":20,"max_daily_loss_quote":10,"max_price_deviation":0.01,
         "max_data_delay_seconds":30,"allowed_symbols":["BTCUSDT","ETHUSDT"]}
    gate=live_gate(enabled=True,config=cfg,evidence_path=tmp_path/"missing.json",confirmation="I_ACCEPT_REAL_ORDERS")
    assert gate["prerequisites_passed"] is False and "acceptance_evidence_missing" in gate["errors"]
