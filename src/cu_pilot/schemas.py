"""Versioned, JSON-safe interfaces. Labels are never prediction inputs."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

MAX_COMPUTE_UNITS = 1_400_000
MAX_LOADED_ACCOUNT_BYTES = 64 * 1024 * 1024
PATTERN_VERSION: Literal["shape-v1"] = "shape-v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("version", mode="before", check_fields=False)
    @classmethod
    def validate_version(cls, value: Any) -> Any:
        if value != "legacy" and type(value) is not int:
            raise ValueError("Version must be legacy or an integer")
        return value


class Account(StrictModel):
    pubkey: str
    signer: StrictBool
    writable: StrictBool
    source: Literal["static", "lookup"] = "static"


class Instruction(StrictModel):
    program_id: str
    accounts: tuple[StrictInt, ...]
    data_hex: str


class TransactionInput(StrictModel):
    version: Literal["legacy", 0, 1]
    accounts: tuple[Account, ...]
    instructions: tuple[Instruction, ...]
    signature_count: StrictInt = Field(ge=0)
    lookup_table_count: StrictInt = Field(default=0, ge=0)
    lookup_writable_count: StrictInt = Field(default=0, ge=0)
    lookup_readonly_count: StrictInt = Field(default=0, ge=0)
    serialized_size: StrictInt | None = Field(default=None, gt=0)
    transaction_config: dict[str, StrictInt | None] | None = None


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
    compute_units: StrictInt | None = Field(default=None, ge=0)
    loaded_accounts_bytes: StrictInt | None = Field(default=None, ge=0)
    success: StrictBool
    error: Any = None


class Observation(StrictModel):
    record_id: str = Field(min_length=1)
    slot: StrictInt = Field(ge=0)
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
    current_slot: StrictInt = Field(ge=0)


def usable_compute_label(label: ResourceLabel) -> bool:
    return (
        label.success
        and label.error is None
        and label.compute_units is not None
        and 0 < label.compute_units <= MAX_COMPUTE_UNITS
    )
