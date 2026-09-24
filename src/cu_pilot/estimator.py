"""Conservative empirical quantiles with chronological calibration and abstention.

Artifacts contain only validated JSON. No estimator state uses execution labels as
prediction inputs, and fitting/calibration never modifies a prediction at runtime.
"""

import math
from collections import defaultdict
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from cu_pilot.schemas import (
    MAX_COMPUTE_UNITS,
    MAX_LOADED_ACCOUNT_BYTES,
    Features,
    Observation,
    Prediction,
    StrictModel,
)

# These inputs can alter execution even within a discriminator/length-bucket shape.
NUMERICAL_FEATURES = (
    "signature_count",
    "account_count",
    "signer_count",
    "writable_count",
    "instruction_count",
    "total_instruction_data_bytes",
    "lookup_table_count",
    "lookup_writable_count",
    "lookup_readonly_count",
    "serialized_size",
    "requested_heap_bytes",
)


class Policy(StrictModel):
    quantile: float = Field(default=0.99, gt=0, le=1, allow_inf_nan=False)
    safety_margin: float = Field(default=0.10, ge=0, le=10, allow_inf_nan=False)
    min_samples: int = Field(default=30, ge=1)
    min_calibration_samples: int = Field(default=100, ge=1)
    max_underestimation_rate: float = Field(default=0.05, gt=0, lt=1, allow_inf_nan=False)
    max_age_slots: int = Field(default=216_000, ge=0)
    near_limit_ratio: float = Field(default=0.9, gt=0, le=1, allow_inf_nan=False)
    calibration_fraction: float = Field(default=0.3, gt=0, lt=1, allow_inf_nan=False)


class NumericalRange(StrictModel):
    minimum: int | None = Field(default=None, ge=0)
    maximum: int | None = Field(default=None, ge=0)
    missing_seen: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("range endpoints must both be present or absent")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise ValueError("range minimum exceeds maximum")
        elif not self.missing_seen:
            raise ValueError("empty numerical range")
        return self

    def contains(self, value: int | None) -> bool:
        if value is None:
            return self.missing_seen
        return (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum <= value <= self.maximum
        )


class PatternStats(StrictModel):
    train_count: int = Field(ge=1)
    calibration_count: int = Field(ge=0)
    training_max_slot: int = Field(ge=0)
    calibration_min_slot: int | None = Field(default=None, ge=0)
    max_slot: int = Field(ge=0)
    quantile_units: int = Field(gt=0, le=MAX_COMPUTE_UNITS)
    proposed_limit: int = Field(gt=0)
    calibration_underestimations: int = Field(ge=0)
    calibration_upper_bound: float | None = Field(default=None, ge=0, le=1)
    numerical_ranges: dict[str, NumericalRange]
    instruction_data_length_ranges: tuple[NumericalRange, ...]
    program_ids: tuple[str, ...]
    version: Literal["legacy", 0, 1]

    @model_validator(mode="after")
    def validate_stats(self) -> Self:
        if set(self.numerical_ranges) != set(NUMERICAL_FEATURES):
            raise ValueError("missing or unknown numerical feature ranges")
        if self.max_slot < self.training_max_slot:
            raise ValueError("pattern freshness predates its training")
        if self.calibration_underestimations > self.calibration_count:
            raise ValueError("underestimation count exceeds calibration count")
        if self.calibration_count:
            if self.calibration_min_slot is None:
                raise ValueError("calibration slot is required")
            if not self.training_max_slot < self.calibration_min_slot <= self.max_slot:
                raise ValueError(
                    "training and calibration slots must be disjoint and chronological"
                )
            expected = wilson_upper_bound(self.calibration_underestimations, self.calibration_count)
            if self.calibration_upper_bound is None or not math.isclose(
                expected, self.calibration_upper_bound, rel_tol=1e-12
            ):
                raise ValueError("calibration upper bound does not match counts")
        elif self.calibration_min_slot is not None or self.calibration_upper_bound is not None:
            raise ValueError("empty calibration cannot have a bound or slot")
        return self


