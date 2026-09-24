import base64
import json
from pathlib import Path

import httpx
import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message, MessageV0, MessageV1, TransactionConfig
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from cu_pilot.binding import (
    LookupEvidence,
    bind_message,
    decode_wire,
    message_identity,
    normalize_message,
    replace_resources,
    unsigned_wire,
    verify_final_message,
)
from cu_pilot.features import extract_features
from cu_pilot.integration import EstimationContext, estimate_resources
from cu_pilot.rpc import RpcClient
from cu_pilot.shadow import ObservationStore, ShadowRequest, collect_shadow


def builder(version="legacy", *, budget=True, amount=100, payer=None):
    payer = payer or Pubkey.from_bytes(bytes([3]) * 32)
    instructions = [
        transfer(
            TransferParams(
                from_pubkey=payer, to_pubkey=Pubkey.from_bytes(bytes([4]) * 32), lamports=amount
            )
        )
    ]
    if budget:
        instructions += [set_compute_unit_limit(20_000), set_compute_unit_price(2**53 + 1)]
    if version == 1:
        return MessageV1.try_compile(
            payer,
            instructions[:1],
            Hash.default(),
            TransactionConfig(
                priority_fee=2**53 + 1,
                compute_unit_limit=20_000,
                loaded_accounts_data_size_limit=32768,
            ),
        )
    if version == 0:
        return MessageV0.try_compile(payer, instructions, [], Hash.default())
    return Message.new_with_blockhash(instructions, payer, Hash.default())


def context(**kwargs):
    return EstimationContext(
        context="local",
        current_slot=10,
        cluster_identity="local-genesis",
        runtime_identity="local-runtime",
        workload="batch-transfer",
        budget_independent=True,
        **kwargs,
    )


def rpc_response(value=None, *, slot=10):
    if value is None:
        value = {"err": None, "unitsConsumed": 600, "loadedAccountsDataSize": 128}
    return {"id": 1, "result": {"context": {"slot": slot}, "value": value}}


@pytest.mark.parametrize("version", ["legacy", 0, 1])
def test_real_sdk_roundtrip_and_bound_fallback(version):
    raw = unsigned_wire(builder(version))
    sent = []

    def handler(request):
        call = json.loads(request.content)
        sent.append(call)
        prepared = normalize_message(decode_wire(call["params"][0]).message, current_slot=10)
        features = extract_features(prepared)
        assert features.requested_compute_units == 1_400_000
        assert features.requested_loaded_accounts_bytes == 64 * 1024 * 1024
        return httpx.Response(200, json=rpc_response())

    with RpcClient("http://example.invalid", transport=httpx.MockTransport(handler)) as rpc:
        result = estimate_resources(raw, rpc=rpc, context=context())
    assert result.status == "simulation_success"
    assert result.compute_unit_limit == 700
    assert result.loaded_accounts_data_size_limit == 32768
    assert sent[0]["params"][1]["replaceRecentBlockhash"] is False
    final = decode_wire(result.unsigned_transaction_base64)
    assert not any(final.verify_with_results())
    f = extract_features(normalize_message(final.message, current_slot=10))
    assert (
        f.requested_priority_fee_lamports if version == 1 else f.requested_micro_lamports
    ) == 2**53 + 1
    assert result.plan.prediction.simulation_recommended


@pytest.mark.parametrize("version", ["legacy", 0, 1])
def test_exact_identity_detects_same_pattern_different_amount(version):
    first = bind_message(unsigned_wire(builder(version, amount=100)), current_slot=10)
    second = bind_message(unsigned_wire(builder(version, amount=200)), current_slot=10)
    assert first.features.pattern_id == second.features.pattern_id
    assert first.prepared_identity != second.prepared_identity
    with pytest.raises(ValueError, match="message changed"):
        verify_final_message(first.wire_base64, second.wire_base64)


def test_controlled_replacement_and_blockhash_rebind():
    original = builder()
    bound = bind_message(unsigned_wire(original), current_slot=10)
    message = decode_wire(bound.wire_base64).message
    final = replace_resources(message, 1000, 32768)
    refreshed = replace_resources(final, 1000, 32768, blockhash=Hash.new_unique())
    with pytest.raises(ValueError):
        verify_final_message(unsigned_wire(final), unsigned_wire(refreshed))
    assert verify_final_message(
        unsigned_wire(final), unsigned_wire(refreshed), allow_blockhash_refresh=True
    ) == message_identity(refreshed)
    with pytest.raises(ValueError):
        verify_final_message(
            unsigned_wire(final), unsigned_wire(replace_resources(final, 1001, 32768))
        )


def test_missing_placeholders_finalized_before_features():
    message = builder(budget=False)
    bound = bind_message(unsigned_wire(message), current_slot=10)
    assert len(bound.transaction.instructions) == len(message.instructions) + 2
    assert bound.transaction.instructions[0].data_hex == message.instructions[0].data.hex()


@pytest.mark.parametrize("version", ["legacy", 0, 1])
def test_real_kit_cross_language_contract(version):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "kit" / f"{version}.json").read_text()
    )
    message = decode_wire(fixture["wireBase64"]).message
    tables = {}
    if version == 0:
        for key, addresses in fixture["lookup"]["tables"].items():
            # A synthetic ALT account encoding for the SDK-generated v0 references.
            header = (1).to_bytes(4, "little") + (2**64 - 1).to_bytes(8, "little")
            header += bytes(44)
            data = header + b"".join(bytes(Pubkey.from_string(k)) for k in addresses)
            tables[key] = LookupEvidence(
                address=key,
                owner="AddressLookupTab1e1111111111111111111111111",
                data_base64=base64.b64encode(data).decode(),
                slot=10,
            )
    tx = normalize_message(message, current_slot=10, lookups=tables)
    assert message_identity(message) == fixture["messageIdentity"]
    expected = fixture["features"]
    actual = extract_features(tx).model_dump(mode="json")
    for field in ("requested_micro_lamports", "requested_priority_fee_lamports"):
        if expected[field] is not None:
            expected[field] = int(expected[field])
    assert actual == expected


