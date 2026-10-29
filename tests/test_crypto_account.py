import pytest

from quant.crypto.account import BinanceAccountError, BinanceSpotAccountReadOnly


class Response:
    def __init__(self, payload): self.payload = payload
    def data(self): return self.payload


class Rest:
    def get_account(self, **kwargs):
        assert kwargs == {"omit_zero_balances":True,"recv_window":5000}
        return Response({"accountType":"SPOT","canTrade":False,"balances":[
            {"asset":"USDT","free":"12.5","locked":"1.5"},
            {"asset":"BTC","free":"0","locked":"0"}]})


class Client: rest_api = Rest()


def test_official_sdk_account_request_is_read_only_and_summarized():
    client = BinanceSpotAccountReadOnly("test-key", "test-secret", client=Client())
    summary = client.summarize(client.get_account())
    assert summary["balances"] == [{"asset":"USDT","free":12.5,"locked":1.5,"total":14.0}]
    assert summary["order_methods_available"] is False


def test_missing_credentials_bad_host_and_recv_window_are_rejected(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False); monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with pytest.raises(ValueError, match="BINANCE_API_KEY"): BinanceSpotAccountReadOnly()
    with pytest.raises(ValueError, match="official Binance"): BinanceSpotAccountReadOnly("k","s","http://api.binance.com",client=Client())
    client = BinanceSpotAccountReadOnly("k","s",client=Client())
    with pytest.raises(ValueError, match="recvWindow"): client.get_account(recv_window=0)


def test_sdk_errors_are_sanitized():
    class Bad:
        class rest_api:
            @staticmethod
            def get_account(**kwargs): raise RuntimeError("private-key-and-request-url")
    client = BinanceSpotAccountReadOnly("sensitive-key","sensitive-secret",client=Bad())
    with pytest.raises(BinanceAccountError) as exc: client.get_account()
    assert "sensitive" not in str(exc.value) and "request-url" not in str(exc.value)
