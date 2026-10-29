"""Auditable AI strategy selection for Binance Spot Testnet.

The model can select one of two compiled profiles. It has no exchange client,
credentials, or ability to alter risk settings. Shadow mode is the default.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .market import validate_bars
from .observer import HOUR_MS, SYMBOLS, canonical
from .strategy import sma

PROFILES = ("sma_24_72", "cash")
MIN_CONFIRMATIONS = 3
MIN_HOLD_MS = 24 * HOUR_MS


def market_features(histories: dict[str, list[dict]], *, now_ms: int) -> dict:
    """Build a compact, deterministic, credentials-free two-symbol feature set."""
    result = {}
    for symbol in SYMBOLS:
        bars = histories[symbol]
        if len(bars) < 72:
            raise ValueError("Need 72 closed hourly candles for adaptive analysis")
        validate_bars(bars[-72:])
        last = bars[-1]
        ts = int(last["timestamp"])
        if ts + HOUR_MS > now_ms or now_ms - (ts + HOUR_MS) > 2 * HOUR_MS:
            raise ValueError("Market candle is unclosed or stale")
        closes = [float(x["close"]) for x in bars[-72:]]
        volumes = [float(x.get("volume", 0)) for x in bars[-72:]]
        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        if any(not math.isfinite(x) or x <= 0 for x in closes) or any(
                not math.isfinite(v) or v < 0 for v in volumes):
            raise ValueError("Invalid market data")
        fast = sum(closes[-24:]) / 24
        slow = sum(closes) / 72
        mean = sum(returns) / len(returns)
        volatility = math.sqrt(sum((x - mean) ** 2 for x in returns) / len(returns))
        result[symbol] = {
            "candle_open_ms": ts,
            "close": closes[-1],
            "volume": volumes[-1],
            "volume_ratio_24_72": ((sum(volumes[-24:]) / 24) /
                                    (sum(volumes) / 72)) if sum(volumes) else 0.0,
            "sma_24_vs_72_pct": (fast / slow - 1) * 100,
            "realized_volatility_71h_pct": volatility * 100,
            "drawdown_72h_pct": (closes[-1] / max(closes) - 1) * 100,
        }
    hours = {result[s]["candle_open_ms"] for s in SYMBOLS}
    if len(hours) != 1:
        raise ValueError("BTC and ETH candles must be aligned to the same hour")
    return {"as_of_hour_ms": next(iter(hours)),
            "symbols": result}


def validate_recommendation(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("AI response must be an object")
    required = {"market_regime", "profile_id", "confidence", "rationale", "model_version"}
    if set(payload) != required:
        raise ValueError("AI response fields do not match the required schema")
    if payload["profile_id"] not in PROFILES:
        raise ValueError("AI selected a profile outside the approved allowlist")
    if not isinstance(payload["market_regime"], str) or not 1 <= len(payload["market_regime"]) <= 80:
        raise ValueError("Invalid market_regime")
    if not isinstance(payload["rationale"], str) or not 1 <= len(payload["rationale"]) <= 500:
        raise ValueError("Invalid rationale")
    if not isinstance(payload["model_version"], str) or not 1 <= len(payload["model_version"]) <= 100:
        raise ValueError("Invalid model_version")
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("Invalid confidence")
    return {**payload, "confidence": float(confidence)}


class OpenAICompatibleProvider:
    """Provider-neutral chat-completions adapter; sends market features only."""
    def __init__(self, *, base_url=None, api_key=None, model=None, timeout=20):
        self.base_url = (base_url or os.environ.get("QUANT_AI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("QUANT_AI_API_KEY", "")
        self.model = model or os.environ.get("QUANT_AI_MODEL", "")
        self.timeout = timeout
        if not self.base_url or not self.model:
            raise ValueError("Set QUANT_AI_BASE_URL and QUANT_AI_MODEL for adaptive analysis")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("QUANT_AI_BASE_URL must be an HTTPS URL without embedded credentials")

    def recommend(self, features: dict) -> dict:
        url = self.base_url if self.base_url.endswith("/chat/completions") else self.base_url + "/chat/completions"
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": (
                    "Analyze the supplied BTC/ETH hourly market features. Choose exactly one "
                    "approved profile: sma_24_72 (fixed SMA24/72 long/cash rule) or cash "
                    "(no target exposure). Do not propose orders, alter risk limits, or invent "
                    "profiles. Return only a JSON object with exactly these fields: "
                    "market_regime (string), profile_id (sma_24_72 or cash), confidence "
                    "(number from 0 to 1), rationale (string), and model_version (string). "
                    "Do not include any additional fields.")},
                            {"role": "user", "content": canonical(features)}],
                # DeepSeek Chat Completions supports JSON mode rather than JSON Schema.
                # The exact response shape and allowlisted profile are enforced below
                # by validate_recommendation, so malformed or unsafe output fails closed.
                "response_format": {"type": "json_object"}}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = json.loads(response.read())
        content = raw["choices"][0]["message"]["content"]
        return validate_recommendation(json.loads(content))


class AdaptiveController:
    """Journal market inputs and AI choices; apply 3-hour confirmation/24h hold."""
    def __init__(self, db_path, *, mode="shadow", provider=None, now_ms=None, notifier=None):
        if mode not in ("shadow", "testnet_auto"):
            raise ValueError("mode must be shadow or testnet_auto")
        self.mode, self.provider = mode, provider
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.notifier = notifier
        path = Path(db_path); path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS ai_hours(hour_ms INTEGER PRIMARY KEY,features TEXT NOT NULL,input_hash TEXT NOT NULL,model_version TEXT,recommendation TEXT,state TEXT NOT NULL,error_type TEXT,recorded_ms INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_transitions(id INTEGER PRIMARY KEY AUTOINCREMENT,hour_ms INTEGER NOT NULL,from_profile TEXT NOT NULL,to_profile TEXT NOT NULL,reason TEXT NOT NULL,mode TEXT NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO ai_state VALUES('active_profile','sma_24_72')")
        self.db.execute("INSERT OR IGNORE INTO ai_state VALUES('created_ms',?)", (str(self.now_ms()),))
        self.db.execute("INSERT INTO ai_state(key,value) VALUES('mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (mode,))
        self.db.execute("INSERT OR IGNORE INTO ai_state VALUES('candidate_profile','')")
        self.db.execute("INSERT OR IGNORE INTO ai_state VALUES('candidate_count','0')")
        self.db.execute("INSERT OR IGNORE INTO ai_state VALUES('last_switch_ms',?)", (str(self.now_ms()),))
        # A process restart breaks a consecutive hourly confirmation sequence.
        self.db.execute("UPDATE ai_state SET value='' WHERE key='candidate_profile'")
        self.db.execute("UPDATE ai_state SET value='0' WHERE key='candidate_count'")

    def close(self):
        self.db.close()

    def _alert(self, event, **data):
        if self.notifier:
            try:
                self.notifier({"event": event, "timestamp_ms": self.now_ms(), **data})
            except Exception:
                pass

    def _state(self, key):
        return self.db.execute("SELECT value FROM ai_state WHERE key=?", (key,)).fetchone()[0]

    def evaluate(self, histories: dict[str, list[dict]]) -> dict:
        now = self.now_ms()
        features = None
        try:
            features = market_features(histories, now_ms=now)
            hour = int(features["as_of_hour_ms"])
            if now - (hour + HOUR_MS) > 2 * HOUR_MS:
                raise ValueError("stale_market_data")
            if self.db.execute("SELECT 1 FROM ai_hours WHERE hour_ms=?", (hour,)).fetchone():
                raise ValueError("duplicate_hourly_evaluation")
            encoded = canonical(features)
            input_hash = hashlib.sha256(encoded.encode()).hexdigest()
            if self.provider is None:
                raise RuntimeError("ai_provider_not_configured")
            decision = validate_recommendation(self.provider.recommend(features))
            self.db.execute("INSERT INTO ai_hours VALUES(?,?,?,?,?,?,NULL,?)",
                            (hour, encoded, input_hash, decision["model_version"],
                             canonical(decision), "accepted", now))
            active = self._state("active_profile")
            candidate = self._state("candidate_profile")
            count = int(self._state("candidate_count"))
            last_switch = int(self._state("last_switch_ms"))
            proposed = decision["profile_id"]
            if proposed == active:
                candidate, count = "", 0
            elif proposed == candidate:
                count += 1
            else:
                candidate, count = proposed, 1
            transitioned = False
            if proposed != active and count >= MIN_CONFIRMATIONS and now - last_switch >= MIN_HOLD_MS:
                self.db.execute("INSERT INTO ai_transitions(hour_ms,from_profile,to_profile,reason,mode) VALUES(?,?,?,?,?)",
                                (hour, active, proposed, decision["rationale"], self.mode))
                active, candidate, count = proposed, "", 0
                self.db.execute("UPDATE ai_state SET value=? WHERE key='active_profile'", (active,))
                self.db.execute("UPDATE ai_state SET value=? WHERE key='last_switch_ms'", (str(now),))
                transitioned = True
            self.db.execute("UPDATE ai_state SET value=? WHERE key='candidate_profile'", (candidate,))
            self.db.execute("UPDATE ai_state SET value=? WHERE key='candidate_count'", (str(count),))
            self._alert("strategy_transition", profile=active, mode=self.mode) if transitioned else None
            return {"hour_ms": hour, "recommendation": decision, "active_profile": active,
                    "candidate_profile": candidate or None, "candidate_confirmations": count,
                    "transitioned": transitioned, "mode": self.mode,
                    "orders_allowed": self.mode == "testnet_auto"}
        except Exception as exc:
            hour = (int(features["as_of_hour_ms"]) if features else
                    int(self.now_ms() // HOUR_MS * HOUR_MS - HOUR_MS))
            # Failed or invalid hourly evidence breaks the consecutive-confirmation run.
            self.db.execute("UPDATE ai_state SET value='' WHERE key='candidate_profile'")
            self.db.execute("UPDATE ai_state SET value='0' WHERE key='candidate_count'")
            # Keep invalid/time-out/API failures in the durable journal too.
            try:
                encoded = canonical(features) if features else "{}"
                input_hash = hashlib.sha256(encoded.encode()).hexdigest() if features else ""
                self.db.execute("INSERT OR IGNORE INTO ai_hours VALUES(?,?,?,NULL,NULL,?,?,?)",
                                (hour, encoded, input_hash, "error", type(exc).__name__, self.now_ms()))
            except sqlite3.Error:
                pass
            self._alert("ai_or_market_failure", error_type=type(exc).__name__)
            return {"mode": self.mode, "state": "no_adaptation", "error_type": type(exc).__name__,
                    "active_profile": self._state("active_profile"), "orders_allowed": False}

    def status(self):
        counts = {row[0]: row[1] for row in self.db.execute("SELECT state,COUNT(*) FROM ai_hours GROUP BY state")}
        span = self.db.execute("SELECT MIN(hour_ms),MAX(hour_ms),COUNT(*) FROM ai_hours WHERE state='accepted'").fetchone()
        span_hours = ((span[1]-span[0])//HOUR_MS+1) if span[0] is not None else 0
        return {"mode": self.mode, "active_profile": self._state("active_profile"),
                "candidate_profile": self._state("candidate_profile") or None,
                "candidate_confirmations": int(self._state("candidate_count")),
                "evaluations": counts,
                "accepted_hours": counts.get("accepted", 0),
                "observation_span_hours": span_hours,
                "first_accepted_hour_ms": span[0], "last_accepted_hour_ms": span[1],
                "transitions": self.db.execute("SELECT COUNT(*) FROM ai_transitions").fetchone()[0],
                "orders_allowed": self.mode == "testnet_auto"}


def validate_acceptance_gate(path):
    """Require a manually reviewed, explicit evidence record before Testnet auto mode."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required_true = ("observation_30_days_passed", "backtest_costs_and_stages_passed",
                     "shadow_decisions_replay_reviewed",
                     "testnet_lifecycle_acceptance_passed", "reconciliation_passed",
                     "human_approval")
    if not isinstance(data, dict) or any(data.get(k) is not True for k in required_true):
        raise ValueError("Acceptance gate failed: 30-day observation, cost-aware backtest, Testnet lifecycle, reconciliation, and human approval are all required")
    if data.get("approved_profiles") != list(PROFILES):
        raise ValueError("Acceptance file must approve exactly the compiled strategy profiles")
    if not isinstance(data.get("approval_id"), str) or not data["approval_id"].strip():
        raise ValueError("Acceptance file requires a traceable approval_id")
    return data


def post_webhook(url, payload, *, timeout=5):
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Alert webhook must be an HTTPS URL without embedded credentials")
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout):
        pass

