"""Application policies and fit-only baselines under one frozen resource budget.

These helpers do not sign, send, simulate or qualify a core release. The runtime
benchmark supplies measured fallback/control outcomes and pays every RPC cost.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, StrictInt

from cu_pilot.estimator import empirical_quantile
from cu_pilot.resources import paired_label, rounded_limit
from cu_pilot.schemas import StrictModel
from examples.payouts.model import (
    PayoutModelBundle,
    PayoutObservation,
    PayoutStateEnvelope,
    canonical_digest,
    unique_rows,
)


class PlanningPolicy(StrictModel):
    """Declared before collection; identical for every method and ablation."""

    policy_version: Literal["payout-local-policy-v1"] = "payout-local-policy-v1"
    compute_cap: StrictInt = Field(default=100_000, gt=0, le=1_400_000)
    loaded_bytes_cap: StrictInt = Field(default=1_048_576, gt=0, le=67_108_864)
    wire_bytes_cap: StrictInt = Field(default=1232, gt=0, le=1232)
    account_cap: StrictInt = Field(default=64, ge=1, le=256)
    allowed_version: Literal["legacy"] = "legacy"


PLANNING_POLICY = PlanningPolicy()


def eligible_counts(remaining: int) -> tuple[int, ...]:
    if type(remaining) is not int or not 0 <= remaining <= 16:
        raise ValueError("remaining queue length must be an integer from zero through sixteen")
    return tuple(
        sorted(
            {n for n in (1, 2, 4, 8) if n <= remaining}
            | ({remaining} if 0 < remaining <= 8 else set())
        )
    )


class CandidateEstimate(StrictModel):
    count: StrictInt = Field(ge=1, le=8)
    compute_units: StrictInt | None = Field(default=None, ge=1)
    loaded_bytes: StrictInt | None = Field(default=None, ge=1)
    serialized_size: StrictInt = Field(gt=0)
    account_count: StrictInt = Field(gt=0)
    version: Literal["legacy", 0, 1] = "legacy"
    source: str
    reason: str

    def infeasible_reason(self, policy: PlanningPolicy = PLANNING_POLICY) -> str | None:
        if self.version != policy.allowed_version:
            return "unsupported_transaction_version"
        if self.serialized_size > policy.wire_bytes_cap:
            return "transaction_too_large"
        if self.account_count > policy.account_cap:
            return "too_many_accounts"
        if self.compute_units is None or self.loaded_bytes is None:
            return "missing_resource_estimate"
        if self.compute_units > policy.compute_cap:
            return "compute_policy_cap"
        if self.loaded_bytes > policy.loaded_bytes_cap:
            return "loaded_data_policy_cap"
        return None


def choose_candidate(
    estimates: list[CandidateEstimate],
    *,
    remaining: int,
    policy: PlanningPolicy = PLANNING_POLICY,
) -> CandidateEstimate | None:
    if len({candidate.count for candidate in estimates}) != len(estimates):
        raise ValueError("each candidate prefix must have exactly one estimate")
    eligible = set(eligible_counts(remaining))
    return max(
        (
            candidate
            for candidate in estimates
            if candidate.count in eligible and candidate.infeasible_reason(policy) is None
        ),
        key=lambda candidate: candidate.count,
        default=None,
    )


def fixed_ablation_estimate(count: int) -> tuple[int, int]:
    """Predeclared causal intervention, not the tuned fixed-batch competitor."""
    return 20_000 + 25_000 * count, PLANNING_POLICY.loaded_bytes_cap


def _solve_three(matrix: list[list[float]], target: list[float]) -> tuple[float, float, float]:
    augmented = [list(row) + [value] for row, value in zip(matrix, target, strict=True)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda index: abs(augmented[index][column]))
        if abs(augmented[pivot][column]) < 1e-9:
            raise ValueError("formula fitting needs independent count and missing-ATA variation")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for index in range(3):
            if index != column:
                multiplier = augmented[index][column]
                augmented[index] = [
                    a - multiplier * b
                    for a, b in zip(
                        augmented[index],
                        augmented[column],
                        strict=True,
                    )
                ]
    return tuple(max(0.0, augmented[index][3]) for index in range(3))  # type: ignore[return-value]


class BaselineArtifact(StrictModel):
    artifact_version: Literal["payout-fit-only-baselines-v1"] = "payout-fit-only-baselines-v1"
    bundle_digest: str
    fitted_observations: StrictInt = Field(ge=1)
    fit_record_ids: tuple[str, ...]
    fixed_batch_count: StrictInt | None = Field(default=None, ge=1, le=8)
    fixed_count_limits: dict[str, tuple[int, int]]
    formula_coefficients: tuple[float, float, float] | None
    formula_upper_residual: StrictInt = Field(ge=0)
    formula_loaded_bytes: StrictInt = Field(ge=1)
    supported_counts: tuple[int, ...]
    policy: PlanningPolicy

    def formula(self, state: PayoutStateEnvelope) -> tuple[int, int] | None:
        if self.formula_coefficients is None or state.candidate_count not in self.supported_counts:
            return None
        intercept, per_payment, per_missing = self.formula_coefficients
        cu = (
            math.ceil(
                intercept + per_payment * state.candidate_count + per_missing * state.missing_atas
            )
            + self.formula_upper_residual
        )
        return rounded_limit(cu, 1000, 100), self.formula_loaded_bytes

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def fit_baselines(
    bundle: PayoutModelBundle,
    observations: list[PayoutObservation],
    *,
    policy: PlanningPolicy = PLANNING_POLICY,
) -> BaselineArtifact:
    """Tune competitors on fitting labels only; calibration/holdout never optimize them.

    Fixed batch chooses the largest fitting-supported count whose conservative
    all-state p99 fits the common caps. Formula uses a three-term least-squares
    packing rule with a fitting maximum positive residual plus the same margin.
    Reporting that this simple fitted formula wins is an intended valid outcome.
    """
    rows = unique_rows(observations)
    development_ids = set(bundle.split.fit_ids + bundle.split.calibration_ids)
    development = [row for row in rows if row.record_id in development_ids]
    if canonical_digest([row.model_dump(mode="json") for row in development]) != (
        bundle.development_digest
    ):
        raise ValueError("baseline development observations differ from the frozen bundle")
    fit_ids = set(bundle.split.fit_ids)
    fitting = [
        row
        for row in rows
        if row.record_id in fit_ids
        and paired_label(row.observation)
        and row.state.risk(
            current_slot=row.observation.slot,
            deployment_bindings=bundle.deployment_bindings,
        )
        is None
    ]
    if not fitting:
        raise ValueError("baselines require successful paired fitting observations")
    counts: dict[int, list[PayoutObservation]] = defaultdict(list)
    for row in fitting:
        counts[row.state.candidate_count].append(row)
    count_limits = {}
    feasible_counts = []
    for count, group in counts.items():
        cu_values = [r.observation.label.compute_units for r in group]
        data_values = [r.observation.label.loaded_accounts_bytes for r in group]
        assert all(value is not None for value in cu_values + data_values)
        cu = rounded_limit(
            empirical_quantile([v for v in cu_values if v is not None], 0.99), 1000, 100
        )
        data = rounded_limit(
            empirical_quantile([v for v in data_values if v is not None], 0.99), 1000, 1024
        )
        count_limits[str(count)] = (cu, data)
        support = len({row.queue_group for row in group})
        if (
            support >= bundle.policy.min_samples
            and cu <= policy.compute_cap
            and data <= policy.loaded_bytes_cap
            and count in (1, 2, 4, 8)
        ):
            feasible_counts.append(count)
    matrix = [[0.0] * 3 for _ in range(3)]
    target = [0.0] * 3
    for row in fitting:
        vector = (1, row.state.candidate_count, row.state.missing_atas)
        assert row.observation.label.compute_units is not None
        for i in range(3):
            target[i] += vector[i] * row.observation.label.compute_units
            for j in range(3):
                matrix[i][j] += vector[i] * vector[j]
    try:
        coefficients = _solve_three(matrix, target)
    except ValueError:
        coefficients = None
    residual = 0
    if coefficients is not None:
        for row in fitting:
            assert row.observation.label.compute_units is not None
            expected = (
                coefficients[0]
                + coefficients[1] * row.state.candidate_count
                + (coefficients[2] * row.state.missing_atas)
            )
            residual = max(residual, math.ceil(row.observation.label.compute_units - expected))
    return BaselineArtifact(
        bundle_digest=bundle.digest,
        fitted_observations=len(fitting),
        fit_record_ids=tuple(sorted(r.record_id for r in fitting)),
        fixed_batch_count=max(feasible_counts, default=None),
        fixed_count_limits=count_limits,
        formula_coefficients=coefficients,
        formula_upper_residual=residual,
        formula_loaded_bytes=max(limits[1] for limits in count_limits.values()),
        supported_counts=tuple(sorted(counts)),
        policy=policy,
    )
