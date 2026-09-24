"""Lossless parsing of compiled Solana RPC JSON; no post-execution features."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, cast

from cu_pilot.schemas import Account, Instruction, Observation, ResourceLabel, TransactionInput

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_VALUES = {char: index for index, char in enumerate(_BASE58_ALPHABET)}
_CONFIG_FIELDS = {
    "computeUnitLimit": 32,
    "heapSize": 32,
    "loadedAccountsDataSizeLimit": 32,
    "priorityFee": 64,
}


def decode_base58(value: str) -> bytes:
    """Decode base58 without an SDK dependency, rejecting noncanonical characters."""
    if not isinstance(value, str):
        raise ValueError("base58 input must be a string")
    if len(value) > 90_000:
        raise ValueError("base58 input exceeds supported transaction size")
    number = 0
    for char in value:
        if char not in _BASE58_VALUES:
            raise ValueError("invalid base58 character")
        number = number * 58 + _BASE58_VALUES[char]
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + number.to_bytes((number.bit_length() + 7) // 8, "big")


def validate_pubkey(value: Any) -> str:
    """Check the wire width as well as the alphabet of a public key."""
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        raise ValueError("public key must be a base58 string encoding 32 bytes")
    if len(decode_base58(value)) != 32:
        raise ValueError("public key must encode exactly 32 bytes")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _uint(value: Any, name: str, bits: int = 64) -> int:
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")
    return value


def _result(raw: dict[str, Any]) -> dict[str, Any]:
    raw = _object(raw, "transaction response")
    if raw.get("error") is not None:
        raise ValueError("RPC response contains an error")
    return _object(raw["result"], "RPC result") if "result" in raw else raw


def _version(value: Any) -> Literal["legacy", 0, 1]:
    if value == "legacy":
        return "legacy"
    if type(value) is int and value in (0, 1):
        return cast(Literal[0, 1], value)
    raise ValueError("unsupported transaction version; expected legacy, 0, or 1")


def validate_config(config: Any) -> dict[str, int | None]:
    """Validate the RPC projection of the v1 config (priorityFee is lamports)."""
    config = _object(config, "transactionConfig")
    if set(config) - _CONFIG_FIELDS.keys():
        raise ValueError("unknown transactionConfig field")
    result: dict[str, int | None] = {}
    for key, bits in _CONFIG_FIELDS.items():
        value = config.get(key)
        result[key] = None if value is None else _uint(value, key, bits)
    return result


def _lookup_accounts(
    message: dict[str, Any], meta: dict[str, Any], version: Literal["legacy", 0, 1]
) -> tuple[list[Account], int, int, int]:
    lookups = _array(message.get("addressTableLookups", []), "addressTableLookups")
    if version != 0 and lookups:
        raise ValueError("only v0 transactions support address lookup tables")
    writable_count = readonly_count = 0
    for raw_lookup in lookups:
        lookup = _object(raw_lookup, "address lookup")
        validate_pubkey(lookup.get("accountKey"))
        writable = _array(lookup.get("writableIndexes"), "writableIndexes")
        readonly = _array(lookup.get("readonlyIndexes"), "readonlyIndexes")
        for index in writable + readonly:
            _uint(index, "lookup index", 8)
        if len(set(writable + readonly)) != len(writable + readonly):
            raise ValueError("duplicate address lookup index")
        writable_count += len(writable)
        readonly_count += len(readonly)
    loaded_raw = meta.get("loadedAddresses")
    if loaded_raw is None:
        if writable_count or readonly_count:
            raise ValueError("v0 lookup addresses are unresolved; loadedAddresses is required")
        return [], len(lookups), 0, 0
    loaded = _object(loaded_raw, "loadedAddresses")
    writable_keys = _array(loaded.get("writable"), "loadedAddresses.writable")
    readonly_keys = _array(loaded.get("readonly"), "loadedAddresses.readonly")
    if (len(writable_keys), len(readonly_keys)) != (writable_count, readonly_count):
        raise ValueError("loadedAddresses counts do not match addressTableLookups")
    accounts = [
        Account(pubkey=validate_pubkey(key), signer=False, writable=True, source="lookup")
        for key in writable_keys
    ] + [
        Account(pubkey=validate_pubkey(key), signer=False, writable=False, source="lookup")
        for key in readonly_keys
    ]
    return accounts, len(lookups), writable_count, readonly_count


def parse_transaction(raw: dict[str, Any]) -> TransactionInput:
    """Read a getTransaction result/envelope or a compiled unsigned message wrapper.

    ``jsonParsed`` and binary RPC encodings are intentionally unsupported: losing
    original instruction bytes or account indices would corrupt pattern identity.
    Unsigned wrappers may omit signatures; their count comes from the header.
    """
    result = _result(raw)
    transaction = _object(result.get("transaction", result), "transaction")
    message = _object(transaction.get("message"), "message")
    if "version" not in result and "version" not in transaction:
        if "transactionConfig" in message or message.get("addressTableLookups"):
            raise ValueError("version is required for versioned messages")
    version = _version(result.get("version", transaction.get("version", "legacy")))
    if "transactionConfig" in message and version != 1:
        raise ValueError("transactionConfig is only supported for v1")
    if version == 1 and "transactionConfig" not in message:
        raise ValueError("v1 message requires transactionConfig; check RPC compatibility")
    config = validate_config(message["transactionConfig"]) if version == 1 else None

    keys = _array(message.get("accountKeys"), "accountKeys")
    if not keys or len(keys) > 256:
        raise ValueError("accountKeys must contain between 1 and 256 public keys")
    if any(not isinstance(key, str) for key in keys):
        raise ValueError("jsonParsed account keys are unsupported; request encoding=json")
    header = _object(message.get("header"), "header")
    required = _uint(header.get("numRequiredSignatures"), "numRequiredSignatures", 8)
    readonly_signed = _uint(header.get("numReadonlySignedAccounts"), "numReadonlySignedAccounts", 8)
    readonly_unsigned = _uint(
        header.get("numReadonlyUnsignedAccounts"), "numReadonlyUnsignedAccounts", 8
    )
    if not 1 <= required <= len(keys) or readonly_signed >= required:
        raise ValueError("invalid signer header or non-writable fee payer")
    if readonly_unsigned > len(keys) - required:
        raise ValueError("invalid readonly unsigned account count")
    signatures = transaction.get("signatures")
    if signatures is not None:
        signatures = _array(signatures, "signatures")
        if len(signatures) != required:
            raise ValueError("signature count does not match message header")
        for signature in signatures:
            if not isinstance(signature, str) or not 64 <= len(signature) <= 88:
                raise ValueError("signature must be a base58 string encoding 64 bytes")
            if len(decode_base58(signature)) != 64:
                raise ValueError("signature must encode exactly 64 bytes")
    if "recentBlockhash" in message:
        validate_pubkey(message["recentBlockhash"])

    accounts = [
        Account(
            pubkey=validate_pubkey(key),
            signer=index < required,
            writable=(
                index < required - readonly_signed
                if index < required
                else index < len(keys) - readonly_unsigned
            ),
        )
        for index, key in enumerate(keys)
    ]
    meta = result.get("meta")
    lookup_accounts, lookup_count, writable_count, readonly_count = _lookup_accounts(
        message, {} if meta is None else _object(meta, "meta"), version
    )
    accounts.extend(lookup_accounts)
    if len(accounts) > 256:
        raise ValueError("resolved account list exceeds index range")
    if len({account.pubkey for account in accounts}) != len(accounts):
        raise ValueError("duplicate account keys; reuse an account index instead")
    instructions: list[Instruction] = []
    for raw_instruction in _array(message.get("instructions"), "instructions"):
        instruction = _object(raw_instruction, "instruction")
        if "parsed" in instruction or "programIdIndex" not in instruction:
            raise ValueError("jsonParsed instructions are unsupported; request encoding=json")
        program_index = _uint(instruction["programIdIndex"], "programIdIndex", 8)
        indices = _array(instruction.get("accounts"), "instruction accounts")
        for index in indices:
            _uint(index, "instruction account index", 8)
        if any(index >= len(accounts) for index in [program_index, *indices]):
            raise ValueError("instruction account index is out of bounds")
        encoded_data = instruction.get("data")
        if not isinstance(encoded_data, str):
            raise ValueError("instruction data must be a base58 string")
        data = decode_base58(encoded_data)
        instructions.append(
            Instruction(
                program_id=accounts[program_index].pubkey,
                accounts=tuple(indices),
                data_hex=data.hex(),
            )
        )
    return TransactionInput(
        version=version,
        accounts=tuple(accounts),
        instructions=tuple(instructions),
        signature_count=required,
        lookup_table_count=lookup_count,
        lookup_writable_count=writable_count,
        lookup_readonly_count=readonly_count,
        transaction_config=config,
    )


def normalize_observation(
    raw: dict[str, Any],
    *,
    context: str,
    source: Literal["historical", "simulation", "synthetic"] = "historical",
) -> Observation:
    """Normalize historical-shaped records without admitting metadata as features.

    Simulation responses alone contain no message and cannot be parsed here. Join
    them with their original pre-execution message in a caller-side collection adapter.
    """
    from cu_pilot.features import extract_features

    result = _result(raw)
    transaction = parse_transaction(result)
    meta = _object(result.get("meta"), "meta")
    if "err" not in meta:
        raise ValueError("meta.err is required to distinguish success from unknown status")
    units = meta.get("computeUnitsConsumed")
    loaded_bytes = meta.get("loadedAccountsDataSize")
    if units is not None:
        units = _uint(units, "meta.computeUnitsConsumed")
    if loaded_bytes is not None:
        loaded_bytes = _uint(loaded_bytes, "meta.loadedAccountsDataSize")
    signatures = _object(result.get("transaction", result), "transaction").get("signatures")
    record_id = (
        signatures[0]
        if signatures
        else "sha256:"
        + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return Observation(
        record_id=record_id,
        slot=_uint(result.get("slot"), "slot"),
        context=context,
        source=source,
        features=extract_features(transaction),
        label=ResourceLabel(
            compute_units=units,
            loaded_accounts_bytes=loaded_bytes,
            success=meta["err"] is None,
            error=meta["err"],
        ),
    )
