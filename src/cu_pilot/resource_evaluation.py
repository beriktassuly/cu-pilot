"""Paired-resource holdout comparisons and observed full preparation latency.

No simulation is performed by this offline evaluator. Simulation-label replay is
counterfactual; historical executions are never replayed against current state.
"""

from __future__ import annotations

import hashlib
import math
import statistics
import time
from collections import Counter
from collections.abc import Callable
from typing import Any, Literal

from pydantic import Field, StrictInt, model_validator

from cu_pilot.data import unique_observations
from cu_pilot.estimator import empirical_quantile, wilson_upper_bound
from cu_pilot.resources import ResourceEstimator, ResourcePolicy, paired_label, rounded_limit
from cu_pilot.schemas import (
    MAX_COMPUTE_UNITS,
    MAX_LOADED_ACCOUNT_BYTES,
    Features,
    Observation,
    StrictModel,
)

Limits = tuple[int, int] | None
Eligibility = Callable[[Features, int], str | None]
CallerPolicy = Callable[[Features, int], Limits]


def configured_priority_fee(features: Features, compute_unit_limit: int) -> int | None:
    """Calculate configured lamports, not the charged base fee or landing outcome.

    Agave uses ceil(price * requested CU / 1e6), saturating to u64 for
    legacy/v0. v1 stores an absolute fee, independent of either resource limit.
    Ambiguous/invalid budget inputs remain unscored.
    """
    if type(compute_unit_limit) is not int or not 0 < compute_unit_limit <= MAX_COMPUTE_UNITS:
        raise ValueError("fee calculation requires a valid final compute limit")
    if {"invalid_compute_budget_instruction", "duplicate_compute_budget_instruction"}.intersection(
        features.risk_flags
    ):
        return None
    if features.version == 1:
        if features.requested_micro_lamports is not None:
            return None
        fee = features.requested_priority_fee_lamports
    else:
        if features.requested_priority_fee_lamports is not None:
            return None
        fee = features.requested_micro_lamports
    fee = 0 if fee is None else fee
    if type(fee) is not int or not 0 <= fee < 2**64:
        return None
    if features.version == 1:
        return fee
    return min(2**64 - 1, (fee * compute_unit_limit + 999_999) // 1_000_000)


def _fee_summary(rows: list[Observation], limits: list[Limits]) -> dict[str, Any]:
    by_version: dict[str, list[int]] = {}
    unscored = 0
    for row, limit in zip(rows, limits, strict=True):
        if limit is None:
            continue
        fee = configured_priority_fee(row.features, limit[0])
        if fee is None:
            unscored += 1
        else:
            by_version.setdefault(str(row.features.version), []).append(fee)
    return {
        "basis": "configured priority fees for proposed limits; not observed charged total fees",
        "unscored_proposals": unscored,
        "by_version": {
            version: {
                "count": len(values),
                "min_lamports": str(min(values)),
                "max_lamports": str(max(values)),
                "total_lamports": str(sum(values)),
            }
            for version, values in sorted(by_version.items())
        },
        "observed_fee_savings_lamports": None,
    }


class PreparationTrace(StrictModel):
    """One caller-measured wall-clock span around the complete preparation path."""

    observation_id: str = Field(min_length=1)
    evidence: Literal["synthetic", "local-runtime", "live-simulation", "historical-execution"]
    mode: Literal["shadow", "deployment"]
    collection_method: Literal["prospective", "offline-replay"] = "prospective"
    total_ms: float = Field(ge=0, allow_inf_nan=False)
    inference_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    state_read_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    artifact_refresh_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    logging_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    resource_estimation_calls: StrictInt = Field(ge=0)
    rpc_attempts: StrictInt = Field(default=0, ge=0)
    control_simulation_calls: StrictInt = Field(default=0, ge=0)
    preflight_calls: StrictInt = Field(default=0, ge=0)
    validation_simulation_calls: StrictInt = Field(default=0, ge=0)
    state_reads: StrictInt = Field(default=0, ge=0)
    artifact_refreshes: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> PreparationTrace:
        if any(
            value is not None and value > self.total_ms
            for value in (
                self.inference_ms,
                self.state_read_ms,
                self.artifact_refresh_ms,
                self.logging_ms,
            )
        ):
            raise ValueError("component duration exceeds the full preparation span")
        return self


def preparation_report(traces: list[PreparationTrace]) -> dict[str, Any]:
    by_id: dict[str, PreparationTrace] = {}
    for trace in traces:
        if trace.observation_id in by_id and trace != by_id[trace.observation_id]:
            raise ValueError("conflicting preparation traces")
        by_id[trace.observation_id] = trace
    rows = list(by_id.values())
    if len({(r.evidence, r.mode, r.collection_method) for r in rows}) > 1:
        raise ValueError("report each evidence source and mode separately")
    values = sorted(r.total_ms for r in rows)
    result: dict[str, Any] = {
        "measured_count": len(rows),
        "evidence": rows[0].evidence if rows else None,
        "mode": rows[0].mode if rows else None,
        "collection_method": rows[0].collection_method if rows else None,
        "mean_ms": statistics.mean(values) if values else None,
        "p50_ms": values[math.ceil(len(values) * 0.50) - 1] if values else None,
        "p95_ms": values[math.ceil(len(values) * 0.95) - 1] if values else None,
        "p99_ms": values[math.ceil(len(values) * 0.99) - 1] if values else None,
    }
    for field in (
        "resource_estimation_calls",
        "rpc_attempts",
        "control_simulation_calls",
        "preflight_calls",
        "validation_simulation_calls",
        "state_reads",
        "artifact_refreshes",
    ):
        result[field] = sum(getattr(r, field) for r in rows)
    for field in ("inference_ms", "state_read_ms", "artifact_refresh_ms", "logging_ms"):
        measured = [getattr(r, field) for r in rows if getattr(r, field) is not None]
        result["mean_" + field] = statistics.mean(measured) if measured else None
    return result


def _summary(rows: list[Observation], limits: list[Limits], controls: list[bool]) -> dict[str, Any]:
    accepted = sum(limit is not None for limit in limits)
    scored = [
        (r, limit) for r, limit in zip(rows, limits, strict=True) if limit and paired_label(r)
    ]
    cu_fail = data_fail = joint_fail = 0
    cu_excess: list[int] = []
    data_excess: list[int] = []
    for row, (cu, data) in scored:
        assert row.label.compute_units is not None and row.label.loaded_accounts_bytes is not None
        cu_bad, data_bad = row.label.compute_units > cu, row.label.loaded_accounts_bytes > data
        cu_fail += cu_bad
        data_fail += data_bad
        joint_fail += cu_bad or data_bad
        cu_excess.append(max(0, cu - row.label.compute_units))
        data_excess.append(max(0, data - row.label.loaded_accounts_bytes))
    return {
        "total": len(rows),
        "accepted": accepted,
        "fallback": len(rows) - accepted,
        "coverage": accepted / len(rows),
        "paired_scored": len(scored),
        "accepted_failed_or_unpaired": accepted - len(scored),
        "compute_underestimations": cu_fail,
        "data_underestimations": data_fail,
        "joint_underestimations": joint_fail,
        "compute_underestimation_rate": cu_fail / len(scored) if scored else None,
        "data_underestimation_rate": data_fail / len(scored) if scored else None,
        "joint_underestimation_rate": joint_fail / len(scored) if scored else None,
        "mean_excess_compute": statistics.mean(cu_excess) if scored else None,
        "mean_excess_data_bytes": statistics.mean(data_excess) if scored else None,
        "p95_excess_compute": empirical_quantile(cu_excess, 0.95) if scored else None,
        "p95_excess_data_bytes": empirical_quantile(data_excess, 0.95) if scored else None,
        "counterfactual_avoided_resource_estimation_calls": accepted,
        "counterfactual_control_simulation_calls": sum(controls),
        "counterfactual_net_calls_avoided": accepted - sum(controls),
        "measured_avoided_calls": None,
        "preflight_or_validation_calls_avoided": 0,
        "configured_priority_fees": _fee_summary(rows, limits),
    }


def _select(record_id: str, seed: str, probability: float) -> bool:
    value = int.from_bytes(hashlib.sha256((seed + ":" + record_id).encode()).digest()[:8], "big")
    return value < int(probability * 2**64)


def _valid_limits(limits: Limits) -> Limits:
    if limits is not None and (
        len(limits) != 2
        or any(type(n) is not int for n in limits)
        or not 0 < limits[0] <= MAX_COMPUTE_UNITS
        or not 0 < limits[1] <= MAX_LOADED_ACCOUNT_BYTES
    ):
        raise ValueError("baseline limits must be positive exact integers within resource caps")
    return limits


def evaluate_resources(
    observations: list[Observation],
    *,
    policy: ResourcePolicy | None = None,
    test_fraction: float = 0.2,
    fixed_limits: Limits = (MAX_COMPUTE_UNITS, MAX_LOADED_ACCOUNT_BYTES),
    caller_policy: CallerPolicy | None = None,
    lifecycle_eligibility: Eligibility | None = None,
    control_probability: float = 0.05,
    control_seed: str = "evaluation-v1",
    cache_refresh_slots: int = 100,
    preparation_traces: list[PreparationTrace] | None = None,
) -> dict[str, Any]:
    if not 0 < test_fraction < 1 or not 0 <= control_probability <= 1:
        raise ValueError("invalid test fraction or control probability")
    if type(cache_refresh_slots) is not int or cache_refresh_slots < 1:
        raise ValueError("cache refresh interval must be positive")
    policy = policy or ResourcePolicy()
    fixed_limits = _valid_limits(fixed_limits)
    rows = unique_observations(observations)
    if (
        not rows
        or len({r.context for r in rows}) != 1
        or len({r.source for r in rows}) != 1
        or len({r.label_source or r.source for r in rows}) != 1
        or len({r.evidence_origin for r in rows}) != 1
    ):
        raise ValueError("evaluation requires one context and one label provenance")
    windows = sorted({r.slot // policy.independence_window_slots for r in rows})
    if len(windows) < 3:
        raise ValueError("need at least three evidence windows for fit/calibration/test")
    boundary = windows[min(len(windows) - 1, max(2, int(len(windows) * (1 - test_fraction))))]
    development = [r for r in rows if r.slot // policy.independence_window_slots < boundary]
    test = [r for r in rows if r.slot // policy.independence_window_slots >= boundary]
    estimator = ResourceEstimator.fit(development, policy)
    fit_rows = [
        r for r in development if r.slot <= estimator.model.training_max_slot and paired_label(r)
    ]
    models = {
        "pattern_p95_ungated": ResourceEstimator.fit(
            development, policy.model_copy(update={"quantile": 0.95})
        ),
        "pattern_p99_ungated": ResourceEstimator.fit(
            development, policy.model_copy(update={"quantile": 0.99})
        ),
    }
    methods: dict[str, list[Limits]] = {
        "always_simulate": [None] * len(test),
        "fixed_operation_limits": [fixed_limits] * len(test),
    }
    for name, model in models.items():
        method: list[Limits] = []
        for row in test:
            stats = model.model.patterns.get(row.features.pattern_id)
            proposal = (
                (stats.compute_unit_limit, stats.loaded_accounts_data_size_limit) if stats else None
            )
            method.append(
                proposal
                if proposal
                and proposal[0] <= MAX_COMPUTE_UNITS
                and proposal[1] <= MAX_LOADED_ACCOUNT_BYTES
                else None
            )
        methods[name] = method
    cache: dict[str, tuple[int, tuple[int, int]]] = {}
    for row in fit_rows:
        assert row.label.compute_units is not None and row.label.loaded_accounts_bytes is not None
        cache[row.features.pattern_id] = (
            row.slot,
            (row.label.compute_units, row.label.loaded_accounts_bytes),
        )
    cache_limits: list[Limits] = []
    # Only refresh after a fallback measurement; accepted cache hits do not get
    # free labels. Defer updates until the next slot to prevent same-slot leakage.
    pending: list[Observation] = []
    last_slot = -1
    for row in test:
        if row.slot != last_slot:
            for previous in pending:
                if paired_label(previous):
                    assert (
                        previous.label.compute_units is not None
                        and previous.label.loaded_accounts_bytes is not None
                    )
                    cache[previous.features.pattern_id] = (
                        previous.slot,
                        (previous.label.compute_units, previous.label.loaded_accounts_bytes),
                    )
            pending = []
            last_slot = row.slot
        entry = cache.get(row.features.pattern_id)
        limits = None
        if entry and row.slot - entry[0] <= cache_refresh_slots:
            limits = (
                rounded_limit(entry[1][0], policy.compute_margin_bps, policy.compute_rounding),
                rounded_limit(entry[1][1], policy.data_margin_bps, policy.data_rounding),
            )
            if limits[0] > MAX_COMPUTE_UNITS or limits[1] > MAX_LOADED_ACCOUNT_BYTES:
                limits = None
        if limits is None:
            pending.append(row)
        cache_limits.append(limits)
    methods["last_successful_cache"] = cache_limits
    if caller_policy:
        methods["caller_policy"] = [_valid_limits(caller_policy(r.features, r.slot)) for r in test]
    started = time.perf_counter()
    predictions = [
        estimator.predict(r.features, context=r.context, current_slot=r.slot) for r in test
    ]
    inference_ms = (time.perf_counter() - started) * 1000 / len(test)
    statistical = [
        (p.compute_unit_limit, p.loaded_accounts_data_size_limit)
        if p.compute_unit_limit is not None
        and p.loaded_accounts_data_size_limit is not None
        and not p.simulation_recommended
        else None
        for p in predictions
    ]
    methods["joint_statistical_policy"] = statistical
    complete: list[Limits] = []
    controls: list[bool] = []
    reasons: list[str] = []
    suspended: set[str] = set()
    pending_suspensions: set[str] = set()
    last_slot = -1
    for row, proposal, prediction in zip(test, statistical, predictions, strict=True):
        if row.slot != last_slot:
            suspended.update(pending_suspensions)
            pending_suspensions = set()
            last_slot = row.slot
        reason = (
            lifecycle_eligibility(row.features, row.slot)
            if lifecycle_eligibility
            else "lifecycle_evidence_unavailable"
        )
        if row.features.pattern_id in suspended:
            reason = "control_excess_suspended"
        accepted = proposal if reason is None else None
        selected = accepted is not None and _select(
            row.record_id, control_seed, control_probability
        )
        complete.append(accepted)
        controls.append(selected)
        reasons.append(reason or prediction.reason)
        if selected and accepted and paired_label(row):
            assert (
                row.label.compute_units is not None and row.label.loaded_accounts_bytes is not None
            )
            if (
                row.label.compute_units > accepted[0]
                or row.label.loaded_accounts_bytes > accepted[1]
            ):
                pending_suspensions.add(row.features.pattern_id)
    methods["complete_policy"] = complete
    no_controls = [False] * len(test)
    summaries = {
        name: _summary(test, limits, controls if name == "complete_policy" else no_controls)
        for name, limits in methods.items()
    }
    per_pattern = []
    for pattern in sorted({r.features.pattern_id for r in test}):
        indexes = [i for i, r in enumerate(test) if r.features.pattern_id == pattern]
        stats = estimator.model.patterns.get(pattern)
        group = [test[i] for i in indexes]
        per_pattern.append(
            {
                "pattern_id": pattern,
                "training_windows": stats.train_count if stats else 0,
                "calibration_windows": stats.calibration_count if stats else 0,
                "calibration_joint_upper_bound": stats.calibration_upper_bound if stats else None,
                "metrics": _summary(
                    group, [complete[i] for i in indexes], [controls[i] for i in indexes]
                ),
                "reasons": dict(Counter(reasons[i] for i in indexes)),
            }
        )
    selected_pairs = [
        (row, proposal)
        for row, proposal, selected in zip(test, complete, controls, strict=True)
        if selected and proposal and paired_label(row)
    ]
    control_failures = 0
    for row, proposal in selected_pairs:
        assert row.label.compute_units is not None and row.label.loaded_accounts_bytes is not None
        control_failures += (
            row.label.compute_units > proposal[0] or row.label.loaded_accounts_bytes > proposal[1]
        )
    traces = preparation_traces or []
    if any(t.observation_id not in {r.record_id for r in test} for t in traces):
        raise ValueError("preparation trace does not belong to the frozen test partition")
    return {
        "report_version": "cu-pilot-resource-evaluation-v1",
        "source": rows[0].source,
        "label_source": rows[0].label_source or rows[0].source,
        "evidence_origin": rows[0].evidence_origin,
        "context": rows[0].context,
        "input_count": len(observations),
        "unique_count": len(rows),
        "development_count": len(development),
        "test_count": len(test),
        "training_max_slot": str(estimator.model.training_max_slot),
        "calibration_min_slot": str(estimator.model.calibration_min_slot),
        "test_min_slot": str(min(r.slot for r in test)),
        "test_paired_count": sum(paired_label(r) for r in test),
        "test_missing_data_count": sum(r.label.loaded_accounts_bytes is None for r in test),
        "policy": policy.model_dump(mode="json"),
        "methods": summaries,
        "patterns": per_pattern,
        "fallback_reasons": dict(
            Counter(
                reason
                for reason, proposal in zip(reasons, complete, strict=True)
                if proposal is None
            )
        ),
        "measured_statistical_inference_ms_per_row": inference_ms,
        "measured_preparation": preparation_report(traces),
        "control_sampling": {
            "probability": control_probability,
            "seed": control_seed,
            "selected": sum(controls),
            "paired_scored": len(selected_pairs),
            "joint_exceedances": control_failures,
            "descriptive_upper_bound": wilson_upper_bound(control_failures, len(selected_pairs))
            if selected_pairs
            else None,
            "population": "eligible predictions before outcome; suspension changes eligibility",
        },
        "cache_refresh_slots": cache_refresh_slots,
        "caller_policy_available": caller_policy is not None,
        "lifecycle_evidence_available": lifecycle_eligibility is not None,
        "limitations": [
            "Call savings are counterfactual. Offline replay performs no RPC calls.",
            "No latency percentiles are inferred from model time or coverage.",
            "Timing includes builder, state, checks, artifact refresh, simulation and logging.",
            "Historical and simulation labels are separate; neither proves future success.",
            "Controls are selected eligible predictions, not fully observed deployment traffic.",
            "Window maxima reduce burst duplication; windows are not guaranteed independent.",
            "Fixed, cache and ungated baselines do not establish acceptable joint risk.",
            "Charged fees and landing are unmeasured; configured priority fees are calculated.",
            "Unchanged v1 absolute priority fees do not fall with CU or data limits.",
            "No preflight or indispensable business-validation simulation is counted as removable.",
        ],
    }
