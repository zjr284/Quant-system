"""Fail-closed configuration and acceptance gate for any live execution."""
from __future__ import annotations

import json
from pathlib import Path

LIVE_SYMBOLS = {"BTCUSDT", "ETHUSDT"}
REQUIRED_LIVE_LIMITS = ("max_order_quote", "max_daily_loss_quote", "max_price_deviation",
                        "max_data_delay_seconds", "allowed_symbols")


def validate_live_config(config: dict) -> list[str]:
    errors = []
    for key in REQUIRED_LIVE_LIMITS:
        if key not in config or config[key] in (None, "", [], 0, 0.0):
            errors.append(f"missing_or_invalid:{key}")
    if config.get("allowed_symbols") and not set(config["allowed_symbols"]).issubset(LIVE_SYMBOLS):
        errors.append("symbol_not_whitelisted")
    for key in ("max_order_quote", "max_daily_loss_quote", "max_data_delay_seconds"):
        if key in config:
            try:
                if float(config[key]) <= 0:
                    errors.append(f"must_be_positive:{key}")
            except (ValueError, TypeError):
                errors.append(f"invalid_number:{key}")
    if "max_price_deviation" in config:
        try:
            if not 0 < float(config["max_price_deviation"]) <= 0.05:
                errors.append("invalid:max_price_deviation")
        except (ValueError, TypeError):
            errors.append("invalid_number:max_price_deviation")
    return errors


def live_gate(*, enabled: bool, config: dict, evidence_path: str | Path | None,
              confirmation: str | None) -> dict:
    errors = validate_live_config(config)
    if not enabled:
        errors.append("live_not_explicitly_enabled")
    if confirmation != "I_ACCEPT_REAL_ORDERS":
        errors.append("explicit_real_order_confirmation_missing")
    evidence = {}
    if not evidence_path or not Path(evidence_path).is_file():
        errors.append("acceptance_evidence_missing")
    else:
        try:
            evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
            checks = evidence.get("checks", {})
            required = ("observation_days_30", "testnet_order_lifecycle", "reconciliation",
                        "persistent_host", "strategy_versioned")
            errors.extend(f"acceptance_failed:{key}" for key in required if checks.get(key) is not True)
        except (OSError, ValueError, AttributeError):
            errors.append("acceptance_evidence_invalid")
    return {"prerequisites_passed": not errors, "errors": errors}
