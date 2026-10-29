from __future__ import annotations

import json

import pytest

from quant.crypto.adaptive import (AdaptiveController, PROFILES, market_features,
                                   validate_acceptance_gate, validate_recommendation)
from quant.crypto.adaptive_backtest import backtest_profiles

HOUR = 3_600_000


def histories(now_ms, offset=0):
    end = now_ms // HOUR * HOUR - HOUR + offset
    result = {}
    for symbol, seed in (("BTCUSDT", 100.0), ("ETHUSDT", 50.0)):
        rows = []
        for i in range(72):
            close = seed + i
            rows.append({"timestamp": end - (71-i)*HOUR, "open": close,
                         "high": close + 1, "low": close - 1,
                         "close": close, "volume": 100 + i})
        result[symbol] = rows
    return result


class Provider:
    def __init__(self, profile="cash"):
        self.profile = profile
    def recommend(self, features):
        return {"market_regime": "trend", "profile_id": self.profile,
                "confidence": .8, "rationale": "test recommendation",
                "model_version": "fake-v1"}


def test_market_features_reproducible_and_credentials_free():
    now = 1_000_000_000_000
    one = market_features(histories(now), now_ms=now)
    two = market_features(histories(now), now_ms=now)
    assert one == two
    assert set(one["symbols"]) == {"BTCUSDT", "ETHUSDT"}
    assert "api_key" not in json.dumps(one).lower()


@pytest.mark.parametrize("bad", [None, {}, {"market_regime":"x", "profile_id":"new_strategy",
    "confidence":.8, "rationale":"x", "model_version":"v"},
    {"market_regime":"x", "profile_id":"cash", "confidence":float("nan"),
     "rationale":"x", "model_version":"v"}])
def test_invalid_ai_output_rejected(bad):
    with pytest.raises(ValueError):
        validate_recommendation(bad)


def test_shadow_logs_but_never_enables_orders_and_handles_provider_failure(tmp_path):
    now = 1_000_000_000_000
    controller = AdaptiveController(tmp_path / "ai.sqlite", mode="shadow",
                                    provider=Provider(), now_ms=lambda: now)
    try:
        result = controller.evaluate(histories(now))
        assert result["orders_allowed"] is False
        assert result["active_profile"] == "sma_24_72"
        assert result["candidate_confirmations"] == 1
        controller.provider = type("Broken", (), {"recommend": lambda *_: (_ for _ in ()).throw(TimeoutError())})()
        failed = controller.evaluate(histories(now + HOUR))
        assert failed["state"] == "no_adaptation"
        assert failed["active_profile"] == "sma_24_72"
        assert controller.status()["candidate_profile"] is None
        assert controller.status()["candidate_confirmations"] == 0
        assert controller.status()["orders_allowed"] is False
    finally:
        controller.close()


def test_three_confirmations_and_24_hour_minimum_hold(tmp_path):
    now = 1_000_000_000_000
    clock = [now]
    controller = AdaptiveController(tmp_path / "ai.sqlite", mode="shadow",
                                    provider=Provider("cash"), now_ms=lambda: clock[0])
    try:
        for i in range(3):
            clock[0] = now + i * HOUR
            result = controller.evaluate(histories(clock[0]))
        assert result["candidate_confirmations"] == 3
        assert result["active_profile"] == "sma_24_72"
        for i in range(3, 25):
            clock[0] = now + i * HOUR
            result = controller.evaluate(histories(clock[0]))
        assert result["transitioned"] is True
        assert result["active_profile"] == "cash"
        assert controller.status()["transitions"] == 1
    finally:
        controller.close()


def test_restart_breaks_pending_confirmation_sequence(tmp_path):
    now = 1_000_000_000_000
    path = tmp_path / "ai.sqlite"
    controller = AdaptiveController(path, provider=Provider("cash"), now_ms=lambda: now)
    controller.evaluate(histories(now))
    controller.close()
    restarted = AdaptiveController(path, provider=Provider("cash"), now_ms=lambda: now+HOUR)
    try:
        assert restarted.status()["candidate_profile"] is None
        assert restarted.status()["candidate_confirmations"] == 0
    finally:
        restarted.close()


def test_stale_data_and_unknown_profile_do_not_transition(tmp_path):
    now = 1_000_000_000_000
    controller = AdaptiveController(tmp_path / "ai.sqlite", mode="shadow",
                                    provider=Provider("unexpected"), now_ms=lambda: now)
    try:
        result = controller.evaluate(histories(now - 3 * HOUR))
        assert result["state"] == "no_adaptation"
        assert result["active_profile"] == "sma_24_72"
        assert controller.status()["transitions"] == 0
    finally:
        controller.close()


def test_acceptance_gate_requires_all_manual_review_items(tmp_path):
    path = tmp_path / "acceptance.json"
    data = {"observation_30_days_passed": True,
            "backtest_costs_and_stages_passed": True,
            "shadow_decisions_replay_reviewed": True,
            "testnet_lifecycle_acceptance_passed": True,
            "reconciliation_passed": True, "human_approval": True,
            "approved_profiles": list(PROFILES), "approval_id": "review-001"}
    path.write_text(json.dumps(data))
    assert validate_acceptance_gate(path)["approval_id"] == "review-001"
    data["reconciliation_passed"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Acceptance gate failed"):
        validate_acceptance_gate(path)


def test_costed_profile_backtest_is_deterministic_and_models_ioc_nonfills():
    bars = {}
    for symbol, seed in (("BTCUSDT", 100.0), ("ETHUSDT", 50.0)):
        rows = []
        for i in range(100):
            close = seed + i
            rows.append({"timestamp": i*HOUR, "open": close, "high": close+1,
                         "low": close-1, "close": close, "volume": 100})
        bars[symbol] = rows
    result = backtest_profiles(bars, nonfill_rate=.25, partial_fill_rate=.2, seed="fixed")
    repeated = backtest_profiles(bars, nonfill_rate=.25, partial_fill_rate=.2, seed="fixed")
    assert result == repeated
    assert result["profiles"]["sma_24_72"]["fees_quote"] > 0
    assert result["profiles"]["sma_24_72"]["orders_not_filled"] > 0
    assert "stages" in result["profiles"]["cash"]
    assert "not the AI selector" in result["warning"]