class FitDiagnostics(StrictModel):
    input_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    missing_label_count: int = Field(ge=0)
    invalid_label_count: int = Field(ge=0)
    unsafe_feature_count: int = Field(ge=0)
    usable_count: int = Field(ge=0)
    calibration_only_pattern_count: int = Field(ge=0)
    ineligible_calibration_count: int = Field(default=0, ge=0)


class EstimatorArtifact(StrictModel):
    artifact_version: Literal["cu-pilot-pattern-estimator-v1"] = "cu-pilot-pattern-estimator-v1"
    context: str = Field(min_length=1)
    source: Literal["historical", "simulation", "synthetic"]
    max_slot: int = Field(ge=0)
    training_max_slot: int = Field(ge=0)
    calibration_min_slot: int | None = Field(default=None, ge=0)
    policy: Policy
    patterns: dict[str, PatternStats]
    diagnostics: FitDiagnostics

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        if self.training_max_slot > self.max_slot:
            raise ValueError("training slot exceeds artifact slot")
        if self.calibration_min_slot is not None:
            if not self.training_max_slot < self.calibration_min_slot <= self.max_slot:
                raise ValueError("global training/calibration boundary is invalid")
        for pattern_id, stats in self.patterns.items():
            if not pattern_id:
                raise ValueError("empty pattern identifier")
            if stats.max_slot > self.max_slot or stats.training_max_slot > self.training_max_slot:
                raise ValueError("pattern slots exceed artifact bounds")
            if stats.calibration_count and (
                self.calibration_min_slot is None
                or stats.calibration_min_slot is None
                or stats.calibration_min_slot < self.calibration_min_slot
            ):
                raise ValueError("pattern calibration violates global split")
            if stats.proposed_limit != conservative_limit(stats.quantile_units, self.policy):
                raise ValueError("proposed limit does not match quantile and safety margin")
        return self


def empirical_quantile(values: list[int], quantile: float) -> int:
    """Nearest-rank empirical quantile, without interpolation or extra dependencies."""
    if not values or not 0 < quantile <= 1:
        raise ValueError("quantile needs nonempty values and 0 < quantile <= 1")
    return sorted(values)[math.ceil(quantile * len(values)) - 1]


def conservative_limit(quantile_units: int, policy: Policy) -> int:
    # Decimal avoids rounding 100 * 1.10 up to 111 due to binary floating point.
    value = Decimal(quantile_units) * (Decimal(1) + Decimal(str(policy.safety_margin)))
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def wilson_upper_bound(failures: int, count: int) -> float:
    """One-sided 95% Wilson bound; not a prediction confidence probability."""
    if count <= 0 or not 0 <= failures <= count:
        raise ValueError("Wilson bound requires 0 <= failures <= count and count > 0")
    z = 1.6448536269514722
    proportion = failures / count
    z_squared = z * z
    center = proportion + z_squared / (2 * count)
    radius = z * math.sqrt(proportion * (1 - proportion) / count + z_squared / (4 * count**2))
    return min(1.0, (center + radius) / (1 + z_squared / count))