@pytest.mark.parametrize(
    "value,slot",
    [
        ({"err": {"InstructionError": [0, "fail"]}, "unitsConsumed": 30}, 10),
        ({"err": None, "unitsConsumed": 30}, 10),
        ({"err": None, "unitsConsumed": 1_400_000, "loadedAccountsDataSize": 1}, 10),
        ({"err": None, "unitsConsumed": 30, "loadedAccountsDataSize": 1}, 9),
    ],
)
def test_failed_missing_unsafe_or_stale_simulation_unresolved(value, slot):
    with RpcClient(
        "http://example.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=rpc_response(value, slot=slot))
        ),
    ) as rpc:
        result = estimate_resources(unsigned_wire(builder()), rpc=rpc, context=context())
    assert result.status == "unresolved"
    assert result.compute_unit_limit is None
    assert result.unsigned_transaction_base64 is None


def test_invalid_duplicate_budget_not_repaired():
    payer = Pubkey.new_unique()
    message = Message.new_with_blockhash(
        [set_compute_unit_limit(1), set_compute_unit_limit(2)], payer, Hash.default()
    )
    with pytest.raises(ValueError, match="duplicate"):
        bind_message(unsigned_wire(message), current_slot=10)


def test_heap_mutation_and_nonce_refresh_rejected():
    from solders.system_program import AdvanceNonceAccountParams, advance_nonce_account

    payer = Pubkey.new_unique()
    nonce = advance_nonce_account(
        AdvanceNonceAccountParams(nonce_pubkey=Pubkey.new_unique(), authorized_pubkey=payer)
    )
    message = Message.new_with_blockhash([nonce], payer, Hash.new_unique())
    bound = bind_message(unsigned_wire(message), current_slot=10)
    assert bound.durable_nonce
    assert decode_wire(bound.wire_base64).message.instructions[0].data == nonce.data
    with pytest.raises(ValueError, match="nonce"):
        verify_final_message(bound.wire_base64, bound.wire_base64, allow_blockhash_refresh=True)


def test_shadow_freezes_prediction_resumes_and_reconciles(tmp_path):
    payer = Keypair()  # Ephemeral local test key; never persisted or printed.
    raw = unsigned_wire(builder(payer=payer.pubkey()))
    request = ShadowRequest(
        observation_id="request-1", wire_base64=raw, context=context(), evidence_origin="synthetic"
    )
    calls = []
    with ObservationStore(tmp_path / "events.sqlite") as store:

        def handler(request):
            assert store.get("request-1")["plan"] is not None
            calls.append(1)
            return httpx.Response(200, json=rpc_response())

        with RpcClient("http://example.invalid", transport=httpx.MockTransport(handler)) as rpc:
            assert collect_shadow([request], store=store, rpc=rpc)["completed"] == 1
            assert collect_shadow([request], store=store, rpc=rpc)["deduplicated"] == 1
        assert len(calls) == 1
        result = store.get("request-1")["result"]
        signed = VersionedTransaction(
            decode_wire(result["unsigned_transaction_base64"]).message, [payer]
        )
        signed_wire = base64.b64encode(bytes(signed)).decode()
        signature = store.attach_signature("request-1", signed_wire)
        assert store.reconcile(signature, None, commitment="finalized") == "unavailable"
        outcome = {
            "slot": 11,
            "transaction": [signed_wire, "base64"],
            "meta": {"err": None, "computeUnitsConsumed": 650, "costUnits": 999999},
        }
        assert store.reconcile(signature, outcome, commitment="confirmed") == "pending_finality"
        assert store.reconcile(signature, outcome, commitment="finalized") == "finalized"
        assert store.reconcile(signature, outcome, commitment="finalized") == "finalized"
        simulation_rows = list(
            store.training_observations(source="simulation", evidence_origin="synthetic")
        )
        execution_rows = list(
            store.training_observations(source="historical", evidence_origin="synthetic")
        )
        assert simulation_rows[0].label.compute_units == 600
        assert execution_rows[0].label.compute_units == 650
        assert execution_rows[0].label.loaded_accounts_bytes is None


def test_lookup_read_newer_than_builder_slot_advances_context():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "kit" / "0.json").read_text())
    key, addresses = next(iter(fixture["lookup"]["tables"].items()))
    header = (1).to_bytes(4, "little") + (2**64 - 1).to_bytes(8, "little") + bytes(44)
    raw = header + b"".join(bytes(Pubkey.from_string(address)) for address in addresses)
    methods = []

    def handler(request):
        call = json.loads(request.content)
        methods.append(call["method"])
        if call["method"] == "getAccountInfo":
            assert call["params"][0] == key
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "result": {
                        "context": {"slot": 11},
                        "value": {
                            "executable": False,
                            "owner": "AddressLookupTab1e1111111111111111111111111",
                            "data": [base64.b64encode(raw).decode(), "base64"],
                        },
                    },
                },
            )
        assert call["params"][1]["minContextSlot"] == 11
        return httpx.Response(200, json=rpc_response(slot=11))

    with RpcClient("http://example.invalid", transport=httpx.MockTransport(handler)) as rpc:
        result = estimate_resources(fixture["wireBase64"], rpc=rpc, context=context())
    assert result.status == "simulation_success"
    assert result.plan.context.current_slot == 11
    assert result.plan.state_reads == 1
    assert result.rpc_attempts == 2
    assert methods == ["getAccountInfo", "simulateTransaction"]
