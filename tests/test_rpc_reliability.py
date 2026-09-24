import json
import threading

import httpx
import pytest

from cu_pilot.rpc import RpcClient, RpcError


def test_transport_retry_then_success_and_cancellation():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout("https://SECRET.invalid/token", request=request)
        return httpx.Response(200, json={"id": 1, "result": 20})

    with RpcClient("http://unused.invalid", transport=httpx.MockTransport(handler)) as rpc:
        assert rpc.get_slot() == 20
        assert rpc.retry_count == 1
    cancel = threading.Event()
    cancel.set()
    with RpcClient(
        "http://unused.invalid", transport=httpx.MockTransport(handler), cancellation=cancel
    ) as rpc:
        with pytest.raises(RpcError, match="cancelled"):
            rpc.get_slot()
        assert rpc.call_count == 0


@pytest.mark.parametrize("code,expected", [(-32005, 2), (-32016, 2), (-32602, 1), (-32002, 1)])
def test_only_retryable_rpc_errors_retry(code, expected):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 1, "error": {"code": code, "message": "SECRET"}})

    with RpcClient(
        "http://unused.invalid/SECRET",
        attempts=2,
        requests_per_second=1000,
        transport=httpx.MockTransport(handler),
    ) as rpc:
        with pytest.raises(RpcError) as error:
            rpc.get_slot()
        assert "SECRET" not in str(error.value)
    assert len(calls) == expected


@pytest.mark.parametrize("slot", [True, 1.0, "1", -1, 2**64])
def test_invalid_slot_reads_rejected(slot):
    with RpcClient(
        "http://unused.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"id": 1, "result": slot})
        ),
    ) as rpc:
        with pytest.raises(RpcError):
            rpc.get_slot()


def test_network_attempt_limit_redacts_transport_error():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("SECRET", request=request)

    with RpcClient(
        "http://unused.invalid/SECRET", attempts=2, transport=httpx.MockTransport(handler)
    ) as rpc:
        with pytest.raises(RpcError) as error:
            rpc.get_slot()
        assert "SECRET" not in str(error.value)
    assert len(calls) == 2
