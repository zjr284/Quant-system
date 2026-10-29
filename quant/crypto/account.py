from __future__ import annotations

import os
import time


class BinanceAccountError(ValueError):
    """Sanitized official SDK account API error."""


class BinanceSpotAccountReadOnly:
    """Read-only Spot account query through Binance's official Python SDK."""

    def __init__(self, api_key=None, api_secret=None, base_url=None, timeout=10, *, client=None):
        self.api_key = api_key if api_key is not None else os.environ.get("BINANCE_API_KEY", "")
        self.api_secret = api_secret if api_secret is not None else os.environ.get("BINANCE_API_SECRET", "")
        if not self.api_key or not self.api_secret:
            raise ValueError("Set BINANCE_API_KEY and BINANCE_API_SECRET in the local environment")
        self.base_url = (base_url or os.environ.get("BINANCE_BASE_URL") or "https://api.binance.com").rstrip("/")
        hosts = {"https://api.binance.com", "https://testnet.binance.vision"}
        if self.base_url not in hosts:
            raise ValueError("BINANCE_BASE_URL must be the official Binance Spot production or testnet host")
        self.timeout = timeout
        self.client = client or self._make_client()

    def _make_client(self):
        try:
            from binance_common.configuration import ConfigurationRestAPI
            from binance_common.constants import SPOT_REST_API_PROD_URL, SPOT_REST_API_TESTNET_URL
            from binance_sdk_spot.spot import Spot
        except ImportError as exc:
            raise RuntimeError("Install the official Binance SDK with: pip install -e '.[crypto]'") from exc
        base_path = SPOT_REST_API_TESTNET_URL if "testnet" in self.base_url else SPOT_REST_API_PROD_URL
        config = ConfigurationRestAPI(api_key=self.api_key, api_secret=self.api_secret,
                                       base_path=base_path, timeout=self.timeout * 1000)
        return Spot(config_rest_api=config)

    def get_account(self, *, omit_zero_balances=True, recv_window=5000):
        if not 1 <= recv_window <= 60000:
            raise ValueError("recvWindow must be between 1 and 60000 ms")
        try:
            response = self.client.rest_api.get_account(omit_zero_balances=omit_zero_balances,
                                                         recv_window=recv_window)
            payload = response.data() if callable(getattr(response, "data", None)) else response
            if hasattr(payload, "to_dict"):
                payload = payload.to_dict()
            if not isinstance(payload, dict):
                raise TypeError("unexpected response")
            balances = payload.get("balances")
            if balances is None and isinstance(payload.get("result"), dict):
                payload = payload["result"]
                balances = payload.get("balances")
            if not isinstance(balances, list):
                raise TypeError("unexpected account payload")
            normalized = []
            for row in balances:
                item = row.to_dict() if hasattr(row, "to_dict") else dict(row)
                if not omit_zero_balances or float(item["free"]) or float(item["locked"]):
                    normalized.append(item)
            return {**payload, "balances": normalized}
        except (TypeError, KeyError, ValueError) as exc:
            raise BinanceAccountError("Binance returned an unexpected account response") from None
        except Exception as exc:
            # SDK exceptions can contain request data. Do not leak it to logs or CLI output.
            code = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            suffix = f" (HTTP {code})" if isinstance(code, int) else f" ({type(exc).__name__})"
            raise BinanceAccountError("Binance account request failed" + suffix) from None

    @staticmethod
    def summarize(account):
        balances = []
        for row in account["balances"]:
            free, locked = float(row["free"]), float(row["locked"])
            if free or locked:
                balances.append({"asset": row["asset"], "free": free, "locked": locked,
                                 "total": free + locked})
        return {"connected": True, "account_type": account.get("accountType"),
                "can_trade": account.get("canTrade"), "balances": balances,
                "balance_count": len(balances), "fetched_at_ms": int(time.time() * 1000),
                "secrets_persisted": False, "order_methods_available": False}
