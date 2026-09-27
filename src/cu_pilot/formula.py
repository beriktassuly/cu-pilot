"""Experimental, bound NNLS resource formulas with independent qualification.

Whole cohorts, not correlated transaction rows, calibrate the joint residual.
Conformal coverage is marginal under exchangeability, never a production SLO.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction
from itertools import combinations
from typing import Final, Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator
from solders.pubkey import Pubkey

from cu_pilot.derivation import canonical_ata
from cu_pilot.estimator import NUMERICAL_FEATURES, wilson_upper_bound
from cu_pilot.resources import PortableSlot, ResourcePolicy, ResourceRange, feature_risk
from cu_pilot.schemas import (
    MAX_COMPUTE_UNITS,
    MAX_LOADED_ACCOUNT_BYTES,
    Features,
    Prediction,
    StrictModel,
    TransactionInput,
)

FORMULA_VERSION: Final = "cu-pilot-joint-conformal-formula-v1"
FEATURE_VERSION: Final = "cu-pilot-ata-derivation-effort-v1"
METHOD_TERMS = {
    "count_missing": ("intercept", "count", "missing"),
    "count_missing_derivation": (
        "intercept",
        "count",
        "missing",
        "total_ata_attempts",
        "missing_ata_attempts",
    ),
}
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SHA256 = r"^[0-9a-f]{64}$"


def features_digest(features: Features) -> str:
    """Compute budget limits may change when applying a bound decision."""
    payload = features.model_dump(
        mode="json", exclude={"requested_compute_units", "requested_loaded_accounts_bytes"}
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def nonnegative_least_squares(
    vectors: list[tuple[int, ...]], targets: list[int]
) -> tuple[float, ...]:
    """Exact active-face search for at most five terms; scaled reorthogonalized QR.

    Every face of the nonnegative orthant is considered. A rank-deficient face
    has a lower-dimensional equivalent, so it can be skipped. This refits free
    coefficients after imposing zero constraints, unlike clipping ordinary LS.
    """
    if not vectors or len(vectors) != len(targets):
        raise ValueError("NNLS requires equally sized nonempty inputs")
    width = len(vectors[0])
    if not 1 <= width <= 5 or any(len(row) != width for row in vectors):
        raise ValueError("NNLS requires one to five consistent terms")
    if any(type(v) is not int or v < 0 for row in vectors for v in row) or any(
        type(y) is not int or y < 0 for y in targets
    ):
        raise ValueError("NNLS values must be nonnegative exact integers")
    columns = [[float(row[j]) for row in vectors] for j in range(width)]
    scales = [math.sqrt(math.fsum(v * v for v in column)) for column in columns]
    best = [0.0] * width
    best_error = math.fsum(float(y) ** 2 for y in targets)
    for size in range(1, width + 1):
        for active in combinations(range(width), size):
            if any(scales[j] == 0 for j in active):
                continue
            q: list[list[float]] = []
            upper = [[0.0] * size for _ in range(size)]
            for j, index in enumerate(active):
                column = [v / scales[index] for v in columns[index]]
                # Two passes keep small correlated design matrices stable.
                for _ in range(2):
                    for i, basis in enumerate(q):
                        projection = math.fsum(a * b for a, b in zip(basis, column, strict=True))
                        upper[i][j] += projection
                        column = [a - projection * b for a, b in zip(column, basis, strict=True)]
                norm = math.sqrt(math.fsum(v * v for v in column))
                if norm < 1e-11:
                    break
                upper[j][j] = norm
                q.append([v / norm for v in column])
            if len(q) != size:
                continue
            solved = [math.fsum(a * b for a, b in zip(col, targets, strict=True)) for col in q]
            for i in reversed(range(size)):
                solved[i] = (
                    solved[i] - math.fsum(upper[i][j] * solved[j] for j in range(i + 1, size))
                ) / upper[i][i]
            coefficients = [0.0] * width
            for j, index in enumerate(active):
                coefficients[index] = solved[j] / scales[index]
            if any(v < -1e-8 for v in coefficients):
                continue
            coefficients = [max(0.0, v) for v in coefficients]
            error = math.fsum(
                (math.fsum(a * b for a, b in zip(row, coefficients, strict=True)) - y) ** 2
                for row, y in zip(vectors, targets, strict=True)
            )
            if error < best_error:
                best, best_error = coefficients, error
    if any(not math.isfinite(v) for v in best):
        raise ValueError("NNLS produced nonfinite coefficients")
    return tuple(best)


def conformal_rank(groups: int, alpha: float) -> int:
    if type(groups) is not int or groups < 1 or not math.isfinite(alpha) or not 0 < alpha < 1:
        raise ValueError("conformal rank needs groups > 0 and 0 < alpha < 1")
    return math.ceil((groups + 1) * (1 - Fraction(str(alpha))))


def conformal_quantile(scores: tuple[float, ...], alpha: float) -> float | None:
    if not scores or any(not math.isfinite(s) or s < 0 for s in scores):
        raise ValueError("calibration scores must be finite and nonnegative")
    rank = conformal_rank(len(scores), alpha)
    return sorted(scores)[rank - 1] if rank <= len(scores) else None


def point_prediction(coefficients: tuple[float, ...], vector: tuple[int, ...]) -> float:
    return math.fsum(c * v for c, v in zip(coefficients, vector, strict=True))


def joint_score(cu: int, data: int, predicted_cu: float, predicted_data: float) -> float:
    return max(
        0.0,
        (cu - predicted_cu) / max(predicted_cu, 1.0),
        (data - predicted_data) / max(predicted_data, 1.0),
    )


def formula_limit(base: float, correction: float, step: int) -> int:
    if not math.isfinite(base) or not math.isfinite(correction) or base < 0 or correction < 0:
        raise ValueError("formula limits require finite nonnegative values")
    if type(step) is not int or step < 1:
        raise ValueError("rounding step must be a positive integer")
    value = Decimal(str(base)) + Decimal(str(correction)) * max(Decimal(str(base)), Decimal(1))
    return max(step, int((value / step).to_integral_value(rounding=ROUND_CEILING)) * step)


class FormulaRecipient(StrictModel):
    recipient: str
    address: str
    exists: StrictBool


class FormulaInputs(StrictModel):
    """Application-owned state evidence sealed to the exact prepared transaction.

    Public derivation is recomputed, never accepted as caller-supplied numbers.
    The application still verifies account ownership, initialization and approval.
    """

    feature_version: Literal["cu-pilot-ata-derivation-effort-v1"] = FEATURE_VERSION
    prepared_identity: str = Field(min_length=1)
    bound_features_digest: str = Field(pattern=SHA256)
    state_digest: str = Field(pattern=SHA256)
    cell_key: str = Field(min_length=1)
    observed_slot: PortableSlot
    max_age_slots: PortableSlot
    mint: str
    recipients: tuple[FormulaRecipient, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len({r.recipient for r in self.recipients}) != len(self.recipients):
            raise ValueError("duplicate formula recipients")
        self.vector("count_missing_derivation")
        return self

    def vector(self, method: str) -> tuple[int, ...]:
        if method not in METHOD_TERMS:
            raise ValueError("unknown formula method")
        attempts, missing_attempts, missing = 0, 0, 0
        for recipient in self.recipients:
            address, bump = canonical_ata(
                recipient.recipient, self.mint, TOKEN_PROGRAM, ATA_PROGRAM
            )
            if address != recipient.address:
                raise ValueError("formula recipient address differs from canonical ATA")
            attempts += 256 - bump
            if not recipient.exists:
                missing += 1
                missing_attempts += 256 - bump
        values = (1, len(self.recipients), missing, attempts, missing_attempts)
        return values if method == "count_missing_derivation" else values[:3]


class FormulaPattern(StrictModel):
    train_count: StrictInt = Field(ge=1)
    qualification_count: StrictInt = Field(ge=0)
    qualification_joint_exceedances: StrictInt = Field(ge=0)
    qualification_upper_bound: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    numerical_ranges: dict[str, ResourceRange]
    instruction_data_length_ranges: tuple[ResourceRange, ...]
    program_ids: tuple[str, ...]
    version: Literal["legacy", 0, 1]
    vector_ranges: tuple[ResourceRange, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if set(self.numerical_ranges) != set(NUMERICAL_FEATURES):
            raise ValueError("formula support requires every numerical feature")
        if not self.program_ids or len(self.program_ids) != len(
            self.instruction_data_length_ranges
        ):
            raise ValueError("formula instruction support mismatch")
        if self.qualification_joint_exceedances > self.qualification_count:
            raise ValueError("qualification failures exceed groups")
        if self.qualification_count:
            expected = wilson_upper_bound(
                self.qualification_joint_exceedances, self.qualification_count
            )
            if self.qualification_upper_bound is None or not math.isclose(
                expected, self.qualification_upper_bound, rel_tol=1e-12
            ):
                raise ValueError("qualification bound does not match independent outcomes")
        elif self.qualification_upper_bound is not None:
            raise ValueError("empty qualification cannot claim an empirical bound")
        return self

    def risk(
        self, features: Features, vector: tuple[int, ...], policy: ResourcePolicy
    ) -> str | None:
        risk = feature_risk(features, policy)
        if risk:
            return risk
        if features.version != self.version or features.program_ids != self.program_ids:
            return "shape_mismatch"
        if len(features.instruction_data_lengths) != len(self.instruction_data_length_ranges):
            return "shape_mismatch"
        if any(
            not bound.contains(getattr(features, name))
            for name, bound in self.numerical_ranges.items()
        ) or any(
            not bound.contains(value)
            for bound, value in zip(
                self.instruction_data_length_ranges, features.instruction_data_lengths, strict=True
            )
        ):
            return "out_of_distribution"
        if len(vector) != len(self.vector_ranges) or any(
            not bound.contains(value)
            for bound, value in zip(self.vector_ranges, vector, strict=True)
        ):
            return "formula_out_of_support"
        return None


class FormulaArtifact(StrictModel):
    artifact_version: Literal["cu-pilot-joint-conformal-formula-v1"] = FORMULA_VERSION
    feature_version: Literal["cu-pilot-ata-derivation-effort-v1"] = FEATURE_VERSION
    method: Literal["count_missing", "count_missing_derivation"] = "count_missing_derivation"
    context: str = Field(min_length=1)
    source: Literal["historical", "simulation", "synthetic"]
    label_source: Literal["historical", "simulation", "synthetic"] | None = None
    evidence_origin: Literal[
        "synthetic", "local-runtime", "live-simulation", "historical-execution"
    ]
    cluster_identity: str = Field(min_length=1)
    runtime_identity: str = Field(min_length=1)
    deployment_bindings: dict[str, str] = Field(min_length=1)
    cell_key: str = Field(min_length=1)
    evidence_min_slot: PortableSlot
    max_slot: PortableSlot
    training_max_slot: PortableSlot
    calibration_min_slot: PortableSlot
    calibration_max_slot: PortableSlot
    qualification_min_slot: PortableSlot
    policy: ResourcePolicy
    alpha: float = Field(default=0.05, gt=0, lt=1, allow_inf_nan=False)
    compute_coefficients: tuple[float, ...]
    data_coefficients: tuple[float, ...]
    calibration_scores: tuple[float, ...] = Field(min_length=1)
    calibration_cohorts: tuple[str, ...] = Field(min_length=1)
    fit_cohorts: tuple[str, ...] = Field(min_length=1)
    qualification_cohorts: tuple[str, ...] = Field(min_length=1)
    correction: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    development_digest: str = Field(pattern=SHA256)
    patterns: dict[str, FormulaPattern] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not (
            self.evidence_min_slot
            <= self.training_max_slot
            < self.calibration_min_slot
            <= self.calibration_max_slot
            < self.qualification_min_slot
            <= self.max_slot
        ):
            raise ValueError(
                "formula fit/calibration/qualification must be chronological and distinct"
            )
        partitions = [self.fit_cohorts, self.calibration_cohorts, self.qualification_cohorts]
        if any(len(set(p)) != len(p) for p in partitions) or any(
            set(partitions[i]) & set(partitions[j]) for i in range(3) for j in range(i)
        ):
            raise ValueError("formula cohorts overlap or repeat")
        if len(self.calibration_scores) != len(self.calibration_cohorts):
            raise ValueError("one joint maximum score is required per calibration cohort")
        expected = conformal_quantile(self.calibration_scores, self.alpha)
        if self.correction != expected:
            raise ValueError("formula correction differs from the exact conformal order statistic")
        width = len(METHOD_TERMS[self.method])
        for coefficients in (self.compute_coefficients, self.data_coefficients):
            if len(coefficients) != width or any(
                not math.isfinite(v) or v < 0 for v in coefficients
            ):
                raise ValueError("formula coefficients must be finite nonnegative terms")
        if any(len(stats.vector_ranges) != width for stats in self.patterns.values()):
            raise ValueError("formula support width differs from model terms")
        if any(
            s.train_count > len(self.fit_cohorts)
            or s.qualification_count > len(self.qualification_cohorts)
            for s in self.patterns.values()
        ):
            raise ValueError("pattern evidence counts exceed independent cohorts")
        if self.evidence_origin == "synthetic" and self.source != "synthetic":
            raise ValueError("synthetic evidence must retain synthetic provenance")
        if self.source != "synthetic" and self.label_source not in (None, self.source):
            raise ValueError("formula source differs from label provenance")
        if self.policy.compute_margin_bps or self.policy.data_margin_bps:
            raise ValueError("formula margins are calibrated, not resource quantile margins")
        for program, fingerprint in self.deployment_bindings.items():
            Pubkey.from_string(program)
            if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
                raise ValueError("formula deployment identity must be SHA256")
        if any(
            not set(s.program_ids) <= set(self.deployment_bindings) for s in self.patterns.values()
        ):
            raise ValueError("formula deployment closure omits an instruction program")
        return self

    def qualified_for_activation(self) -> bool:
        return self.correction is not None and all(
            s.train_count >= self.policy.min_samples
            and s.qualification_count >= self.policy.min_calibration_samples
            and s.qualification_upper_bound is not None
            and s.qualification_upper_bound <= self.policy.max_joint_underestimation_rate
            for s in self.patterns.values()
        )

    def limits(self, vector: tuple[int, ...]) -> tuple[int, int]:
        if self.correction is None:
            raise ValueError("insufficient_conformal_cohorts")
        return (
            formula_limit(
                point_prediction(self.compute_coefficients, vector),
                self.correction,
                self.policy.compute_rounding,
            ),
            formula_limit(
                point_prediction(self.data_coefficients, vector),
                self.correction,
                self.policy.data_rounding,
            ),
        )


class FormulaEstimator:
    """State-bound dynamic predictor; constructing an unbound estimator never authorizes use."""

    def __init__(self, model: FormulaArtifact, inputs: FormulaInputs | None = None) -> None:
        # Frozen Pydantic models do not freeze nested dictionaries; isolate callers.
        self._model = FormulaArtifact.model_validate_json(model.model_dump_json())
        self.inputs = (
            FormulaInputs.model_validate_json(inputs.model_dump_json()) if inputs else None
        )

    @property
    def model(self) -> FormulaArtifact:
        return FormulaArtifact.model_validate_json(self._model.model_dump_json())

    def bind(self, inputs: FormulaInputs) -> Self:
        return type(self)(self._model, inputs)

    def validate_binding(
        self, prepared_identity: str, *, transaction: TransactionInput | None = None
    ) -> None:
        if self.inputs is None:
            raise ValueError("formula_state_unbound")
        if self.inputs.prepared_identity != prepared_identity:
            raise ValueError("formula_message_mismatch")
        if transaction is not None:
            # A shape digest cannot bind account identities. Verify the public
            # derivation inputs against authoritative decoded instruction keys.
            # Existence/initialization remains trusted application snapshot data
            # and must be refreshed by the application's final pre-sign guard.
            expected = {self.inputs.mint}
            expected.update(r.recipient for r in self.inputs.recipients)
            expected.update(r.address for r in self.inputs.recipients)
            accounts = transaction.accounts
            if not expected <= {account.pubkey for account in accounts}:
                raise ValueError("formula_accounts_mismatch")
            if not any(
                instruction.program_id != "ComputeBudget111111111111111111111111111111"
                and expected <= {accounts[index].pubkey for index in instruction.accounts}
                for instruction in transaction.instructions
            ):
                raise ValueError("formula_instruction_accounts_mismatch")

    def predict(self, features: Features, *, context: str, current_slot: int) -> Prediction:
        if type(current_slot) is not int or current_slot < 0:
            raise ValueError("current slot must be a nonnegative exact integer")
        model, inputs = self._model, self.inputs
        stats = model.patterns.get(features.pattern_id)
        reason: str | None = None
        limits: tuple[int, int] | None = None
        if context != model.context:
            reason = "context_mismatch"
        elif inputs is None:
            reason = "formula_state_unbound"
        elif current_slot < max(model.max_slot, inputs.observed_slot):
            reason = "backwards_slot"
        elif current_slot - inputs.observed_slot > inputs.max_age_slots:
            reason = "stale_state"
        elif inputs.cell_key != model.cell_key:
            reason = "formula_state_mismatch"
        elif inputs.bound_features_digest != features_digest(features):
            reason = "formula_features_mismatch"
        elif current_slot - model.max_slot > model.policy.max_age_slots:
            reason = "stale_pattern"
        elif stats is None:
            reason = "unknown_pattern"
        elif model.correction is None:
            reason = "insufficient_conformal_cohorts"
        elif not model.qualified_for_activation():
            reason = "formula_qualification_risk"
        else:
            vector = inputs.vector(model.method)
            reason = stats.risk(features, vector, model.policy)
            if reason is None:
                limits = model.limits(vector)
                for resource, value, cap in (
                    ("compute", limits[0], MAX_COMPUTE_UNITS),
                    ("loaded_accounts", limits[1], MAX_LOADED_ACCOUNT_BYTES),
                ):
                    if value >= cap:
                        reason = f"hard_{resource}_limit"
                        break
                    if value >= cap * model.policy.near_limit_ratio:
                        reason = f"near_{resource}_limit"
                        break
        return Prediction(
            pattern_id=features.pattern_id,
            compute_unit_limit=limits[0] if limits and reason is None else None,
            loaded_accounts_data_size_limit=limits[1] if limits and reason is None else None,
            simulation_recommended=reason is not None,
            reason=reason or "qualified_joint_conformal_formula",
            explanation=(
                "Fallback required: " + reason
                if reason
                else "Dynamic limits passed separate grouped qualification; local experimental "
                "evidence is not a production failure-rate guarantee."
            ),
            sample_count=stats.train_count if stats else 0,
            calibration_count=stats.qualification_count if stats else 0,
            calibration_underestimation_upper_bound=stats.qualification_upper_bound
            if stats
            else None,
        )