def _feature_risk(
    features: Features, *, require_compute_limit: bool = False
) -> tuple[str, str] | None:
    if features.version == 1:
        return "unsupported_version", "Version 1 requires resource handling beyond this CU model."
    if features.risk_flags:
        return "risky_features", "Feature extraction flagged: " + ", ".join(features.risk_flags)
    if (
        features.requested_loaded_accounts_bytes is not None
        and features.requested_loaded_accounts_bytes != MAX_LOADED_ACCOUNT_BYTES
    ):
        return (
            "loaded_accounts_limit",
            "A nondefault loaded-account limit requires simulation; this model predicts CU only.",
        )
    if (
        features.account_count < 1
        or features.signature_count < 1
        or features.signer_count < 1
        or features.signer_count > features.account_count
        or features.writable_count > features.account_count
        or features.signer_count != features.signature_count
        or features.instruction_count < 1
        or features.instruction_count != len(features.program_ids)
        or features.instruction_count != len(features.instruction_data_lengths)
        or any(length < 0 for length in features.instruction_data_lengths)
        or sum(features.instruction_data_lengths) != features.total_instruction_data_bytes
        or features.lookup_writable_count + features.lookup_readonly_count > features.account_count
    ):
        return "incomplete_features", "Account or instruction features are missing or inconsistent."
    if not features.pattern_id or any(not program_id for program_id in features.program_ids):
        return "incomplete_features", "A pattern ID and complete program IDs are required."
    if features.requested_compute_units is not None and not (
        0 < features.requested_compute_units <= MAX_COMPUTE_UNITS
    ):
        return "invalid_compute_budget", "The requested compute limit is outside supported bounds."
    if require_compute_limit and features.requested_compute_units is None:
        return (
            "missing_compute_limit",
            "Include a positive compute-limit instruction before prediction; "
            "inserting one afterward changes the transaction's execution shape.",
        )
    if features.requested_heap_bytes is not None and not (
        32_768 <= features.requested_heap_bytes <= 262_144
        and features.requested_heap_bytes % 1024 == 0
    ):
        return "invalid_heap_budget", "The requested heap size is outside supported bounds."
    return None


def _support_risk(
    features: Features,
    *,
    version: Literal["legacy", 0, 1],
    program_ids: tuple[str, ...],
    numerical_ranges: dict[str, NumericalRange],
    instruction_data_length_ranges: tuple[NumericalRange, ...],
) -> tuple[str, str] | None:
    """Apply the same feature eligibility rule to calibration and prediction."""
    feature_risk = _feature_risk(features, require_compute_limit=True)
    if feature_risk:
        return feature_risk
    if (
        features.version != version
        or features.program_ids != program_ids
        or len(features.instruction_data_lengths) != len(instruction_data_length_ranges)
    ):
        return "shape_mismatch", "Features do not match the trained pattern definition."
    for name, value_range in numerical_ranges.items():
        if not value_range.contains(getattr(features, name)):
            return "out_of_distribution", "Outside the training range for " + name + "."
    for length, value_range in zip(
        features.instruction_data_lengths, instruction_data_length_ranges, strict=True
    ):
        if not value_range.contains(length):
            return "out_of_distribution", "Instruction data length is outside training support."
    return None


def _numerical_range(values: list[int | None]) -> NumericalRange:
    present = [value for value in values if value is not None]
    return NumericalRange(
        minimum=min(present) if present else None,
        maximum=max(present) if present else None,
        missing_seen=any(value is None for value in values),
    )


def _slot_maxima(observations: list[Observation]) -> list[int]:
    maxima: dict[int, int] = {}
    for observation in observations:
        assert observation.label.compute_units is not None
        maxima[observation.slot] = max(
            maxima.get(observation.slot, 0), observation.label.compute_units
        )
    return list(maxima.values())


