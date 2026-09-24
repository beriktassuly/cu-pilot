"""Portable, paired-resource quantiles and chronological joint-risk calibration.

This is a statistical component, not permission to skip a simulation: the bound
adapter additionally verifies message, workload, deployment and lifecycle evidence.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BeforeValidator, Field, PlainSerializer, StrictBool, StrictInt, model_validator

from cu_pilot.data import unique_observations
from cu_pilot.estimator import (
    NUMERICAL_FEATURES,
    NumericalRange,
    empirical_quantile,
    wilson_upper_bound,
)
from cu_pilot.schemas import (
    MAX_COMPUTE_UNITS,
    MAX_LOADED_ACCOUNT_BYTES,
    Features,
    Observation,
    Prediction,
    StrictModel,
)


def _slot(value: Any) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        if str(int(value)) != value:
            raise ValueError("slot strings must be canonical unsigned decimals")
        value = int(value)
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("slot must be a nonnegative integer below 2**64")
    return value


PortableSlot = Annotated[
    int,
    BeforeValidator(_slot),
    PlainSerializer(str, return_type=str, when_used="json"),
]


class ResourcePolicy(StrictModel):
    quantile: float = Field(default=0.99, gt=0, le=1, allow_inf_nan=False, strict=True)
    compute_margin_bps: StrictInt = Field(default=1000, ge=0, le=100_000)
    data_margin_bps: StrictInt = Field(default=1000, ge=0, le=100_000)
    compute_rounding: StrictInt = Field(default=100, ge=1, le=MAX_COMPUTE_UNITS)
    data_rounding: StrictInt = Field(default=1024, ge=1, le=MAX_LOADED_ACCOUNT_BYTES)
    min_samples: StrictInt = Field(default=30, ge=1)
    min_calibration_samples: StrictInt = Field(default=100, ge=1)
    max_joint_underestimation_rate: float = Field(
        default=0.05, gt=0, lt=1, allow_inf_nan=False, strict=True
    )
    max_age_slots: PortableSlot = 216_000
    near_limit_ratio: float = Field(default=0.9, gt=0, le=1, allow_inf_nan=False, strict=True)
    calibration_fraction: float = Field(default=0.3, gt=0, lt=1, allow_inf_nan=False, strict=True)
    independence_window_slots: StrictInt = Field(default=1, ge=1, le=2**32 - 1)
    allow_v1: StrictBool = False


class ResourceRange(NumericalRange):
    """Range endpoints have a bounded, exact JavaScript representation."""

    minimum: StrictInt | None = Field(default=None, ge=0, le=2**32 - 1)
    maximum: StrictInt | None = Field(default=None, ge=0, le=2**32 - 1)
    missing_seen: StrictBool = False


class ResourceStats(StrictModel):
    train_count: StrictInt = Field(ge=1)
    calibration_count: StrictInt = Field(ge=0)
    training_max_slot: PortableSlot
    calibration_min_slot: PortableSlot | None = None
    max_slot: PortableSlot
    quantile_compute_units: StrictInt = Field(ge=0, le=MAX_COMPUTE_UNITS)
    quantile_loaded_accounts_bytes: StrictInt = Field(ge=0, le=MAX_LOADED_ACCOUNT_BYTES)
    compute_unit_limit: StrictInt = Field(gt=0)
    loaded_accounts_data_size_limit: StrictInt = Field(gt=0)
    calibration_compute_exceedances: StrictInt = Field(ge=0)
    calibration_data_exceedances: StrictInt = Field(ge=0)
    calibration_joint_exceedances: StrictInt = Field(ge=0)
    calibration_upper_bound: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    numerical_ranges: dict[str, ResourceRange]
    instruction_data_length_ranges: tuple[ResourceRange, ...]
    program_ids: tuple[str, ...]
    version: Literal["legacy", 0, 1]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if set(self.numerical_ranges) != set(NUMERICAL_FEATURES):
            raise ValueError("missing or unknown numerical feature ranges")
        if not self.program_ids or len(self.program_ids) != len(
            self.instruction_data_length_ranges
        ):
            raise ValueError("inconsistent program and instruction ranges")
        if self.max_slot < self.training_max_slot:
            raise ValueError("profile freshness precedes fitting")
        cu = self.calibration_compute_exceedances
        data = self.calibration_data_exceedances
        joint = self.calibration_joint_exceedances
        if not max(cu, data) <= joint <= min(cu + data, self.calibration_count):
            raise ValueError("joint and marginal exceedance counts are inconsistent")
        if self.calibration_count:
            if self.calibration_min_slot is None or not (
                self.training_max_slot < self.calibration_min_slot <= self.max_slot
            ):
                raise ValueError("calibration must follow fitting")
            expected = wilson_upper_bound(joint, self.calibration_count)
            if self.calibration_upper_bound is None or not math.isclose(
                self.calibration_upper_bound, expected, rel_tol=1e-12
            ):
                raise ValueError("joint bound does not match observed counts")
        elif self.calibration_min_slot is not None or self.calibration_upper_bound is not None:
            raise ValueError("empty calibration must not claim a slot or bound")
        return self


class ResourceDiagnostics(StrictModel):
    input_count: StrictInt = Field(ge=0)
    duplicate_count: StrictInt = Field(ge=0)
    failed_count: StrictInt = Field(ge=0)
    missing_compute_count: StrictInt = Field(ge=0)
    missing_data_count: StrictInt = Field(ge=0)
    invalid_label_count: StrictInt = Field(ge=0)
    unsafe_feature_count: StrictInt = Field(ge=0)
    paired_usable_count: StrictInt = Field(ge=0)
    calibration_only_pattern_count: StrictInt = Field(ge=0)
    ineligible_calibration_count: StrictInt = Field(ge=0)


class ResourceArtifact(StrictModel):
    artifact_version: Literal["cu-pilot-resources-v1"] = "cu-pilot-resources-v1"
    context: str = Field(min_length=1)
    source: Literal["historical", "simulation", "synthetic"]
    label_source: Literal["historical", "simulation", "synthetic"] | None = None
    evidence_origin: (
        Literal["synthetic", "local-runtime", "live-simulation", "historical-execution"] | None
    ) = None
    max_slot: PortableSlot
    training_max_slot: PortableSlot
    calibration_min_slot: PortableSlot | None = None
    policy: ResourcePolicy
    patterns: dict[str, ResourceStats]
    diagnostics: ResourceDiagnostics

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.evidence_origin == "synthetic" and self.source != "synthetic":
            raise ValueError("synthetic evidence must retain synthetic provenance")
        if (
            self.source != "synthetic"
            and self.label_source is not None
            and self.label_source != self.source
        ):
            raise ValueError("label source conflicts with artifact provenance")
        if self.training_max_slot > self.max_slot:
            raise ValueError("training slot exceeds artifact slot")
        if self.calibration_min_slot is not None and not (
            self.training_max_slot < self.calibration_min_slot <= self.max_slot
        ):
            raise ValueError("invalid frozen chronological boundary")
        for pattern, stats in self.patterns.items():
            if not pattern or stats.training_max_slot > self.training_max_slot:
                raise ValueError("invalid pattern or fitting range")
            if stats.max_slot > self.max_slot:
                raise ValueError("profile exceeds artifact freshness")
            if stats.calibration_min_slot is not None and (
                self.calibration_min_slot is None
                or stats.calibration_min_slot < self.calibration_min_slot
            ):
                raise ValueError("profile calibration violates frozen boundary")
            if stats.compute_unit_limit != rounded_limit(
                stats.quantile_compute_units,
                self.policy.compute_margin_bps,
                self.policy.compute_rounding,
            ) or stats.loaded_accounts_data_size_limit != rounded_limit(
                stats.quantile_loaded_accounts_bytes,
                self.policy.data_margin_bps,
                self.policy.data_rounding,
            ):
                raise ValueError("resource limits do not match fitting quantiles and policy")
            window = self.policy.independence_window_slots
            if stats.calibration_min_slot is not None and (
                stats.training_max_slot // window >= stats.calibration_min_slot // window
            ):
                raise ValueError("an evidence window crosses fitting and calibration")
        return self


def rounded_limit(value: int, margin_bps: int, step: int) -> int:
    """Round upward using integer arithmetic; never silently cap a limit."""
    if any(type(item) is not int for item in (value, margin_bps, step)):
        raise ValueError("rounding requires exact integers")
    if value < 0 or margin_bps < 0 or step < 1:
        raise ValueError("invalid rounding arguments")
    denominator = 10_000 * step
    return max(step, ((value * (10_000 + margin_bps) + denominator - 1) // denominator) * step)


def paired_label(row: Observation) -> bool:
    label = row.label
    return (
        label.success
        and label.error is None
        and label.compute_units is not None
        and 0 <= label.compute_units <= MAX_COMPUTE_UNITS
        and label.loaded_accounts_bytes is not None
        and 0 <= label.loaded_accounts_bytes <= MAX_LOADED_ACCOUNT_BYTES
    )


def feature_risk(features: Features, policy: ResourcePolicy) -> str | None:
    if features.version == 1 and not policy.allow_v1:
        return "unsupported_version"
    if features.risk_flags:
        return "risky_features"
    integer_names = (
        *NUMERICAL_FEATURES,
        "requested_compute_units",
        "requested_loaded_accounts_bytes",
    )
    if any(
        value is not None and (type(value) is not int or value < 0)
        for value in (getattr(features, name) for name in integer_names)
    ):
        return "incomplete_features"
    if (
        features.account_count < 1
        or features.account_count > 256
        or features.signature_count < 1
        or features.signer_count != features.signature_count
        or features.signer_count > features.account_count
        or features.writable_count > features.account_count
        or features.instruction_count < 1
        or features.instruction_count != len(features.program_ids)
        or features.instruction_count != len(features.instruction_data_lengths)
        or any(type(n) is not int or n < 0 for n in features.instruction_data_lengths)
        or sum(features.instruction_data_lengths) != features.total_instruction_data_bytes
        or features.lookup_writable_count + features.lookup_readonly_count > features.account_count
        or not features.pattern_id
        or any(not p for p in features.program_ids)
    ):
        return "incomplete_features"
    if features.requested_compute_units is None:
        return "missing_compute_limit"
    if not 0 < features.requested_compute_units <= MAX_COMPUTE_UNITS:
        return "invalid_compute_budget"
    if features.requested_loaded_accounts_bytes is None:
        return "missing_loaded_accounts_limit"
    if not 0 < features.requested_loaded_accounts_bytes <= MAX_LOADED_ACCOUNT_BYTES:
        return "invalid_loaded_accounts_budget"
    heap = features.requested_heap_bytes
    if heap is not None and (not 32768 <= heap <= 262144 or heap % 1024):
        return "invalid_heap_budget"
    return None


def support_risk(features: Features, stats: ResourceStats, policy: ResourcePolicy) -> str | None:
    risk = feature_risk(features, policy)
    if risk:
        return risk
    if (
        features.version != stats.version
        or features.program_ids != stats.program_ids
        or len(features.instruction_data_lengths) != len(stats.instruction_data_length_ranges)
    ):
        return "shape_mismatch"
    if any(
        not bounds.contains(getattr(features, name))
        for name, bounds in stats.numerical_ranges.items()
    ):
        return "out_of_distribution"
    if any(
        not bounds.contains(length)
        for bounds, length in zip(
            stats.instruction_data_length_ranges, features.instruction_data_lengths, strict=True
        )
    ):
        return "out_of_distribution"
    return None


def limit_risk(stats: ResourceStats, policy: ResourcePolicy) -> str | None:
    for name, value, cap in (
        ("compute", stats.compute_unit_limit, MAX_COMPUTE_UNITS),
        ("loaded_accounts", stats.loaded_accounts_data_size_limit, MAX_LOADED_ACCOUNT_BYTES),
    ):
        if value >= cap:
            return f"hard_{name}_limit"
        if value >= cap * policy.near_limit_ratio:
            return f"near_{name}_limit"
    return None


def _range(values: list[int | None]) -> ResourceRange:
    present = [value for value in values if value is not None]
    return ResourceRange(
        minimum=min(present) if present else None,
        maximum=max(present) if present else None,
        missing_seen=len(values) != len(present),
    )


def _maxima(rows: list[Observation], window: int) -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    for row in rows:
        assert row.label.compute_units is not None and row.label.loaded_accounts_bytes is not None
        cu, data = result.get(row.slot // window, (0, 0))
        result[row.slot // window] = (
            max(cu, row.label.compute_units),
            max(data, row.label.loaded_accounts_bytes),
        )
    return result


class ResourceEstimator:
    def __init__(self, model: ResourceArtifact) -> None:
        self.model = model

    @classmethod
    def fit(cls, observations: list[Observation], policy: ResourcePolicy | None = None) -> Self:
        policy = policy or ResourcePolicy()
        if not observations:
            raise ValueError("fitting requires observations")
        contexts = {r.context for r in observations}
        sources = {r.source for r in observations}
        label_sources = {r.label_source or r.source for r in observations}
        origins = {r.evidence_origin for r in observations}
        if len(contexts) != 1 or len(sources) != 1 or len(label_sources) != 1 or len(origins) != 1:
            raise ValueError("fit one context and one label provenance at a time")
        rows = unique_observations(observations)
        window = policy.independence_window_slots
        windows = sorted({r.slot // window for r in rows})
        calibration_windows = min(
            len(windows) - 1, math.ceil(len(windows) * policy.calibration_fraction)
        )
        boundary_window = windows[-calibration_windows] if calibration_windows else None
        boundary = min(
            (
                r.slot
                for r in rows
                if boundary_window is not None and r.slot // window >= boundary_window
            ),
            default=None,
        )
        diagnostics = {
            "input_count": len(observations),
            "duplicate_count": len(observations) - len(rows),
            "failed_count": 0,
            "missing_compute_count": 0,
            "missing_data_count": 0,
            "invalid_label_count": 0,
            "unsafe_feature_count": 0,
            "paired_usable_count": 0,
            "calibration_only_pattern_count": 0,
            "ineligible_calibration_count": 0,
        }
        training: dict[str, list[Observation]] = defaultdict(list)
        calibration: dict[str, list[Observation]] = defaultdict(list)
        for row in rows:
            if not row.label.success or row.label.error is not None:
                diagnostics["failed_count"] += 1
                continue
            if row.label.compute_units is None or row.label.loaded_accounts_bytes is None:
                diagnostics["missing_compute_count"] += row.label.compute_units is None
                diagnostics["missing_data_count"] += row.label.loaded_accounts_bytes is None
                continue
            if not paired_label(row):
                diagnostics["invalid_label_count"] += 1
                continue
            if feature_risk(row.features, policy):
                diagnostics["unsafe_feature_count"] += 1
                continue
            diagnostics["paired_usable_count"] += 1
            target = calibration if boundary is not None and row.slot >= boundary else training
            target[row.features.pattern_id].append(row)
        if not training:
            raise ValueError("no safe paired-resource observations in chronological fitting data")
        diagnostics["calibration_only_pattern_count"] = len(set(calibration) - set(training))
        patterns: dict[str, ResourceStats] = {}
        for pattern, fitted in training.items():
            first = fitted[0].features
            later = calibration.get(pattern, [])
            if any(
                row.features.version != first.version
                or row.features.program_ids != first.program_ids
                or len(row.features.instruction_data_lengths) != len(first.instruction_data_lengths)
                for row in fitted + later
            ):
                raise ValueError("pattern ID contains incompatible shapes")
            maxima = _maxima(fitted, window)
            cu_quantile = empirical_quantile([pair[0] for pair in maxima.values()], policy.quantile)
            data_quantile = empirical_quantile(
                [pair[1] for pair in maxima.values()], policy.quantile
            )
            fit_slot = max(r.slot for r in fitted)
            stats = ResourceStats(
                train_count=len(maxima),
                calibration_count=0,
                training_max_slot=fit_slot,
                max_slot=fit_slot,
                quantile_compute_units=cu_quantile,
                quantile_loaded_accounts_bytes=data_quantile,
                compute_unit_limit=rounded_limit(
                    cu_quantile, policy.compute_margin_bps, policy.compute_rounding
                ),
                loaded_accounts_data_size_limit=rounded_limit(
                    data_quantile, policy.data_margin_bps, policy.data_rounding
                ),
                calibration_compute_exceedances=0,
                calibration_data_exceedances=0,
                calibration_joint_exceedances=0,
                numerical_ranges={
                    name: _range([getattr(r.features, name) for r in fitted])
                    for name in NUMERICAL_FEATURES
                },
                instruction_data_length_ranges=tuple(
                    _range([r.features.instruction_data_lengths[i] for r in fitted])
                    for i in range(len(first.instruction_data_lengths))
                ),
                program_ids=first.program_ids,
                version=first.version,
            )
            eligible: list[Observation] = []
            for row in later:
                # Anchor freshness to fit evidence. Requalification needs a new
                # fit; plentiful late labels cannot rescue already-stale limits.
                if (
                    support_risk(row.features, stats, policy)
                    or limit_risk(stats, policy)
                    or stats.train_count < policy.min_samples
                    or row.slot - fit_slot > policy.max_age_slots
                ):
                    diagnostics["ineligible_calibration_count"] += 1
                else:
                    eligible.append(row)
            pairs = list(_maxima(eligible, window).values())
            cu_fail = sum(cu > stats.compute_unit_limit for cu, _ in pairs)
            data_fail = sum(data > stats.loaded_accounts_data_size_limit for _, data in pairs)
            joint_fail = sum(
                cu > stats.compute_unit_limit or data > stats.loaded_accounts_data_size_limit
                for cu, data in pairs
            )
            patterns[pattern] = ResourceStats.model_validate(
                {
                    **stats.model_dump(),
                    "calibration_count": len(pairs),
                    "calibration_min_slot": min((r.slot for r in eligible), default=None),
                    "max_slot": max(r.slot for r in fitted + eligible),
                    "calibration_compute_exceedances": cu_fail,
                    "calibration_data_exceedances": data_fail,
                    "calibration_joint_exceedances": joint_fail,
                    "calibration_upper_bound": wilson_upper_bound(joint_fail, len(pairs))
                    if pairs
                    else None,
                }
            )
        return cls(
            ResourceArtifact(
                context=next(iter(contexts)),
                source=next(iter(sources)),
                label_source=observations[0].label_source,
                evidence_origin=observations[0].evidence_origin,
                max_slot=max(r.slot for r in rows),
                training_max_slot=max(r.slot for group in training.values() for r in group),
                calibration_min_slot=boundary,
                policy=policy,
                patterns=patterns,
                diagnostics=ResourceDiagnostics(**diagnostics),
            )
        )

    def predict(self, features: Features, *, context: str, current_slot: int) -> Prediction:
        if type(current_slot) is not int or not 0 <= current_slot < 2**64:
            raise ValueError("current_slot must be a nonnegative integer below 2**64")
        stats = self.model.patterns.get(features.pattern_id)
        reason: str | None = None
        if context != self.model.context:
            reason = "context_mismatch"
        elif current_slot < self.model.max_slot:
            reason = "backwards_slot"
        elif feature_risk(features, self.model.policy):
            reason = feature_risk(features, self.model.policy)
        elif stats is None:
            reason = "unknown_pattern"
        elif current_slot - stats.max_slot > self.model.policy.max_age_slots:
            reason = "stale_pattern"
        elif support_risk(features, stats, self.model.policy):
            reason = support_risk(features, stats, self.model.policy)
        elif stats.train_count < self.model.policy.min_samples:
            reason = "insufficient_training"
        elif stats.calibration_count < self.model.policy.min_calibration_samples:
            reason = "insufficient_calibration"
        elif (
            stats.calibration_upper_bound is None
            or stats.calibration_upper_bound > self.model.policy.max_joint_underestimation_rate
        ):
            reason = "joint_calibration_risk"
        else:
            reason = limit_risk(stats, self.model.policy)
        return Prediction(
            pattern_id=features.pattern_id,
            compute_unit_limit=stats.compute_unit_limit if stats and reason is None else None,
            loaded_accounts_data_size_limit=stats.loaded_accounts_data_size_limit
            if stats and reason is None
            else None,
            simulation_recommended=reason is not None,
            reason=reason or "calibrated_resources",
            explanation=(
                "Fallback required: " + reason
                if reason
                else "Rounded paired-resource limits passed chronological joint calibration; "
                "the empirical bound is not a guarantee of future transaction success."
            ),
            sample_count=stats.train_count if stats else 0,
            calibration_count=stats.calibration_count if stats else 0,
            calibration_underestimation_upper_bound=stats.calibration_upper_bound
            if stats
            else None,
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Self:
        return cls(ResourceArtifact.model_validate_json(Path(path).read_text(encoding="utf-8")))
