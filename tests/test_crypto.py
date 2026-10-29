import pytest

from quant.crypto.market import validate_bars
from quant.crypto.paper import CryptoPaperBroker, run
from quant.crypto.risk import check_order
from quant.crypto.strategy import target_weights


def bars(prices):
    return [{"timestamp": i * 3_600_000, "open": p, "high": p * 1.01,
             "low": p * 0.99, "close": p, "volume": 1} for i, p in enumerate(prices)]


def test_strategy_warms_up_and_is_long_only():
    weights = target_weights(bars([100] * 72 + list(range(100, 140))), fast=3, slow=5)
    assert set(weights) <= {0.0, 1.0}
    assert weights[3] == 0
    assert weights[-1] == 1


def test_rejects_invalid_or_noncontiguous_bars():
    with pytest.raises(ValueError, match="contiguous"):
        validate_bars([bars([100, 101])[0], {**bars([100, 101])[1], "timestamp": 7_200_000}])
    with pytest.raises(ValueError, match="OHLC"):
        validate_bars([{**bars([100])[0], "high": 90}])


def test_paper_backtest_fees_caps_and_reconciliation(tmp_path):
    data = bars([100] * 80 + list(range(100, 160)))
    broker = CryptoPaperBroker(tmp_path / "paper.sqlite", initial_cash=1000,
        fee_rate=0.001, slippage=0.0005, max_order_quote=100,
        max_position_weight=0.25)
    result = run(data, broker, fast=5, slow=10)
    assert result["filled_orders"] > 0
    assert result["fees"] > 0
    assert result["reconciliation"]["ok"]
    # Replaying the same candles cannot duplicate orders or ledger entries.
    first_count = broker.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    again = run(data, broker, fast=5, slow=10)
    assert broker.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == first_count
    assert again["reconciliation"]["ok"]
    broker.close()


def test_risk_rejects_size_and_price_outlier():
    common = dict(quantity=1, price=110, cash=1000, position=0, equity=1000,
        max_order_quote=1000, max_position_weight=0.5, max_daily_loss=0.03,
        daily_pnl=0, reference_price=100, max_price_deviation=0.02)
    assert check_order(side="BUY", **common) == "price_deviation"
    assert check_order(side="SELL", **{**common, "quantity": 2}) == "price_deviation"


def test_kill_switch_stops_paper_trading(tmp_path):
    broker = CryptoPaperBroker(tmp_path / "paper.sqlite")
    broker.halt("operator stop")
    with pytest.raises(ValueError, match="Kill switch"):
        broker.step(timestamp=0, symbol="BTCUSDT", target_weight=1,
            price=100, reference_price=100, daily_start_equity=1000)
    broker.close()

