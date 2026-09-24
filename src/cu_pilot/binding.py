"""SDK-decoded authoritative messages and narrowly controlled unsigned preparation.

No feature/bytes pair is accepted. Signatures are always discarded on preparation.
Identity hashes the complete SDK message serialization (including its blockhash).
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, StrictInt
from solders.address_lookup_table_account import ID as ALT_OWNER
from solders.address_lookup_table_account import AddressLookupTable
from solders.hash import Hash
from solders.instruction import CompiledInstruction
from solders.message import (
    Message,
    MessageHeader,
    MessageV0,
    MessageV1,
    TransactionConfig,
    VersionedMessage,
    to_bytes_versioned,
)
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from cu_pilot.features import COMPUTE_BUDGET_PROGRAM, extract_features
from cu_pilot.schemas import (
    MAX_COMPUTE_UNITS,
    MAX_LOADED_ACCOUNT_BYTES,
    Account,
    Features,
    Instruction,
    StrictModel,
    TransactionInput,
)


def slot_value(value: int) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("slot must be an unsigned 64-bit integer")
    return value


class LookupEvidence(StrictModel):
    """Raw RPC table account, never an unrelated list of resolved keys."""

    address: str
    owner: str
    data_base64: str
    slot: StrictInt = Field(ge=0)

    @classmethod
    def from_rpc(cls, address: str, result: dict[str, Any]) -> LookupEvidence:
        value = result.get("value")
        if not isinstance(value, dict) or value.get("executable") is not False:
            raise ValueError("lookup account unavailable or executable")
        data = value.get("data")
        if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
            raise ValueError("lookup account must use base64 encoding")
        return cls(
            address=address,
            owner=value["owner"],
            data_base64=data[0],
            slot=result["context"]["slot"],
        )

    def addresses(self, current_slot: int, max_age_slots: int) -> list[str]:
        slot_value(current_slot)
        if type(max_age_slots) is not int or max_age_slots < 0:
            raise ValueError("invalid lookup freshness window")
        if not 0 <= current_slot - self.slot <= max_age_slots:
            raise ValueError("lookup evidence is stale or from the future")
        if self.owner != str(ALT_OWNER):
            raise ValueError("invalid lookup account owner")
        table = AddressLookupTable.deserialize(base64.b64decode(self.data_base64, validate=True))
        # Deactivating tables depend on SlotHashes; this adapter deliberately rejects them.
        if table.meta.deactivation_slot != 2**64 - 1:
            raise ValueError("deactivating lookup tables are unsupported")
        if table.meta.last_extended_slot > self.slot:
            raise ValueError("lookup metadata is newer than its observation")
        length = len(table.addresses)
        if table.meta.last_extended_slot == self.slot:
            length = table.meta.last_extended_slot_start_index
        return [str(key) for key in table.addresses[:length]]


def decode_wire(wire_base64: str) -> VersionedTransaction:
    try:
        raw = base64.b64decode(wire_base64, validate=True)
        if not raw or len(raw) > 4096:
            raise ValueError("invalid transaction size")
        transaction = VersionedTransaction.from_bytes(raw)
        transaction.sanitize()
        if bytes(transaction) != raw:
            raise ValueError("noncanonical transaction encoding")
        if not isinstance(transaction.message, MessageV1) and len(raw) > 1232:
            raise ValueError("legacy/v0 transaction exceeds 1232 bytes")
        return transaction
    except Exception as exc:
        raise ValueError("invalid or unsupported serialized transaction") from exc


def message_identity(message: VersionedMessage) -> str:
    return "message-v1:" + hashlib.sha256(to_bytes_versioned(message)).hexdigest()


def unsigned_wire(message: VersionedMessage) -> str:
    transaction = VersionedTransaction.populate(
        message, [Signature.default()] * message.header.num_required_signatures
    )
    wire = base64.b64encode(bytes(transaction)).decode("ascii")
    decode_wire(wire)
    return wire


def normalize_message(
    message: VersionedMessage,
    *,
    lookups: Mapping[str, LookupEvidence] | None = None,
    current_slot: int,
    max_lookup_age_slots: int = 32,
) -> TransactionInput:
    slot_value(current_slot)
    keys = [str(key) for key in message.account_keys]
    header = message.header
    required = header.num_required_signatures
    accounts = [
        Account(
            pubkey=key,
            signer=i < required,
            writable=(
                i < required - header.num_readonly_signed_accounts
                if i < required
                else i < len(keys) - header.num_readonly_unsigned_accounts
            ),
        )
        for i, key in enumerate(keys)
    ]
    writable: list[str] = []
    readonly: list[str] = []
    lookup_count = 0
    version: Literal["legacy", 0, 1] = "legacy"
    config = None
    if isinstance(message, MessageV0):
        version = 0
        lookup_count = len(message.address_table_lookups)
        for lookup in message.address_table_lookups:
            key = str(lookup.account_key)
            if lookups is None or key not in lookups or lookups[key].address != key:
                raise ValueError("v0 lookup evidence is required")
            addresses = lookups[key].addresses(current_slot, max_lookup_age_slots)
            try:
                writable.extend(addresses[i] for i in lookup.writable_indexes)
                readonly.extend(addresses[i] for i in lookup.readonly_indexes)
            except IndexError:
                raise ValueError("lookup index is unavailable at the evidence slot") from None
        accounts.extend(
            Account(pubkey=k, signer=False, writable=True, source="lookup") for k in writable
        )
        accounts.extend(
            Account(pubkey=k, signer=False, writable=False, source="lookup") for k in readonly
        )
    elif isinstance(message, MessageV1):
        version = 1
        config = {
            "computeUnitLimit": message.config.compute_unit_limit,
            "loadedAccountsDataSizeLimit": message.config.loaded_accounts_data_size_limit,
            "heapSize": message.config.heap_size,
            "priorityFee": message.config.priority_fee,
        }
    tx = TransactionInput(
        version=version,
        accounts=tuple(accounts),
        signature_count=required,
        instructions=tuple(
            Instruction(
                program_id=accounts[ix.program_id_index].pubkey,
                accounts=tuple(ix.accounts),
                data_hex=ix.data.hex(),
            )
            for ix in message.instructions
        ),
        lookup_table_count=lookup_count,
        lookup_writable_count=len(writable),
        lookup_readonly_count=len(readonly),
        transaction_config=config,
        serialized_size=len(base64.b64decode(unsigned_wire(message))),
    )
    extract_features(tx)  # Semantic validation supplements SDK sanitization.
    return tx


def replace_resources(
    message: VersionedMessage,
    compute_units: int,
    loaded_bytes: int,
    *,
    add_missing: bool = False,
    blockhash: Hash | None = None,
) -> VersionedMessage:
    """Preserve fees, heap, account roles, nonce-first order, and existing instruction order.

    Missing budget placeholders are appended ONLY during initial preparation.
    Appending a static program key shifts resolved v0 indices without changing roles.
    """
    if type(compute_units) is not int or not 0 < compute_units <= MAX_COMPUTE_UNITS:
        raise ValueError("compute limit outside protocol bounds")
    if type(loaded_bytes) is not int or not 0 < loaded_bytes <= MAX_LOADED_ACCOUNT_BYTES:
        raise ValueError("loaded-data limit outside protocol bounds")
    lifetime = message.recent_blockhash if blockhash is None else blockhash
    if isinstance(message, MessageV1):
        return MessageV1(
            message.header,
            TransactionConfig(
                priority_fee=message.config.priority_fee,
                compute_unit_limit=compute_units,
                loaded_accounts_data_size_limit=loaded_bytes,
                heap_size=message.config.heap_size,
            ),
            lifetime,
            message.account_keys,
            message.instructions,
        )
    keys = list(message.account_keys)
    header = message.header
    instructions = list(message.instructions)
    program = Pubkey.from_string(COMPUTE_BUDGET_PROGRAM)
    if program not in keys:
        if not add_missing:
            raise ValueError("missing resource placeholders")
        static_count = len(keys)
        keys.append(program)
        header = MessageHeader(
            header.num_required_signatures,
            header.num_readonly_signed_accounts,
            header.num_readonly_unsigned_accounts + 1,
        )
        instructions = [
            CompiledInstruction(
                ix.program_id_index + (ix.program_id_index >= static_count),
                ix.data,
                bytes(i + (i >= static_count) for i in ix.accounts),
            )
            for ix in instructions
        ]
    program_index = keys.index(program)
    positions: dict[int, int] = {}
    for index, instruction in enumerate(instructions):
        if instruction.program_id_index != program_index:
            continue
        raw = instruction.data
        if not raw or raw[0] not in {1, 2, 3, 4} or len(raw) != (9 if raw[0] == 3 else 5):
            raise ValueError("invalid compute budget instruction")
        if raw[0] in positions:
            raise ValueError("duplicate compute budget instruction")
        positions[raw[0]] = index
    for tag, limit in [(2, compute_units), (4, loaded_bytes)]:
        if tag not in positions:
            if not add_missing:
                raise ValueError("missing resource placeholder")
            instructions.append(
                CompiledInstruction(program_index, bytes([tag]) + limit.to_bytes(4, "little"), b"")
            )
        else:
            old = instructions[positions[tag]]
            instructions[positions[tag]] = CompiledInstruction(
                old.program_id_index, bytes([tag]) + limit.to_bytes(4, "little"), old.accounts
            )
    if isinstance(message, MessageV0):
        return MessageV0(header, keys, lifetime, instructions, message.address_table_lookups)
    return Message.new_with_compiled_instructions(
        header.num_required_signatures,
        header.num_readonly_signed_accounts,
        header.num_readonly_unsigned_accounts,
        keys,
        lifetime,
        instructions,
    )


@dataclass(frozen=True)
class BoundMessage:
    original_identity: str
    prepared_identity: str
    wire_base64: str
    transaction: TransactionInput
    features: Features
    durable_nonce: bool


def bind_message(
    wire_base64: str,
    *,
    current_slot: int,
    lookups: Mapping[str, LookupEvidence] | None = None,
    max_lookup_age_slots: int = 32,
) -> BoundMessage:
    original = decode_wire(wire_base64)
    tx = normalize_message(
        original.message,
        lookups=lookups,
        current_slot=current_slot,
        max_lookup_age_slots=max_lookup_age_slots,
    )
    risks = set(extract_features(tx).risk_flags)
    # V1 unset resources are expected at the builder boundary; placeholders fill them.
    risks -= {"v1_zero_compute_limit", "v1_zero_loaded_accounts_limit"}
    if risks:
        raise ValueError("invalid resource message: " + ",".join(sorted(risks)))
    prepared = replace_resources(
        original.message, MAX_COMPUTE_UNITS, MAX_LOADED_ACCOUNT_BYTES, add_missing=True
    )
    prepared_tx = normalize_message(
        prepared,
        lookups=lookups,
        current_slot=current_slot,
        max_lookup_age_slots=max_lookup_age_slots,
    )
    return BoundMessage(
        message_identity(original.message),
        message_identity(prepared),
        unsigned_wire(prepared),
        prepared_tx,
        extract_features(prepared_tx),
        original.uses_durable_nonce(),
    )


def verify_final_message(
    expected_wire: str, actual_wire: str, *, allow_blockhash_refresh: bool = False
) -> str:
    """Verify exact message equality, ignoring signatures; return the rebound identity.

    Explicit ordinary blockhash refresh is the sole optional transform. Changing
    resource values after estimation requires a new decision too.
    """
    expected, actual = decode_wire(expected_wire), decode_wire(actual_wire)
    comparison = actual.message
    if allow_blockhash_refresh:
        if expected.uses_durable_nonce() or actual.uses_durable_nonce():
            raise ValueError("durable nonce refresh requires a new decision")
        # Preserve every actual field except lifetime using SDK constructors.
        msg = actual.message
        if isinstance(msg, MessageV1):
            comparison = MessageV1(
                msg.header,
                msg.config,
                expected.message.recent_blockhash,
                msg.account_keys,
                msg.instructions,
            )
        elif isinstance(msg, MessageV0):
            comparison = MessageV0(
                msg.header,
                msg.account_keys,
                expected.message.recent_blockhash,
                msg.instructions,
                msg.address_table_lookups,
            )
        else:
            h = msg.header
            comparison = Message.new_with_compiled_instructions(
                h.num_required_signatures,
                h.num_readonly_signed_accounts,
                h.num_readonly_unsigned_accounts,
                msg.account_keys,
                expected.message.recent_blockhash,
                msg.instructions,
            )
    if to_bytes_versioned(expected.message) != to_bytes_versioned(comparison):
        raise ValueError("message changed; estimate resources again")
    return message_identity(actual.message)
