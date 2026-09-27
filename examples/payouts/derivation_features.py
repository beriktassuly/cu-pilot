"""Offline experiment for public ATA derivation effort; never release authority.

Run ``python -m examples.payouts.derivation_features DATA --output NEW_DIRECTORY``.
The application never loads this experimental feature or formula format.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from cu_pilot.data import read_jsonl
from cu_pilot.estimator import empirical_quantile, wilson_upper_bound
from cu_pilot.resources import limit_risk, paired_label, rounded_limit, support_risk
from cu_pilot.schemas import StrictModel
from examples.payouts.model import (
    ATA_PROGRAM,
    LOCAL_POLICY,
    TOKEN_PROGRAM,
    PayoutModelBundle,
    PayoutObservation,
    PayoutStateEnvelope,
    canonical_ata,
    canonical_digest,
    fit_bundle,
    unique_rows,
)

FEATURE_VERSION = "cu-pilot-ata-derivation-effort-v1"
ARTIFACT_VERSION = "cu-pilot-experimental-derivation-formulas-v1"
METHODS = {
    "count_missing": ("intercept", "count", "missing"),
    "count_missing_derivation": (
        "intercept",
        "count",
        "missing",
        "total_ata_attempts",
        "missing_ata_attempts",
    ),
}


class DerivationFeatures(StrictModel):
    feature_version: Literal["cu-pilot-ata-derivation-effort-v1"] = FEATURE_VERSION
    count: StrictInt = Field(ge=1, le=8)
    missing: StrictInt = Field(ge=0, le=8)
    total_ata_attempts: StrictInt = Field(ge=1, le=2048)
    missing_ata_attempts: StrictInt = Field(ge=0, le=2048)
    ata_bumps: tuple[StrictInt, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len(self.ata_bumps) != self.count or any(not 0 <= b <= 255 for b in self.ata_bumps):
            raise ValueError("one valid ATA bump is required per recipient")
        if self.total_ata_attempts != sum(256 - bump for bump in self.ata_bumps):
            raise ValueError("total attempts do not match public ATA bumps")
        if not 0 <= self.missing <= self.count:
            raise ValueError("missing account count exceeds candidate count")
        if (
            not self.missing
            <= self.missing_ata_attempts
            <= min(256 * self.missing, self.total_ata_attempts)
        ):
            raise ValueError("missing ATA attempts are inconsistent")
        if self.missing == self.count and self.missing_ata_attempts != self.total_ata_attempts:
            raise ValueError("all missing accounts must include all ATA attempts")
        return self

    def vector(self, method: str) -> tuple[int, ...]:
        values = {"intercept": 1, **self.model_dump()}
        return tuple(values[name] for name in METHODS[method])


def derive_features(state: PayoutStateEnvelope) -> DerivationFeatures:
    """Use only public recipient, mint and pre-execution account-presence fields.

    Pinned @solana/addresses 8.3.0 starts at bump 255 and decrements one on
    on-curve failures, so a successful bump requires 256-bump attempts. This
    counts one ATA search per recipient, not every on-chain derivation invocation.
    """
    bumps = []
    missing_attempts = 0
    for account in state.recipient_accounts:
        address, bump = canonical_ata(account.recipient, state.mint, TOKEN_PROGRAM, ATA_PROGRAM)
        if address != account.address:
            raise ValueError("derived ATA differs from pre-execution account evidence")
        bumps.append(bump)
        if not account.exists:
            missing_attempts += 256 - bump
    return DerivationFeatures(
        count=state.candidate_count,
        missing=state.missing_atas,
        total_ata_attempts=sum(256 - bump for bump in bumps),
        missing_ata_attempts=missing_attempts,
        ata_bumps=tuple(bumps),
    )


def _cohorts(rows: list[PayoutObservation]) -> dict[str, int]:
    """Mirror grouped_split's queue/snapshot joins and overlapping slot spans."""
    parents: dict[str, str] = {}

    def root(key: str) -> str:
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = root(parents[key])
        return parents[key]

    for row in rows:
        keys = (
            "g:" + row.queue_group,
            "q:" + row.state.queue_address,
            "s:" + row.state.snapshot_digest,
        )
        for key in keys[1:]:
            parents[root(key)] = root(keys[0])
    grouped: dict[str, list[PayoutObservation]] = defaultdict(list)
    for row in rows:
        grouped[root("g:" + row.queue_group)].append(row)
    window = LOCAL_POLICY.independence_window_slots
    spans = sorted(
        (
            min(r.observation.slot // window for r in group),
            max(r.observation.slot // window for r in group),
            key,
            group,
        )
        for key, group in grouped.items()
    )
    result = {}
    cohort, last_end = -1, -1
    for first, last, _, group in spans:
        if first > last_end:
            cohort += 1
        last_end = max(last_end, last)
        for row in group:
            result[row.record_id] = cohort
    return result


def _solve(vectors: list[tuple[int, ...]], targets: list[int]) -> list[float]:
    """Small ordinary least squares, with the existing baseline's nonnegative clipping."""
    width = len(vectors[0])
    matrix = [
        [sum(float(v[i]) * v[j] for v in vectors) for j in range(width)]
        + [sum(float(v[i]) * y for v, y in zip(vectors, targets, strict=True))]
        for i in range(width)
    ]
    for column in range(width):
        pivot = max(range(column, width), key=lambda i: abs(matrix[i][column]))
        if abs(matrix[pivot][column]) < 1e-9:
            raise ValueError("fitting data lacks independent feature variation")
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        divisor = matrix[column][column]
        matrix[column] = [value / divisor for value in matrix[column]]
        for row in range(width):
            if row != column:
                multiplier = matrix[row][column]
                matrix[row] = [
                    a - multiplier * b for a, b in zip(matrix[row], matrix[column], strict=True)
                ]
    return [max(0.0, matrix[i][-1]) for i in range(width)]


def _cell(row: PayoutObservation) -> str:
    return canonical_digest([row.state.state_key, row.observation.features.pattern_id])


def _scope_reason(bundle: PayoutModelBundle, row: PayoutObservation) -> str | None:
    state, observation = row.state, row.observation
    reason = state.risk(
        current_slot=observation.slot, deployment_bindings=bundle.deployment_bindings
    )
    if reason:
        return reason
    if (
        state.cluster_identity != bundle.cluster_identity
        or state.runtime_identity != bundle.runtime_identity
        or observation.context != bundle.context
    ):
        return "runtime_or_context_mismatch"
    model = bundle.models.get(state.state_key)
    if model is None:
        return "unsupported_state_cell"
    stats = model.patterns.get(observation.features.pattern_id)
    if stats is None:
        return "unknown_pattern"
    return support_risk(observation.features, stats, LOCAL_POLICY)


def _proposal(bundle, formula, row, derived):
    reason = _scope_reason(bundle, row)
    if reason:
        return None, None, reason
    if formula.get("unfitted_reason"):
        return None, None, formula["unfitted_reason"]
    if derived is None:
        return None, None, "invalid_derivation_evidence"
    vector = derived.vector(formula["method"])
    if any(
        not low <= value <= high
        for value, (low, high) in zip(vector, formula["feature_ranges"], strict=True)
    ):
        return None, None, "derivation_features_out_of_distribution"
    cell = _cell(row)
    support = formula["fitting_support"].get(cell)
    if support is None or support["cohorts"] < LOCAL_POLICY.min_samples:
        return None, None, "insufficient_fitting_cohorts"
    if row.observation.slot < support["max_slot"]:
        return None, None, "backwards_evidence_slot"
    if row.observation.slot - support["max_slot"] > LOCAL_POLICY.max_age_slots:
        return None, None, "stale_fitting_evidence"
    estimate = math.ceil(sum(c * x for c, x in zip(formula["coefficients"], vector, strict=True)))
    compute = rounded_limit(
        max(0, estimate + formula["upper_fitting_residual"]),
        LOCAL_POLICY.compute_margin_bps,
        LOCAL_POLICY.compute_rounding,
    )
    data = formula["loaded_accounts_data_size_limit"]
    stats = bundle.models[row.state.state_key].patterns[row.observation.features.pattern_id]
    reason = limit_risk(
        stats.model_copy(
            update={"compute_unit_limit": compute, "loaded_accounts_data_size_limit": data}
        ),
        LOCAL_POLICY,
    )
    return compute, data, reason


def _distribution(values):
    values = sorted(values)
    return {
        "n": len(values),
        "mean": statistics.mean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p95_nearest_rank": values[math.ceil(len(values) * 0.95) - 1] if values else None,
    }


def run_experiment(
    observations: list[PayoutObservation],
) -> tuple[dict, dict, list[dict], list[dict]]:
    """Fit on the frozen first partition; only calibrate on the second and score the last."""
    rows = unique_rows(observations)
    bundle = fit_bundle(rows, policy=LOCAL_POLICY)
    cohorts = _cohorts(rows)
    partitions = {
        name: set(getattr(bundle.split, name + "_ids"))
        for name in ("fit", "calibration", "holdout")
    }
    cohort_partitions = {
        name: {cohorts[identifier] for identifier in ids} for name, ids in partitions.items()
    }
    recipient_partitions = {
        name: {
            account.recipient
            for row in rows
            if row.record_id in ids
            for account in row.state.recipient_accounts
        }
        for name, ids in partitions.items()
    }
    overlap = {
        f"{first}/{second}": {
            "cohorts": len(cohort_partitions[first] & cohort_partitions[second]),
            "recipients": len(recipient_partitions[first] & recipient_partitions[second]),
        }
        for first, second in (
            ("fit", "calibration"),
            ("fit", "holdout"),
            ("calibration", "holdout"),
        )
    }
    if any(values["cohorts"] or values["recipients"] for values in overlap.values()):
        raise ValueError("fitting, calibration and holdout require disjoint cohorts and recipients")
    if len(set(cohorts.values())) != sum(
        (bundle.split.fit_groups, bundle.split.calibration_groups, bundle.split.holdout_groups)
    ):
        raise ValueError("cohort calculation disagrees with frozen split")
    for row in rows:
        if (
            row.state.cluster_identity != bundle.cluster_identity
            or row.state.runtime_identity != bundle.runtime_identity
            or row.state.deployment_bindings != bundle.deployment_bindings
            or row.observation.context != bundle.context
            or row.observation.source != bundle.pattern_p99.source
            or (row.observation.label_source or row.observation.source)
            != (bundle.pattern_p99.label_source or bundle.pattern_p99.source)
            or row.observation.evidence_origin != bundle.pattern_p99.evidence_origin
        ):
            raise ValueError("evaluate one original environment and evidence origin")
    started = time.perf_counter()
    derived, derived_rows = {}, []
    for row in rows:
        try:
            value = derive_features(row.state)
            reason = None
        except ValueError as exc:
            value, reason = None, str(exc)
        derived[row.record_id] = value
        derived_rows.append(
            {
                "record_id": row.record_id,
                "cohort": cohorts[row.record_id],
                "state_snapshot_digest": row.state.snapshot_digest,
                "features": value.model_dump() if value else None,
                "error": reason,
            }
        )
    feature_seconds = time.perf_counter() - started
    fitting = [
        r
        for r in rows
        if r.record_id in partitions["fit"]
        and _scope_reason(bundle, r) is None
        and derived[r.record_id] is not None
        and paired_label(r.observation)
    ]
    if not fitting:
        raise ValueError("no supported paired fitting observations")
    grouped_fit = defaultdict(list)
    for row in fitting:
        grouped_fit[_cell(row)].append(row)
    support = {
        key: {
            "state_key": group[0].state.state_key,
            "pattern_id": group[0].observation.features.pattern_id,
            "cohorts": len({cohorts[r.record_id] for r in group}),
            "max_slot": max(r.observation.slot for r in group),
        }
        for key, group in grouped_fit.items()
    }
    by_count = defaultdict(list)
    for row in fitting:
        by_count[row.state.candidate_count].append(row.observation.label.loaded_accounts_bytes)
    data_limit = max(
        rounded_limit(
            empirical_quantile(values, LOCAL_POLICY.quantile),
            LOCAL_POLICY.data_margin_bps,
            LOCAL_POLICY.data_rounding,
        )
        for values in by_count.values()
    )
    methods = {}
    for method in METHODS:
        vectors = [derived[r.record_id].vector(method) for r in fitting]
        targets = [r.observation.label.compute_units for r in fitting]
        formula = {
            "method": method,
            "feature_names": list(METHODS[method]),
            "fitting_support": support,
            "loaded_accounts_data_size_limit": data_limit,
            "feature_ranges": [
                [min(v[i] for v in vectors), max(v[i] for v in vectors)]
                for i in range(len(METHODS[method]))
            ],
        }
        try:
            coefficients = _solve(vectors, targets)
            formula["coefficients"] = coefficients
            formula["upper_fitting_residual"] = max(
                0,
                max(
                    math.ceil(y - sum(c * x for c, x in zip(coefficients, v, strict=True)))
                    for v, y in zip(vectors, targets, strict=True)
                ),
            )
        except ValueError as exc:
            formula["unfitted_reason"] = str(exc)
        windows: dict[str, dict[int, bool | None]] = defaultdict(dict)
        excluded = Counter()
        for row in rows:
            if row.record_id not in partitions["calibration"]:
                continue
            compute, data, reason = _proposal(bundle, formula, row, derived[row.record_id])
            if reason:
                excluded[reason] += 1
                continue
            cohort = cohorts[row.record_id]
            cell = windows[_cell(row)]
            prior = cell.get(cohort, False)
            if not paired_label(row.observation):
                cell[cohort] = True if prior is True else None
            else:
                exceeded = (
                    row.observation.label.compute_units > compute
                    or row.observation.label.loaded_accounts_bytes > data
                )
                cell[cohort] = True if exceeded or prior is True else prior
        calibration = {}
        for key in support:
            values = list(windows[key].values())
            known = [value for value in values if value is not None]
            failures = sum(known)
            bound = wilson_upper_bound(failures, len(known)) if known else None
            calibration[key] = {
                "eligible_paired_cohorts": len(known),
                "unknown_cohorts": sum(value is None for value in values),
                "joint_exceeding_cohorts": failures,
                "joint_upper_bound": bound,
                "qualified": len(known) >= LOCAL_POLICY.min_calibration_samples
                and bound is not None
                and bound <= LOCAL_POLICY.max_joint_underestimation_rate,
            }
        formula["calibration"] = calibration
        formula["calibration_exclusions"] = dict(excluded)
        methods[method] = formula
    trials = []
    for row in rows:
        if row.record_id not in partitions["holdout"]:
            continue
        trial = {
            "record_id": row.record_id,
            "queue_group": row.queue_group,
            "cohort": cohorts[row.record_id],
            "state_key": row.state.state_key,
            "label": row.observation.label.model_dump(mode="json"),
            "methods": {},
        }
        for method, formula in methods.items():
            compute, data, reason = _proposal(bundle, formula, row, derived[row.record_id])
            calibration = formula["calibration"].get(_cell(row))
            if reason is None and (calibration is None or not calibration["qualified"]):
                reason = (
                    "insufficient_calibration_cohorts"
                    if (
                        calibration is None
                        or calibration["eligible_paired_cohorts"]
                        < LOCAL_POLICY.min_calibration_samples
                    )
                    else "joint_calibration_risk"
                )
            paired = paired_label(row.observation)
            trial["methods"][method] = {
                "accepted": reason is None,
                "reason": reason,
                "proposed_compute_limit": compute,
                "proposed_loaded_data_limit": data,
                "paired_label": paired,
                "compute_exceeded": row.observation.label.compute_units > compute
                if reason is None and paired
                else None,
                "data_exceeded": row.observation.label.loaded_accounts_bytes > data
                if reason is None and paired
                else None,
                "compute_over_allocation": max(0, compute - row.observation.label.compute_units)
                if reason is None and paired
                else None,
            }
        trials.append(trial)
    reports = {}
    for method in methods:
        decisions = [r["methods"][method] for r in trials]
        accepted = [d for d in decisions if d["accepted"]]
        paired = [d for d in accepted if d["paired_label"]]
        reports[method] = {
            "holdout_rows": len(trials),
            "accepted": len(accepted),
            "paired_accepted": len(paired),
            "failed_or_unpaired_accepted": len(accepted) - len(paired),
            "fallback_reasons": dict(Counter(d["reason"] for d in decisions if not d["accepted"])),
            "compute_exceedances": sum(d["compute_exceeded"] for d in paired),
            "data_exceedances": sum(d["data_exceeded"] for d in paired),
            "joint_exceedances": sum(d["compute_exceeded"] or d["data_exceeded"] for d in paired),
            "compute_over_allocation": _distribution(
                [d["compute_over_allocation"] for d in paired]
            ),
            "qualified_cells": sum(c["qualified"] for c in methods[method]["calibration"].values()),
        }
    matched = [
        r for r in trials if all(d["accepted"] and d["paired_label"] for d in r["methods"].values())
    ]
    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "feature_version": FEATURE_VERSION,
        "release_authority": False,
        "use": "offline_research_only",
        "source_observations_digest": canonical_digest([r.model_dump(mode="json") for r in rows]),
        "split": bundle.split.model_dump(mode="json"),
        "policy": LOCAL_POLICY.model_dump(mode="json"),
        "methods": methods,
        "resource_scope_bundle": bundle.model_dump(mode="json"),
    }
    report = {
        "report_version": "cu-pilot-derivation-research-report-v1",
        "artifact_digest": canonical_digest(artifact),
        "rows": len(rows),
        "duplicates_removed": len(observations) - len(rows),
        "split_counts": {name: len(ids) for name, ids in partitions.items()},
        "split_cohorts": {name: len(ids) for name, ids in cohort_partitions.items()},
        "cross_partition_overlap": overlap,
        "partition_labels": {
            name: {
                "rows": len(ids),
                "paired_success": sum(
                    paired_label(r.observation) for r in rows if r.record_id in ids
                ),
                "failed": sum(not r.observation.label.success for r in rows if r.record_id in ids),
                "missing_compute": sum(
                    r.observation.label.compute_units is None for r in rows if r.record_id in ids
                ),
                "missing_loaded_data": sum(
                    r.observation.label.loaded_accounts_bytes is None
                    for r in rows
                    if r.record_id in ids
                ),
            }
            for name, ids in partitions.items()
        },
        "fitting_paired_supported": len(fitting),
        "failed_labels": sum(not r.observation.label.success for r in rows),
        "missing_paired_labels": sum(not paired_label(r.observation) for r in rows),
        "feature_extraction_seconds": feature_seconds,
        "methods": reports,
        "matched_accepted_paired_rows": len(matched),
        "matched_exceedances": {
            method: {
                "paired_denominator": len(matched),
                "compute": sum(r["methods"][method]["compute_exceeded"] for r in matched),
                "loaded_data": sum(r["methods"][method]["data_exceeded"] for r in matched),
                "joint": sum(
                    r["methods"][method]["compute_exceeded"]
                    or r["methods"][method]["data_exceeded"]
                    for r in matched
                ),
            }
            for method in methods
        },
        "matched_compute_over_allocation": {
            method: _distribution(
                [r["methods"][method]["compute_over_allocation"] for r in matched]
            )
            for method in methods
        },
        "limitations": [
            "No release eligibility, payout speed, RPC savings or fee savings is established.",
            "A new local bank and held-out cohorts do not establish production risk guarantees.",
            "ATA attempts describe one search per recipient, not all runtime operations.",
            "Fit residuals, margins and policy thresholds are fixed before holdout labels.",
            "Calibration counts queue/snapshot/slot cohorts within each exact state/pattern cell.",
            "Missing or failed calibration labels do not count as successful demand evidence.",
        ],
    }
    return artifact, report, trials, derived_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evidence-status",
        choices=("exploratory", "fresh-prospective"),
        default="exploratory",
        help="Operator declaration; freshness is not inferred from a filename",
    )
    parser.add_argument(
        "--plan", type=Path, help="Required frozen feature-plan.json for prospective evaluation"
    )
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("preserving existing output directory")
    rows = [PayoutObservation.model_validate(r) for r in read_jsonl(args.dataset)]
    module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if args.evidence_status == "fresh-prospective":
        if args.plan is None:
            raise ValueError("prospective evaluation requires the pre-collection frozen plan")
        plan = json.loads(args.plan.read_text())
        if plan["module_sha256"] != module_sha256:
            raise ValueError("experimental module changed after plan freeze")
        if (
            len(rows) != 3520
            or len(unique_rows(rows)) != 3520
            or len(set(_cohorts(rows).values())) != 80
        ):
            raise ValueError(
                "prospective plan requires 3520 unique observations in 80 independent cohorts"
            )
        if any(
            row.observation.source != "simulation"
            or row.observation.evidence_origin != "local-runtime"
            or (row.observation.label_source or row.observation.source) != "simulation"
            for row in rows
        ):
            raise ValueError(
                "prospective local experiment requires original local simulation evidence"
            )
    started = time.perf_counter()
    artifact, report, trials, features = run_experiment(rows)
    checkout = Path(__file__).resolve().parents[2]
    sdk = checkout / "typescript/node_modules/@solana/addresses"
    proof = sdk / "src/program-derived-address.ts"
    report["sdk_algorithm_evidence"] = {
        "package": "@solana/addresses",
        "version": json.loads((sdk / "package.json").read_text())["version"],
        "source_path": str(proof),
        "source_sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
        "algorithm": "Start at 255; decrement on curve rejection; return first successful bump.",
        "successful_search_attempts": "256 - bump",
        "payout_source": "programs/payout_queue/src/lib.rs:94 and :445",
    }
    report.update(
        dataset_path=str(args.dataset.resolve()),
        dataset_sha256=hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        module_sha256=module_sha256,
        plan_path=str(args.plan.resolve()) if args.plan else None,
        plan_sha256=hashlib.sha256(args.plan.read_bytes()).hexdigest() if args.plan else None,
        evidence_status=args.evidence_status,
        created_at=datetime.now(UTC).isoformat(),
        total_experiment_seconds=time.perf_counter() - started,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in (("formula-artifact.json", artifact), ("evaluation.json", report)):
        with (args.output / name).open("x") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
    for name, values in (
        ("holdout-predictions.jsonl", trials),
        ("derived-features.jsonl", features),
    ):
        with (args.output / name).open("x") as stream:
            for value in values:
                stream.write(json.dumps(value, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "evidence_status": args.evidence_status,
                "methods": report["methods"],
                "matched_rows": report["matched_accepted_paired_rows"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
