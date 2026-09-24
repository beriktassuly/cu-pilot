"""Pre-execution features and a versioned, deterministic transaction shape hash."""

from __future__ import annotations

import hashlib
import json
import string
from typing import Any

from cu_pilot.parsing import validate_config, validate_pubkey
from cu_pilot.schemas import PATTERN_VERSION, Features, TransactionInput

SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}
_HEX_DIGITS = frozenset(string.hexdigits)


def _validated_data(tx: TransactionInput) -> list[bytes]:
    """Validate normalized caller inputs as carefully as RPC inputs."""
    if not tx.accounts or len(tx.accounts) > 256:
        raise ValueError("transaction must contain between 1 and 256 accounts")
    if not (tx.accounts[0].signer and tx.accounts[0].writable):
        raise ValueError("first account must be a writable signer (fee payer)")
    keys = [validate_pubkey(account.pubkey) for account in tx.accounts]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate account keys; reuse an account index instead")
    if tx.signature_count != sum(account.signer for account in tx.accounts):
        raise ValueError("signature count must match signer accounts")
    lookup_writable = sum(
        account.source == "lookup" and account.writable for account in tx.accounts
    )
    lookup_readonly = sum(
        account.source == "lookup" and not account.writable for account in tx.accounts
    )
    if (tx.lookup_writable_count, tx.lookup_readonly_count) != (lookup_writable, lookup_readonly):
        raise ValueError("resolved lookup account counts are inconsistent")
    if any(account.signer and account.source == "lookup" for account in tx.accounts):
        raise ValueError("lookup accounts cannot be signers")
    if (lookup_writable or lookup_readonly) and not tx.lookup_table_count:
        raise ValueError("lookup accounts require at least one lookup table")
    if tx.version != 0 and (tx.lookup_table_count or lookup_writable or lookup_readonly):
        raise ValueError("only v0 supports address lookup tables")
    if tx.version != 1 and tx.transaction_config is not None:
        raise ValueError("transaction config is only supported for v1")
    if tx.version == 1 and tx.transaction_config is None:
        raise ValueError("v1 transaction requires transaction config")
    if tx.transaction_config is not None:
        validate_config(tx.transaction_config)
    result: list[bytes] = []
    for instruction in tx.instructions:
        validate_pubkey(instruction.program_id)
        if instruction.program_id not in keys:
            raise ValueError("program must be in the resolved account list")
        if any(
            type(index) is not int or not 0 <= index < len(keys) for index in instruction.accounts
        ):
            raise ValueError("instruction account index is out of bounds")
        if len(instruction.data_hex) % 2 or any(
            char not in _HEX_DIGITS for char in instruction.data_hex
        ):
            raise ValueError("instruction data must contain an even number of hex digits")
        result.append(bytes.fromhex(instruction.data_hex))
    return result


def _budget_features(
    tx: TransactionInput, instruction_data: list[bytes]
) -> tuple[dict[str, int | None], set[str]]:
    budget: dict[str, int | None] = {
        "requested_compute_units": None,
        "requested_loaded_accounts_bytes": None,
        "requested_heap_bytes": None,
        "requested_micro_lamports": None,
        "requested_priority_fee_lamports": None,
    }
    risks: set[str] = set()
    if tx.version == 1:
        config = validate_config(tx.transaction_config)
        budget.update(
            requested_compute_units=config["computeUnitLimit"] or 0,
            requested_loaded_accounts_bytes=config["loadedAccountsDataSizeLimit"] or 0,
            requested_heap_bytes=config["heapSize"],
            requested_priority_fee_lamports=config["priorityFee"] or 0,
        )
        if budget["requested_compute_units"] == 0:
            risks.add("v1_zero_compute_limit")
        if budget["requested_loaded_accounts_bytes"] == 0:
            risks.add("v1_zero_loaded_accounts_limit")
        if any(ix.program_id == COMPUTE_BUDGET_PROGRAM for ix in tx.instructions):
            risks.add("v1_compute_budget_noop")
    else:
        fields = {
            1: ("requested_heap_bytes", 5),
            2: ("requested_compute_units", 5),
            3: ("requested_micro_lamports", 9),
            4: ("requested_loaded_accounts_bytes", 5),
        }
        seen: set[int] = set()
        for instruction, data in zip(tx.instructions, instruction_data, strict=True):
            if instruction.program_id != COMPUTE_BUDGET_PROGRAM:
                continue
            if not data or data[0] not in fields:
                risks.add("invalid_compute_budget_instruction")
                continue
            tag = data[0]
            if tag in seen:
                risks.add("duplicate_compute_budget_instruction")
            seen.add(tag)
            field, expected_length = fields[tag]
            if len(data) != expected_length:
                risks.add("invalid_compute_budget_instruction")
                continue
            budget[field] = int.from_bytes(data[1:], "little")
        if budget["requested_compute_units"] == 0:
            risks.add("zero_compute_limit")
        if budget["requested_loaded_accounts_bytes"] == 0:
            risks.add("zero_loaded_accounts_limit")
    heap = budget["requested_heap_bytes"]
    if heap is not None and (not 32_768 <= heap <= 262_144 or heap % 1_024):
        risks.add("invalid_heap_size")
    return budget, risks


