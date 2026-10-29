"""Fail-closed validation and manifest generation for hourly market CSVs."""
from __future__ import annotations

import csv
import hashlib
import io
import math
from datetime import datetime, timezone
from pathlib import Path

HOUR_MS = 3_600_000
TIMESTAMP_COLUMNS = ("timestamp", "open_time", "time")
REQUIRED_VALUE_COLUMNS = ("open", "high", "low", "close", "volume")


def _timestamp(value: str) -> int:
    value = (value or "").strip()
    if not value:
        raise ValueError("empty timestamp")
    try:
        result = int(value)
        # The project data contract stores Unix epoch milliseconds.
        if result < 100_000_000_000:
            raise ValueError("timestamp integer must be Unix epoch milliseconds")
        return result
    except ValueError as numeric_error:
        if "must be Unix" in str(numeric_error):
            raise
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be epoch milliseconds or ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("ISO timestamp must include timezone (use Z for UTC)")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _number(value: str, field: str, row_number: int) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"row {row_number}: {field} is not numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"row {row_number}: {field} must be finite")
    return parsed


def _read_file(path: Path, *, now_ms: int, max_age_hours: float | None) -> dict:
    if not path.is_file():
        raise ValueError(f"CSV does not exist: {path.name}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name}: CSV must be UTF-8 encoded") from exc
    with io.StringIO(decoded, newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError(f"duplicate CSV headers: {path.name}")
        time_column = next((name for name in TIMESTAMP_COLUMNS if name in fields), None)
        missing = [name for name in REQUIRED_VALUE_COLUMNS if name not in fields]
        if time_column is None or missing:
            needed = [*REQUIRED_VALUE_COLUMNS, "timestamp (or open_time/time)"]
            raise ValueError(f"{path.name}: missing required columns {missing or needed}")
        rows, warnings = [], []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"row {row_number}: more values than CSV headers")
            if not row or all(value is None or not value.strip() for value in row.values()):
                raise ValueError(f"row {row_number}: blank row")
            try:
                ts = _timestamp(row[time_column])
            except ValueError as exc:
                raise ValueError(f"row {row_number}: {exc}") from exc
            if ts % HOUR_MS:
                raise ValueError(f"row {row_number}: candle open time is not UTC-hour aligned")
            bar = {name: _number(row[name], name, row_number) for name in REQUIRED_VALUE_COLUMNS}
            if min(bar[name] for name in ("open", "high", "low", "close")) <= 0:
                raise ValueError(f"row {row_number}: OHLC prices must be positive")
            if bar["volume"] < 0:
                raise ValueError(f"row {row_number}: volume cannot be negative")
            if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]) or bar["low"] > bar["high"]:
                raise ValueError(f"row {row_number}: inconsistent OHLC bounds")
            if "quote_volume" in fields:
                qv = _number(row["quote_volume"], "quote_volume", row_number)
                if qv < 0:
                    raise ValueError(f"row {row_number}: quote_volume cannot be negative")
            else:
                qv = None
            if bar["volume"] == 0:
                warnings.append({"row": row_number, "warning": "zero_volume"})
            if ts + HOUR_MS > now_ms:
                raise ValueError(f"row {row_number}: candle is not fully closed at check time")
            rows.append({"timestamp": ts, **bar, "quote_volume": qv})

    if not rows:
        raise ValueError(f"CSV has no data rows: {path.name}")
    timestamps = [row["timestamp"] for row in rows]
    close_warnings = []
    for index in range(1, len(timestamps)):
        delta = timestamps[index] - timestamps[index - 1]
        if delta <= 0:
            kind = "duplicate timestamp" if delta == 0 else "out-of-order timestamp"
            raise ValueError(f"{path.name}: {kind} at data row {index + 2}")
        if delta != HOUR_MS:
            missing = delta // HOUR_MS - 1
            raise ValueError(f"{path.name}: hourly gap after {timestamps[index - 1]} ({missing} missing candle(s))")
        change = rows[index]["close"] / rows[index - 1]["close"] - 1
        if abs(change) >= 0.5:
            close_warnings.append({"row": index + 2, "warning": "close_change_at_least_50_percent",
                                  "change_pct": round(change * 100, 4)})

    latest_age_hours = (now_ms - (timestamps[-1] + HOUR_MS)) / HOUR_MS
    if latest_age_hours < 0:
        raise ValueError(f"{path.name}: latest candle closes in the future")
    if max_age_hours is not None and latest_age_hours > max_age_hours:
        raise ValueError(f"{path.name}: latest closed candle is stale ({latest_age_hours:.2f}h old; limit {max_age_hours}h)")
    return {
        "file": path.name,
        "sha256": digest,
        "rows": len(rows),
        "start_ms": timestamps[0],
        "end_ms": timestamps[-1],
        "start_utc": datetime.fromtimestamp(timestamps[0] / 1000, timezone.utc).isoformat(),
        "end_utc": datetime.fromtimestamp(timestamps[-1] / 1000, timezone.utc).isoformat(),
        "span_hours_inclusive": (timestamps[-1] - timestamps[0]) // HOUR_MS + 1,
        "latest_closed_candle_age_hours": round(latest_age_hours, 4),
        "zero_volume_rows": len(warnings),
        "large_hourly_close_changes": len(close_warnings),
        "warnings": (warnings + close_warnings)[:20],
        "_timestamps": timestamps,
    }


