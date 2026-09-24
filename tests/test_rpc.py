import base64
import json

import httpx
import pytest

from cu_pilot.rpc import RpcClient, RpcError


def wire_bytes(version="legacy") -> str:
    wire = (
        b"\x81" + bytes(100)
        if version == 1
        else (b"\x01" + bytes(64) + (b"\x80\x01" if version == 0 else b"\x01") + bytes(80))
    )
    return base64.b64encode(wire).decode()


def test_get_transaction_version_one_and_redacted_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["method"] == "getTransaction"
        assert payload["params"][1]["maxSupportedTransactionVersion"] == 1
        assert payload["params"][1]["encoding"] == "json"
        return httpx.Response(200, json={"id": 1, "error": {"message": "SECRET"}})

    with RpcClient("https://example.invalid/SECRET", transport=httpx.MockTransport(handler)) as rpc:
        with pytest.raises(RpcError) as exc:
            rpc.get_transaction("public-signature")
        assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize("version", ["legacy", 0, 1])
def test_simulation_success_and_wire_preservation(version) -> None:
    wire = wire_bytes(version)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["method"] == "simulateTransaction"
        assert payload["params"][0] == wire
        assert payload["params"][1]["sigVerify"] is False
        assert payload["params"][1]["replaceRecentBlockhash"] is True
        return httpx.Response(
            200,
            json={
                "id": 1,
                "result": {
                    "context": {"slot": 42},
                    "value": {"err": None, "unitsConsumed": 1000, "loadedAccountsDataSize": 2000},
                },
            },
        )

    with RpcClient("https://example.invalid", transport=httpx.MockTransport(handler)) as rpc:
        estimate = rpc.simulate(wire, version=version)
    assert estimate.compute_unit_limit == 1100
    assert estimate.loaded_accounts_data_size_limit == 32768
    assert estimate.slot == 42


@pytest.mark.parametrize(
    "value,version",
    [
        ({"err": "failure", "unitsConsumed": 99}, "legacy"),
        ({"unitsConsumed": 99}, "legacy"),
        ({"err": None}, "legacy"),
        ({"err": None, "unitsConsumed": -1}, "legacy"),
        ({"err": None, "unitsConsumed": True}, "legacy"),
        ({"err": None, "unitsConsumed": 99}, 1),
        ({"err": None, "unitsConsumed": 1_400_000}, "legacy"),
    ],
)
def test_simulation_never_uses_failed_or_incomplete_results(value, version) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, json={"id": 1, "result": {"context": {"slot": 1}, "value": value}}
        )
    )
    with RpcClient("https://example.invalid", transport=transport) as rpc:
        with pytest.raises(RpcError):
            rpc.simulate(wire_bytes(version), version=version)


def test_v1_detected_without_flag_and_mismatched_flag_rejected() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "id": 1,
                "result": {"context": {"slot": 1}, "value": {"err": None, "unitsConsumed": 100}},
            },
        )
    )
    with RpcClient("https://example.invalid", transport=transport) as rpc:
        with pytest.raises(RpcError, match="loadedAccountsDataSize"):
            rpc.simulate(wire_bytes(1))
        with pytest.raises(ValueError, match="Declared version"):
            rpc.simulate(wire_bytes(1), version="legacy")


def test_retry_is_bounded() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429)

    with RpcClient(
        "https://example.invalid/key", transport=httpx.MockTransport(handler), attempts=2
    ) as rpc:
        with pytest.raises(RpcError, match="transport"):
            rpc.get_slot()
    assert len(calls) == 2