class PatternEstimator:
    """A fixed empirical model whose public output explicitly recommends simulation."""

    def __init__(self, model: EstimatorArtifact) -> None:
        self.model = model

    @classmethod
    def fit(cls, observations: list[Observation], policy: Policy | None = None) -> Self:
        policy = policy or Policy()
        if not observations:
            raise ValueError("fitting requires observations")
        contexts = {observation.context for observation in observations}
        sources = {observation.source for observation in observations}
        if len(contexts) != 1:
            raise ValueError("fit one context at a time; cluster/program epochs must not mix")
        if (
            len(sources) != 1
            or len({r.label_source or r.source for r in observations}) != 1
            or len({r.evidence_origin for r in observations}) != 1
        ):
            raise ValueError("historical, simulation, and synthetic provenance must not mix")
        seen: dict[str, Observation] = {}
        diagnostics = {
            "input_count": len(observations),
            "duplicate_count": 0,
            "failed_count": 0,
            "missing_label_count": 0,
            "invalid_label_count": 0,
            "unsafe_feature_count": 0,
            "usable_count": 0,
            "calibration_only_pattern_count": 0,
            "ineligible_calibration_count": 0,
        }
        usable: list[Observation] = []
        for observation in observations:
            if not observation.record_id:
                raise ValueError("record IDs must be nonempty")
            if observation.record_id in seen:
                if observation != seen[observation.record_id]:
                    raise ValueError(
                        "conflicting observations share record ID " + observation.record_id
                    )
                diagnostics["duplicate_count"] += 1
                continue
            seen[observation.record_id] = observation
            if not observation.label.success or observation.label.error is not None:
                diagnostics["failed_count"] += 1
            elif observation.label.compute_units is None:
                diagnostics["missing_label_count"] += 1
            elif not 0 < observation.label.compute_units <= MAX_COMPUTE_UNITS:
                diagnostics["invalid_label_count"] += 1
            elif _feature_risk(observation.features):
                diagnostics["unsafe_feature_count"] += 1
            else:
                usable.append(observation)
        if not usable:
            raise ValueError("no safe successful observations with valid compute labels")
        diagnostics["usable_count"] = len(usable)
        # Fix the time boundary before eligibility filtering: otherwise removing
        # risky late rows can pull easy earlier observations into calibration.
        slots = sorted({observation.slot for observation in seen.values()})
        calibration_slots = min(len(slots) - 1, math.ceil(len(slots) * policy.calibration_fraction))
        boundary = slots[-calibration_slots] if calibration_slots else None
        training: dict[str, list[Observation]] = defaultdict(list)
        calibration: dict[str, list[Observation]] = defaultdict(list)
        for observation in usable:
            destination = (
                calibration if boundary is not None and observation.slot >= boundary else training
            )
            destination[observation.features.pattern_id].append(observation)
        if not training:
            raise ValueError("no safe observations remain in the chronological training partition")
        diagnostics["calibration_only_pattern_count"] = len(set(calibration) - set(training))
        patterns: dict[str, PatternStats] = {}
        for pattern_id, train_rows in training.items():
            calibration_rows = calibration.get(pattern_id, [])
            first = train_rows[0].features
            # Detect incompatible hand-crafted Features or corrupted pattern IDs.
            for row in train_rows + calibration_rows:
                if (
                    row.features.version != first.version
                    or row.features.program_ids != first.program_ids
                ):
                    raise ValueError(
                        "pattern ID refers to incompatible transaction programs/version"
                    )
                if len(row.features.instruction_data_lengths) != len(
                    first.instruction_data_lengths
                ):
                    raise ValueError("pattern ID refers to incompatible instruction counts")
            numerical_ranges = {
                name: _numerical_range([getattr(row.features, name) for row in train_rows])
                for name in NUMERICAL_FEATURES
            }
            instruction_data_length_ranges = tuple(
                _numerical_range([row.features.instruction_data_lengths[i] for row in train_rows])
                for i in range(len(first.instruction_data_lengths))
            )
            # The risk bound must describe only transactions that could receive
            # predictions. Easy OOD rows must not dilute in-support failures.
            eligible_calibration_rows = [
                row
                for row in calibration_rows
                if _support_risk(
                    row.features,
                    version=first.version,
                    program_ids=first.program_ids,
                    numerical_ranges=numerical_ranges,
                    instruction_data_length_ranges=instruction_data_length_ranges,
                )
                is None
            ]
            diagnostics["ineligible_calibration_count"] += len(calibration_rows) - len(
                eligible_calibration_rows
            )
            calibration_rows = eligible_calibration_rows
            train_values = _slot_maxima(train_rows)
            calibration_values = _slot_maxima(calibration_rows)
            quantile_units = empirical_quantile(train_values, policy.quantile)
            proposed_limit = conservative_limit(quantile_units, policy)
            failures = sum(value > proposed_limit for value in calibration_values)
            patterns[pattern_id] = PatternStats(
                train_count=len(train_values),
                calibration_count=len(calibration_values),
                training_max_slot=max(row.slot for row in train_rows),
                calibration_min_slot=min((row.slot for row in calibration_rows), default=None),
                max_slot=max(row.slot for row in train_rows + calibration_rows),
                quantile_units=quantile_units,
                proposed_limit=proposed_limit,
                calibration_underestimations=failures,
                calibration_upper_bound=(
                    wilson_upper_bound(failures, len(calibration_values))
                    if calibration_values
                    else None
                ),
                numerical_ranges=numerical_ranges,
                instruction_data_length_ranges=instruction_data_length_ranges,
                program_ids=first.program_ids,
                version=first.version,
            )
        return cls(
            EstimatorArtifact(
                context=next(iter(contexts)),
                source=next(iter(sources)),
                max_slot=max(observation.slot for observation in observations),
                training_max_slot=max(row.slot for rows in training.values() for row in rows),
                calibration_min_slot=boundary,
                policy=policy,
                patterns=patterns,
                diagnostics=FitDiagnostics(**diagnostics),
            )
        )

    def predict(self, features: Features, *, context: str, current_slot: int) -> Prediction:
        if type(current_slot) is not int or current_slot < 0:
            raise ValueError("current_slot must be a nonnegative integer")
        stats = self.model.patterns.get(features.pattern_id)

        def fallback(reason: str, explanation: str) -> Prediction:
            return Prediction(
                pattern_id=features.pattern_id,
                simulation_recommended=True,
                reason=reason,
                explanation=explanation,
                sample_count=stats.train_count if stats else 0,
                calibration_count=stats.calibration_count if stats else 0,
                calibration_underestimation_upper_bound=(
                    stats.calibration_upper_bound if stats else None
                ),
            )

        if context != self.model.context:
            return fallback(
                "context_mismatch", "Cluster/program epoch differs from the fitted model."
            )
        if current_slot < self.model.max_slot:
            return fallback(
                "backwards_slot", "Prediction slot precedes observations in this artifact."
            )
        feature_risk = _feature_risk(features, require_compute_limit=True)
        if feature_risk:
            return fallback(*feature_risk)
        if stats is None:
            return fallback(
                "unknown_pattern", "No training observations exist for this transaction shape."
            )
        if current_slot - stats.max_slot > self.model.policy.max_age_slots:
            return fallback(
                "stale_pattern", "Pattern observations are older than the allowed slot age."
            )
        support_risk = _support_risk(
            features,
            version=stats.version,
            program_ids=stats.program_ids,
            numerical_ranges=stats.numerical_ranges,
            instruction_data_length_ranges=stats.instruction_data_length_ranges,
        )
        if support_risk:
            return fallback(*support_risk)
        if stats.train_count < self.model.policy.min_samples:
            return fallback(
                "insufficient_training", "Too few distinct training slots for this pattern."
            )
        if stats.calibration_count < self.model.policy.min_calibration_samples:
            return fallback(
                "insufficient_calibration", "Too few distinct chronological calibration slots."
            )
        if (
            stats.calibration_upper_bound is None
            or stats.calibration_upper_bound > self.model.policy.max_underestimation_rate
        ):
            return fallback(
                "calibration_risk",
                "The one-sided 95% underestimation bound exceeds the configured tolerance.",
            )
        if stats.proposed_limit >= MAX_COMPUTE_UNITS:
            return fallback(
                "hard_compute_limit", "Conservative estimate reaches or exceeds the CU cap."
            )
        if stats.proposed_limit >= MAX_COMPUTE_UNITS * self.model.policy.near_limit_ratio:
            return fallback(
                "near_compute_limit", "Conservative estimate is too close to the CU cap."
            )
        return Prediction(
            pattern_id=features.pattern_id,
            compute_unit_limit=stats.proposed_limit,
            simulation_recommended=False,
            reason="calibrated_pattern",
            explanation=(
                f"Training p{self.model.policy.quantile * 100:g} plus "
                f"{self.model.policy.safety_margin:.0%} margin passed chronological calibration; "
                "the Wilson bound describes historical slot samples, not guaranteed future safety."
            ),
            sample_count=stats.train_count,
            calibration_count=stats.calibration_count,
            calibration_underestimation_upper_bound=stats.calibration_upper_bound,
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Self:
        return cls(EstimatorArtifact.model_validate_json(Path(path).read_text(encoding="utf-8")))
