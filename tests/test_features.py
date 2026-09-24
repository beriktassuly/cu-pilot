import json
from pathlib import Path

import pytest

from cu_pilot.features import COMPUTE_BUDGET_PROGRAM, SYSTEM_PROGRAM, extract_features
from cu_pilot.parsing import parse_transaction
from cu_pilot.schemas import Account, Instruction, TransactionInput


def transaction(name: str = "legacy_transfer") -> TransactionInput:
    return parse_transaction(
        json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())
    )


def budget_instruction(tag: int, value: int, width: int = 4) -> Instruction:
    return Instruction(
        program_id=COMPUTE_BUDGET_PROGRAM,
        accounts=(),
        data_hex=(bytes([tag]) + value.to_bytes(width, "little")).hex(),
    )


def test_features_expose_only_pre_execution_fields() -> None:
    features = extract_features(transaction())
    assert features.account_count == 4
    assert features.signer_count == 1
    assert features.writable_count == 2
    assert features.instruction_count == 2
    assert features.instruction_data_lengths == (5, 12)
    assert features.requested_compute_units == 30_000
    assert features.risk_flags == ()
    assert features.serialized_size is None
    assert features.program_ids == (COMPUTE_BUDGET_PROGRAM, SYSTEM_PROGRAM)


def test_pattern_hash_is_deterministic_and_versioned() -> None:
    first = extract_features(transaction())
    second = extract_features(transaction())
    assert first.pattern_id == second.pattern_id
    assert first.pattern_id.startswith("shape-v1:")
    assert len(first.pattern_id.split(":")[1]) == 64


def test_payer_and_recipient_identity_are_excluded_from_pattern() -> None:
    first = transaction()
    second = first.model_copy(
        update={
            "accounts": (
                first.accounts[0].model_copy(update={"pubkey": first.accounts[1].pubkey}),
                first.accounts[1].model_copy(update={"pubkey": first.accounts[0].pubkey}),
                *first.accounts[2:],
            )
        }
    )
    assert extract_features(first).pattern_id == extract_features(second).pattern_id


def test_compute_limit_and_price_values_do_not_fragment_patterns() -> None:
    first = transaction()
    first = first.model_copy(
        update={
            "instructions": (
                first.instructions[0],
                budget_instruction(3, 100, 8),
                first.instructions[1],
            )
        }
    )
    second = first.model_copy(
        update={
            "instructions": (
                budget_instruction(2, 900_000),
                budget_instruction(3, 9999, 8),
                first.instructions[2],
            )
        }
    )
    assert extract_features(first).pattern_id == extract_features(second).pattern_id
    assert extract_features(second).requested_compute_units == 900_000
    assert extract_features(second).requested_micro_lamports == 9999


def test_system_transfer_amount_does_not_fragment_pattern() -> None:
    first = transaction()
    second = first.model_copy(
        update={
            "instructions": (
                first.instructions[0],
                first.instructions[1].model_copy(update={"data_hex": "02000000ffffffffffffffff"}),
            )
        }
    )
    assert extract_features(first).pattern_id == extract_features(second).pattern_id


def test_heap_changes_pattern_because_heap_cost_changes() -> None:
    first = transaction()
    first = first.model_copy(
        update={"instructions": (*first.instructions, budget_instruction(1, 32768))}
    )
    second = first.model_copy(
        update={"instructions": (*first.instructions[:-1], budget_instruction(1, 65536))}
    )
    assert extract_features(first).pattern_id != extract_features(second).pattern_id
    assert extract_features(second).requested_heap_bytes == 65536


def test_instruction_order_changes_pattern() -> None:
    first = transaction()
    second = first.model_copy(update={"instructions": tuple(reversed(first.instructions))})
    assert extract_features(first).pattern_id != extract_features(second).pattern_id


def test_data_length_and_discriminator_changes_change_pattern() -> None:
    first = transaction()
    for new_data in ("03000000e803000000000000", "02000000e80300000000000000"):
        second = first.model_copy(
            update={
                "instructions": (
                    first.instructions[0],
                    first.instructions[1].model_copy(update={"data_hex": new_data}),
                )
            }
        )
        assert extract_features(first).pattern_id != extract_features(second).pattern_id


def test_account_alias_topology_is_preserved() -> None:
    first = transaction()
    second = first.model_copy(
        update={
            "instructions": (
                first.instructions[0],
                first.instructions[1].model_copy(update={"accounts": (0, 0)}),
            )
        }
    )
    assert extract_features(first).pattern_id != extract_features(second).pattern_id


def test_cross_instruction_account_alias_topology_is_preserved() -> None:
    first = transaction()
    first = first.model_copy(
        update={"instructions": (first.instructions[1], first.instructions[1])}
    )
    second = first.model_copy(
        update={
            "instructions": (
                first.instructions[0],
                first.instructions[1].model_copy(update={"accounts": (1, 0)}),
            )
        }
    )
    assert extract_features(first).pattern_id != extract_features(second).pattern_id


def test_peer_account_reordering_is_canonicalized() -> None:
    first = transaction()
    second = first.model_copy(
        update={"accounts": (*first.accounts[:2], first.accounts[3], first.accounts[2])}
    )
    assert extract_features(first).pattern_id == extract_features(second).pattern_id


