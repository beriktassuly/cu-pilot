"""Versioned, JSON-safe interfaces. Labels are never prediction inputs."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_COMPUTE_UNITS = 1_400_000
MAX_LOADED_ACCOUNT_BYTES = 64 * 1024 * 1024
PATTERN_VERSION = "shape-v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Account(StrictModel):
    pubkey: str
    signer: bool
    writable: bool
    source: Literal["static", "lookup"] = "static"


class Instruction(StrictModel):
    program_id: str
    accounts: tuple[int, ...]
    data_hex: str


class TransactionInput(StrictModel):
    version: Literal["legacy", 0, 1]
    accounts: tuple[Account, ...]
    instructions: tuple[Instruction, ...]
    signature_count: int = Field(ge=0)
    lookup_table_count: int = Field(default=0, ge=0)
    lookup_writable_count: int = Field(default=0, ge=0)
    lookup_readonly_count: int = Field(default=0, ge=0)
    serialized_size: int | None = Field(default=None, gt=0)
    transaction_config: dict[str, int | None] | None = None


class Features(StrictModel):
    schema_version: Literal["shape-v1"] = PATTERN_VERSION
    pattern_id: str
    version: Literal["legacy", 0, 1]
    signature_count: int = Field(ge=0)
    account_count: int = Field(ge=0)
    signer_count: int = Field(ge=0)
    writable_count: int = Field(ge=0)
    instruction_count: int = Field(ge=0)
    program_ids: tuple[str, ...]
    instruction_data_lengths: tuple[int, ...]
    total_instruction_data_bytes: int = Field(ge=0)
    lookup_table_count: int = Field(ge=0)
    lookup_writable_count: int = Field(ge=0)
    lookup_readonly_count: int = Field(ge=0)
    serialized_size: int | None = Field(default=None, gt=0)
    requested_compute_units: int | None = Field(default=None, ge=0)
    requested_loaded_accounts_bytes: int | None = Field(default=None, ge=0)
    requested_heap_bytes: int | None = Field(default=None, ge=0)
    requested_micro_lamports: int | None = Field(default=None, ge=0)
    requested_priority_fee_lamports: int | None = Field(default=None, ge=0)
    risk_flags: tuple[str, ...] = ()


class ResourceLabel(StrictModel):
    compute_units: int | None = Field(default=None, ge=0)
    loaded_accounts_bytes: int | None = Field(default=None, ge=0)
    success: bool
    error: Any = None


class Observation(StrictModel):
    record_id: str
    slot: int = Field(ge=0)
    context: str = Field(min_length=1)
    source: Literal["historical", "simulation", "synthetic"]
    features: Features
    label: ResourceLabel


class Prediction(StrictModel):
    pattern_id: str | None
    compute_unit_limit: int | None = None
    loaded_accounts_data_size_limit: int | None = None
    simulation_recommended: bool
    reason: str
    explanation: str
    sample_count: int = 0
    calibration_count: int = 0
    calibration_underestimation_upper_bound: float | None = None


class PredictRequest(StrictModel):
    transaction: TransactionInput
    context: str = Field(min_length=1)
    current_slot: int = Field(ge=0)