def _discriminator(program: str, data: bytes) -> str:
    length = 4 if program == SYSTEM_PROGRAM else 1 if program in TOKEN_PROGRAMS else 8
    if program == COMPUTE_BUDGET_PROGRAM:
        length = 1  # Never let requested limits/prices become a shape identifier.
    return data[:length].hex()


def _pattern_payload(
    tx: TransactionInput, data: list[bytes], requested_heap: int | None
) -> dict[str, Any]:
    # Canonical labels preserve repeated account references and cross-instruction
    # aliases without depending on public keys or compiler ordering of peer accounts.
    canonical: dict[int, int] = {0: 0}
    keys = {account.pubkey: index for index, account in enumerate(tx.accounts)}

    def account_label(index: int) -> int:
        if index not in canonical:
            canonical[index] = len(canonical)
        return canonical[index]

    instructions = []
    for instruction, raw in zip(tx.instructions, data, strict=True):
        instructions.append(
            {
                "program": instruction.program_id,
                "program_account": account_label(keys[instruction.program_id]),
                "accounts": [account_label(index) for index in instruction.accounts],
                "discriminator": _discriminator(instruction.program_id, raw),
                "data_length": len(raw),
            }
        )
    roles = [
        [tx.accounts[index].signer, tx.accounts[index].writable, tx.accounts[index].source]
        for index in canonical
    ]
    unused = sorted(
        (account.signer, account.writable, account.source)
        for index, account in enumerate(tx.accounts)
        if index not in canonical
    )
    return {
        "schema": PATTERN_VERSION,
        "version": tx.version,
        "signature_count": tx.signature_count,
        "roles": roles,
        "unused_roles": unused,
        "instructions": instructions,
        "lookups": [
            tx.lookup_table_count,
            tx.lookup_writable_count,
            tx.lookup_readonly_count,
        ],
        "heap_bytes": 32_768 if requested_heap is None else requested_heap,
    }


def extract_features(tx: TransactionInput) -> Features:
    """Extract only pre-execution fields and a deterministic SHA-256 shape ID.

    Shape is a grouping heuristic, never a proof of identical execution. Account
    state, amount-dependent branches, CPI behavior and upgrades can change cost.
    """
    data = _validated_data(tx)
    budget, risks = _budget_features(tx, data)
    if not tx.instructions:
        risks.add("empty_instruction_list")
    if any(
        instruction.program_id == SYSTEM_PROGRAM
        and len(raw) < 4
        or instruction.program_id in TOKEN_PROGRAMS
        and not raw
        for instruction, raw in zip(tx.instructions, data, strict=True)
    ):
        risks.add("truncated_instruction_discriminator")
    size_limit = 4_096 if tx.version == 1 else 1_232
    if tx.serialized_size is not None and tx.serialized_size > size_limit:
        risks.add("transaction_size_exceeds_limit")
    if any(len(raw) > size_limit for raw in data):
        risks.add("instruction_data_exceeds_transaction_limit")
    payload = _pattern_payload(tx, data, budget["requested_heap_bytes"])
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lengths = tuple(map(len, data))
    return Features(
        pattern_id=f"{PATTERN_VERSION}:{digest}",
        version=tx.version,
        signature_count=tx.signature_count,
        account_count=len(tx.accounts),
        signer_count=sum(account.signer for account in tx.accounts),
        writable_count=sum(account.writable for account in tx.accounts),
        instruction_count=len(tx.instructions),
        program_ids=tuple(instruction.program_id for instruction in tx.instructions),
        instruction_data_lengths=lengths,
        total_instruction_data_bytes=sum(lengths),
        lookup_table_count=tx.lookup_table_count,
        lookup_writable_count=tx.lookup_writable_count,
        lookup_readonly_count=tx.lookup_readonly_count,
        serialized_size=tx.serialized_size,
        risk_flags=tuple(sorted(risks)),
        requested_compute_units=budget["requested_compute_units"],
        requested_loaded_accounts_bytes=budget["requested_loaded_accounts_bytes"],
        requested_heap_bytes=budget["requested_heap_bytes"],
        requested_micro_lamports=budget["requested_micro_lamports"],
        requested_priority_fee_lamports=budget["requested_priority_fee_lamports"],
    )