def test_account_roles_change_pattern() -> None:
    first = transaction()
    second = first.model_copy(
        update={
            "accounts": (
                first.accounts[0],
                first.accounts[1].model_copy(update={"writable": False}),
                *first.accounts[2:],
            )
        }
    )
    assert extract_features(first).pattern_id != extract_features(second).pattern_id


def test_transaction_version_changes_pattern() -> None:
    first = transaction()
    second = first.model_copy(update={"version": 0})
    assert extract_features(first).pattern_id != extract_features(second).pattern_id


def test_duplicate_budget_instruction_is_risky() -> None:
    tx = transaction()
    tx = tx.model_copy(update={"instructions": (*tx.instructions, budget_instruction(2, 10_000))})
    assert "duplicate_compute_budget_instruction" in extract_features(tx).risk_flags


@pytest.mark.parametrize("data", ["", "00", "05", "0201", "0300000000"])
def test_malformed_budget_instruction_is_risky(data: str) -> None:
    tx = transaction()
    tx = tx.model_copy(
        update={
            "instructions": (
                tx.instructions[0].model_copy(update={"data_hex": data}),
                tx.instructions[1],
            )
        }
    )
    assert "invalid_compute_budget_instruction" in extract_features(tx).risk_flags


@pytest.mark.parametrize("heap", [0, 1024, 32769, 263168])
def test_invalid_heap_request_is_risky(heap: int) -> None:
    tx = transaction()
    tx = tx.model_copy(update={"instructions": (*tx.instructions, budget_instruction(1, heap))})
    assert "invalid_heap_size" in extract_features(tx).risk_flags


def test_zero_resource_requests_are_risky() -> None:
    tx = transaction()
    tx = tx.model_copy(
        update={
            "instructions": (budget_instruction(2, 0), budget_instruction(4, 0), tx.instructions[1])
        }
    )
    assert set(extract_features(tx).risk_flags) == {
        "zero_compute_limit",
        "zero_loaded_accounts_limit",
    }


def test_v1_config_has_total_lamport_fee_and_no_micro_lamport_price() -> None:
    features = extract_features(transaction("v1_transfer"))
    assert features.requested_compute_units == 30000
    assert features.requested_loaded_accounts_bytes == 200000
    assert features.requested_priority_fee_lamports == 500
    assert features.requested_micro_lamports is None
    assert features.risk_flags == ()


def test_v1_absent_inner_config_values_resolve_to_zero_limits() -> None:
    tx = transaction("v1_transfer").model_copy(update={"transaction_config": {}})
    features = extract_features(tx)
    assert features.requested_compute_units == features.requested_loaded_accounts_bytes == 0
    assert features.requested_heap_bytes is None
    assert set(features.risk_flags) == {"v1_zero_compute_limit", "v1_zero_loaded_accounts_limit"}


def test_v1_compute_budget_instructions_are_noops() -> None:
    tx = transaction("v1_transfer")
    tx = tx.model_copy(
        update={
            "accounts": (
                *tx.accounts,
                Account(pubkey=COMPUTE_BUDGET_PROGRAM, signer=False, writable=False),
            ),
            "instructions": (
                *tx.instructions,
                budget_instruction(2, 1_000_000),
                budget_instruction(1, 0),
            ),
        }
    )
    features = extract_features(tx)
    assert features.requested_compute_units == 30000
    assert features.requested_heap_bytes is None
    assert features.risk_flags == ("v1_compute_budget_noop",)


def test_v1_limit_and_fee_values_are_excluded_but_heap_is_retained() -> None:
    first = transaction("v1_transfer")
    second = first.model_copy(
        update={
            "transaction_config": {
                "computeUnitLimit": 90_000,
                "loadedAccountsDataSizeLimit": 300_000,
                "priorityFee": 1000,
            }
        }
    )
    assert extract_features(first).pattern_id == extract_features(second).pattern_id
    third = second.model_copy(
        update={"transaction_config": {**second.transaction_config, "heapSize": 65536}}
    )
    assert extract_features(second).pattern_id != extract_features(third).pattern_id


@pytest.mark.parametrize("data", ["0", "zz", "02 00"])
def test_normalized_input_hex_is_validated(data: str) -> None:
    tx = transaction()
    tx = tx.model_copy(
        update={"instructions": (tx.instructions[0].model_copy(update={"data_hex": data}),)}
    )
    with pytest.raises(ValueError, match="hex digits"):
        extract_features(tx)


def test_normalized_input_cannot_skip_lookup_resolution_validation() -> None:
    tx = transaction("v0_transfer").model_copy(update={"lookup_writable_count": 2})
    with pytest.raises(ValueError, match="lookup account counts"):
        extract_features(tx)


def test_normalized_input_program_must_be_in_account_list() -> None:
    tx = transaction("v1_transfer")
    tx = tx.model_copy(update={"instructions": (budget_instruction(2, 30_000),)})
    with pytest.raises(ValueError, match="program must be"):
        extract_features(tx)


def test_truncated_system_instruction_and_oversized_message_are_risky() -> None:
    tx = transaction()
    tx = tx.model_copy(
        update={
            "serialized_size": 1233,
            "instructions": (tx.instructions[1].model_copy(update={"data_hex": "02"}),),
        }
    )
    assert set(extract_features(tx).risk_flags) == {
        "truncated_instruction_discriminator",
        "transaction_size_exceeds_limit",
    }