def check_hourly_csvs(*, files: dict[str, str | Path], purpose: str,
                      source_label: str, min_days: float = 365,
                      max_age_hours: float | None = None, require_exact_alignment: bool = True,
                      now: datetime | None = None, code_path: str | Path | None = None) -> dict:
    """Check hourly CSV integrity and emit a reproducible, content-hashed manifest.

    ``source_label`` is an operator assertion; hashes prove file identity, not source
    authenticity. Live data must also pass application health and account checks.
    """
    if not files or not source_label.strip():
        raise ValueError("At least one CSV and a non-empty source label are required")
    if purpose not in ("research", "live"):
        raise ValueError("purpose must be research or live")
    if not math.isfinite(min_days) or min_days < 0 or (
            max_age_hours is not None and (not math.isfinite(max_age_hours) or max_age_hours <= 0)):
        raise ValueError("min_days must be non-negative and max_age_hours positive")
    checked = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        raise ValueError("Check time must include a timezone")
    checked = checked.astimezone(timezone.utc)
    now_ms = int(checked.timestamp() * 1000)
    reports = {}
    errors = []
    loaded = {}
    for symbol, raw_path in files.items():
        path = Path(raw_path)
        try:
            if not symbol.isalnum() or len(symbol) > 20:
                raise ValueError("symbol must contain only letters and digits")
            report = _read_file(path, now_ms=now_ms,
                                max_age_hours=max_age_hours if purpose == "live" else None)
            required_rows = math.ceil(min_days * 24)
            if report["rows"] < required_rows:
                raise ValueError(f"{symbol}: {report['rows']} rows; need at least {required_rows} for {min_days:g} days")
            reports[symbol] = report
            loaded[symbol] = report.pop("_timestamps")
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            errors.append({"symbol": symbol, "file": path.name, "error": str(exc)})
    if not errors and require_exact_alignment and len(loaded) > 1:
        reference_symbol = next(iter(loaded))
        reference = loaded[reference_symbol]
        for symbol, timestamps in loaded.items():
            if timestamps != reference:
                errors.append({"symbol": symbol, "file": reports[symbol]["file"],
                               "error": f"timestamp sequence is not exactly aligned with {reference_symbol}"})
    if code_path and Path(code_path).is_file():
        implementation_sha = hashlib.sha256(Path(code_path).read_bytes()).hexdigest()
    else:
        implementation_sha = None
    return {
        "schema": "quant.hourly-data-check.v1",
        "mode": "data_quality_manifest",
        "purpose": purpose,
        "passed": not errors,
        "checked_at_utc": checked.isoformat(),
        "timezone": "UTC",
        "interval": "1h",
        "timestamp_contract": "UTC candle-open Unix milliseconds or timezone-qualified ISO-8601",
        "source_label_asserted_by_operator": source_label.strip(),
        "source_authenticity_verified": False,
        "policy": {"minimum_history_days": min_days,
                   "max_age_hours": max_age_hours if purpose == "live" else None,
                   "require_exact_cross_symbol_alignment": require_exact_alignment},
        "rules_applied": ["required_ohlcv_columns", "finite_values_and_ohlc_bounds",
                          "negative_volume_rejected", "closed_hourly_candles_only",
                          "sorted_unique_contiguous_timestamps", "minimum_history_length",
                          "sha256_recorded", *( ["exact_cross_symbol_alignment"]
                              if require_exact_alignment and len(files) > 1 else [] ),
                          *( ["maximum_latest_candle_age"]
                              if purpose == "live" and max_age_hours is not None else [] )],
        "symbols": reports,
        "errors": errors,
        "implementation_sha256": implementation_sha,
        "note": "A passed report verifies format and internal consistency, not economic correctness or provider authenticity.",
    }
