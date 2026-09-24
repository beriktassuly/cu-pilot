import base64
import json
from pathlib import Path

import httpx
import pytest
from solders.pubkey import Pubkey
from test_binding import context, rpc_response

from cu_pilot.integration import estimate_resources
from cu_pilot.rpc import RpcClient


def lookup_fixture():
    fixture = json.loads((Path(__file__).parent / "fixtures/kit/0.json").read_text())
    key, addresses = next(iter(fixture["lookup"]["tables"].items()))
    header = (1).to_bytes(4, "little") + (2**64 - 1).to_bytes(8, "little") + bytes(44)
    data = header + b"".join(bytes(Pubkey.from_string(address)) for address in addresses)
    response = {
        "id": 1,
        "result": {
            "context": {"slot": 10},
            "value": {
                "executable": False,
                "owner": "AddressLookupTab1e1111111111111111111111111",
                "data": [base64.b64encode(data).decode(), "base64"],
            },
        },
    }
    return fixture["wireBase64"], key, response


@pytest.mark.parametrize("retry_simulation", [False, True])
def test_estimate_counts_preparation_and_execution_retries_without_prior_calls(retry_simulation):
    wire, key, account = lookup_fixture()
    counts = {"getSlot": 0, "getAccountInfo": 0, "simulateTransaction": 0}

    def handler(request):
        call = json.loads(request.content)
        method = call["method"]
        counts[method] += 1
        if method == "getSlot":
            if counts[method] == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"id": 1, "result": 10})
        if method == "getAccountInfo":
            assert call["params"][0] == key
            if counts[method] == 1:
                return httpx.Response(429)
            return httpx.Response(200, json=account)
        if retry_simulation and counts[method] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=rpc_response())

    with RpcClient(
        "http://example.invalid/SECRET",
        transport=httpx.MockTransport(handler),
        requests_per_second=10_000,
    ) as rpc:
        assert rpc.get_slot() == 10  # The same client may already have earlier retries.
        result = estimate_resources(wire, rpc=rpc, context=context())
        assert result.status == "simulation_success"
        assert result.rpc_attempts == 3 + int(retry_simulation)
        assert result.retries == 1 + int(retry_simulation)
        assert result.resource_simulation_calls == 1
        assert result.plan.state_reads == 1
        assert rpc.retry_count == result.retries + 1
        assert rpc.call_count == result.rpc_attempts + 2


@pytest.mark.parametrize("failure", ["rate_limit", "timeout", "invalid_evidence"])
def test_preparation_failure_preserves_spent_attempts_and_retries(failure):
    wire, _, _ = lookup_fixture()
    methods = []

    def handler(request):
        method = json.loads(request.content)["method"]
        methods.append(method)
        if method == "getSlot":
            return httpx.Response(200, json={"id": 1, "result": 10})
        assert method == "getAccountInfo"
        if failure == "rate_limit":
            return httpx.Response(429)
        if failure == "timeout":
            raise httpx.ReadTimeout("SECRET", request=request)
        return httpx.Response(200, json={"id": 1, "result": {"context": {"slot": 10}}})

    with RpcClient(
        "http://example.invalid/SECRET",
        transport=httpx.MockTransport(handler),
        attempts=2,
        requests_per_second=10_000,
    ) as rpc:
        assert rpc.get_slot() == 10
        result = estimate_resources(wire, rpc=rpc, context=context(), shadow=True)
        expected_attempts = 1 if failure == "invalid_evidence" else 2
        assert result.status == "unresolved"
        assert result.rpc_attempts == expected_attempts
        assert result.retries == expected_attempts - 1
        assert result.shadow is True
        assert result.plan is None
        assert result.unsigned_transaction_base64 is None
        assert result.resource_simulation_calls == 0
        assert rpc.call_count == result.rpc_attempts + 1
        assert "simulateTransaction" not in methods
        assert "SECRET" not in result.model_dump_json()
