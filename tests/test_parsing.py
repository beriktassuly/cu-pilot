"""Synthetic fixtures exercise RPC formats, never a live cluster."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from cu_pilot.parsing import decode_base58, normalize_observation, parse_transaction

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str = "legacy_transfer") -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())  # type: ignore[no-any-return]


def test_legacy_header_roles_and_instruction_bytes() -> None:
    tx = parse_transaction(load_fixture())
    assert tx.version == "legacy"
    assert tx.signature_count == 1
    assert [(account.signer, account.writable) for account in tx.accounts] == [
        (True, True),
        (False, True),
        (False, False),
        (False, False),
    ]
    assert tx.instructions[1].data_hex == "02000000e803000000000000"
    assert tx.instructions[1].accounts == (0, 1)


def test_rpc_envelope_matches_result() -> None:
    raw = load_fixture()
    assert parse_transaction({"jsonrpc": "2.0", "id": 1, "result": raw}) == parse_transaction(raw)


def test_unsigned_compiled_message_wrapper() -> None:
    raw = load_fixture()
    wrapper = {"version": "legacy", "message": raw["transaction"]["message"]}
    assert parse_transaction(wrapper).signature_count == 1


def test_v0_resolves_writable_then_readonly_lookup_accounts() -> None:
    tx = parse_transaction(load_fixture("v0_transfer"))
    assert tx.version == 0
    assert len(tx.accounts) == 5
    assert tx.lookup_table_count == tx.lookup_writable_count == tx.lookup_readonly_count == 1
    assert tx.accounts[3].source == tx.accounts[4].source == "lookup"
    assert tx.accounts[3].writable and not tx.accounts[4].writable
    assert tx.instructions[1].accounts == (0, 3)


def test_v1_extracts_message_config() -> None:
    tx = parse_transaction(load_fixture("v1_transfer"))
    assert tx.version == 1
    assert tx.transaction_config == {
        "computeUnitLimit": 30_000,
        "heapSize": None,
        "loadedAccountsDataSizeLimit": 200_000,
        "priorityFee": 500,
    }


def test_label_is_compute_consumed_never_cost_units() -> None:
    raw = load_fixture()
    observation = normalize_observation(raw, context="devnet:fixture", source="synthetic")
    assert observation.label.compute_units == 450
    assert observation.label.loaded_accounts_bytes is None
    assert observation.record_id == raw["transaction"]["signatures"][0]
    assert observation.label.success
    del raw["meta"]["computeUnitsConsumed"]
    raw["meta"]["unitsConsumed"] = 999
    assert normalize_observation(raw, context="devnet:fixture").label.compute_units is None


def test_metadata_changes_never_leak_into_features() -> None:
    first = load_fixture()
    second = deepcopy(first)
    second["slot"] += 1
    second["blockTime"] = 123456
    second["meta"].update(
        computeUnitsConsumed=30_000,
        loadedAccountsDataSize=1000,
        fee=100_000,
        costUnits=999_999,
        preBalances=[0] * 4,
        postBalances=[1] * 4,
        innerInstructions=[{"index": 1, "instructions": [{"parsed": "ignored"}]}],
        err={"InstructionError": [1, "Custom"]},
    )
    original = normalize_observation(first, context="devnet")
    changed = normalize_observation(second, context="devnet")
    assert original.features == changed.features
    assert not changed.label.success
    assert changed.label.compute_units == 30_000
    assert changed.label.loaded_accounts_bytes == 1000


def test_unsigned_record_hash_is_stable_across_dictionary_order() -> None:
    raw = load_fixture()
    del raw["transaction"]["signatures"]
    first = normalize_observation(raw, context="test")
    second = normalize_observation(dict(reversed(list(raw.items()))), context="test")
    assert first.record_id.startswith("sha256:")
    assert first.record_id == second.record_id


@pytest.mark.parametrize("version", [2, "0", True, None, -1])
def test_unsupported_or_mistyped_versions_fail_closed(version: Any) -> None:
    raw = load_fixture()
    raw["version"] = version
    with pytest.raises(ValueError, match="unsupported transaction version"):
        parse_transaction(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("numRequiredSignatures", 0),
        ("numRequiredSignatures", 5),
        ("numRequiredSignatures", True),
        ("numReadonlySignedAccounts", 1),
        ("numReadonlyUnsignedAccounts", 4),
        ("numReadonlyUnsignedAccounts", -1),
    ],
)
def test_invalid_headers_fail_closed(field: str, value: Any) -> None:
    raw = load_fixture()
    raw["transaction"]["message"]["header"][field] = value
    with pytest.raises(ValueError):
        parse_transaction(raw)


@pytest.mark.parametrize("data", ["0", "O", "l", "a a", [], None])
def test_invalid_instruction_base58_fails_closed(data: Any) -> None:
    raw = load_fixture()
    raw["transaction"]["message"]["instructions"][0]["data"] = data
    with pytest.raises(ValueError, match="base58"):
        parse_transaction(raw)


@pytest.mark.parametrize("index", [-1, 4, 256, True, "0"])
def test_invalid_instruction_indices_fail_closed(index: Any) -> None:
    raw = load_fixture()
    raw["transaction"]["message"]["instructions"][1]["accounts"] = [index]
    with pytest.raises(ValueError):
        parse_transaction(raw)


def test_invalid_program_index_fails_closed() -> None:
    raw = load_fixture()
    raw["transaction"]["message"]["instructions"][0]["programIdIndex"] = 4
    with pytest.raises(ValueError, match="out of bounds"):
        parse_transaction(raw)


def test_json_parsed_is_rejected_without_lossy_reconstruction() -> None:
    raw = load_fixture()
    raw["transaction"]["message"]["accountKeys"][0] = {"pubkey": "ignored", "signer": True}
    with pytest.raises(ValueError, match="jsonParsed"):
        parse_transaction(raw)
    raw = load_fixture()
    raw["transaction"]["message"]["instructions"][1] = {"parsed": {"type": "transfer"}}
    with pytest.raises(ValueError, match="jsonParsed"):
        parse_transaction(raw)


@pytest.mark.parametrize("key", ["1", "1" * 33, "0" * 32, "a" * 100])
def test_invalid_public_keys_fail_closed(key: str) -> None:
    raw = load_fixture()
    raw["transaction"]["message"]["accountKeys"][0] = key
    with pytest.raises(ValueError, match="public key|base58"):
        parse_transaction(raw)


def test_duplicate_public_keys_fail_closed() -> None:
    raw = load_fixture()
    keys = raw["transaction"]["message"]["accountKeys"]
    keys[1] = keys[0]
    with pytest.raises(ValueError, match="duplicate account keys"):
        parse_transaction(raw)


@pytest.mark.parametrize("signatures", [[], ["1"], ["0" * 64], ["1" * 64, "1" * 64]])
def test_invalid_signatures_fail_closed(signatures: list[str]) -> None:
    raw = load_fixture()
    raw["transaction"]["signatures"] = signatures
    with pytest.raises(ValueError):
        parse_transaction(raw)


def test_lookup_accounts_cannot_be_inferred_without_resolution() -> None:
    raw = load_fixture("v0_transfer")
    del raw["meta"]["loadedAddresses"]
    with pytest.raises(ValueError, match="unresolved"):
        parse_transaction(raw)


@pytest.mark.parametrize("field", ["readonly", "writable"])
def test_lookup_account_counts_must_match_exactly(field: str) -> None:
    raw = load_fixture("v0_transfer")
    raw["meta"]["loadedAddresses"][field] = []
    with pytest.raises(ValueError, match="counts do not match"):
        parse_transaction(raw)


def test_unexpected_loaded_addresses_fail_closed() -> None:
    raw = load_fixture()
    raw["meta"]["loadedAddresses"] = {
        "writable": [raw["transaction"]["message"]["accountKeys"][1]],
        "readonly": [],
    }
    with pytest.raises(ValueError, match="counts do not match"):
        parse_transaction(raw)


@pytest.mark.parametrize("indices", [[7, 7], [-1], [256], [False]])
def test_bad_lookup_indices_fail_closed(indices: list[Any]) -> None:
    raw = load_fixture("v0_transfer")
    raw["transaction"]["message"]["addressTableLookups"][0]["writableIndexes"] = indices
    with pytest.raises(ValueError):
        parse_transaction(raw)


@pytest.mark.parametrize("version", ["legacy", 1])
def test_lookups_are_v0_only(version: str | int) -> None:
    raw = load_fixture("v0_transfer")
    raw["version"] = version
    if version == 1:
        raw["transaction"]["message"]["transactionConfig"] = {}
    with pytest.raises(ValueError, match="only v0"):
        parse_transaction(raw)


def test_missing_version_for_versioned_messages_is_rejected() -> None:
    for name in ("v0_transfer", "v1_transfer"):
        raw = load_fixture(name)
        del raw["version"]
        with pytest.raises(ValueError, match="version is required"):
            parse_transaction(raw)


def test_v1_without_config_is_rejected_as_incompatible_projection() -> None:
    raw = load_fixture("v1_transfer")
    del raw["transaction"]["message"]["transactionConfig"]
    with pytest.raises(ValueError, match="requires transactionConfig"):
        parse_transaction(raw)


def test_config_cannot_be_attached_to_legacy() -> None:
    raw = load_fixture()
    raw["transaction"]["message"]["transactionConfig"] = {}
    with pytest.raises(ValueError, match="only supported for v1"):
        parse_transaction(raw)


@pytest.mark.parametrize(
    "config",
    [
        None,
        {"computeUnitLimit": -1},
        {"computeUnitLimit": True},
        {"priorityFeeLamports": 1},
        {"computeUnitLimit": 2**32},
        {"priorityFee": 2**64},
    ],
)
def test_invalid_v1_config_fails_closed(config: Any) -> None:
    raw = load_fixture("v1_transfer")
    raw["transaction"]["message"]["transactionConfig"] = config
    with pytest.raises(ValueError):
        parse_transaction(raw)


@pytest.mark.parametrize("raw", [{"result": None}, {"error": {"code": -32015}}, {}])
def test_missing_rpc_transaction_fails_closed(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_transaction(raw)


def test_metadata_must_identify_execution_status() -> None:
    raw = load_fixture()
    del raw["meta"]["err"]
    with pytest.raises(ValueError, match="meta.err"):
        normalize_observation(raw, context="test")


@pytest.mark.parametrize("units", [-1, True, 1.5, "450"])
def test_invalid_labels_are_rejected(units: Any) -> None:
    raw = load_fixture()
    raw["meta"]["computeUnitsConsumed"] = units
    with pytest.raises(ValueError, match="computeUnitsConsumed"):
        normalize_observation(raw, context="test")


def test_base58_leading_zeroes_and_empty_data() -> None:
    assert decode_base58("") == b""
    assert decode_base58("111") == b"\x00\x00\x00"
    assert decode_base58("12") == b"\x00\x01"
