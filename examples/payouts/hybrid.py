"""Fit and qualify experimental payout formulas with simulation fallback.

The last development partition is qualification, not a held-out comparison.
Use newly collected queues after freezing this bundle for final comparisons.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from cu_pilot.data import read_jsonl
from cu_pilot.estimator import NUMERICAL_FEATURES, wilson_upper_bound
from cu_pilot.formula import (
    METHOD_TERMS,
    FormulaArtifact,
    FormulaEstimator,
    FormulaInputs,
    FormulaPattern,
    FormulaRecipient,
    conformal_quantile,
    features_digest,
    formula_limit,
    joint_score,
    nonnegative_least_squares,
    point_prediction,
)
from cu_pilot.lifecycle import DeploymentEvidence, ProfileManifest, ProfileRegistry, artifact_digest
from cu_pilot.resources import (
    PortableSlot,
    ResourcePolicy,
    ResourceRange,
    feature_risk,
    paired_label,
)
from cu_pilot.schemas import Features, Prediction, StrictModel
from examples.payouts.derivation_features import derive_features
from examples.payouts.model import (
    LOCAL_POLICY,
    PayoutObservation,
    PayoutStateEnvelope,
    canonical_digest,
    unique_rows,
)

# Local experimental gate, not a rare-failure or deployment safety guarantee.
# Independent qualification uses 16 cohorts, separate from conformal calibration.
HYBRID_LOCAL_POLICY = ResourcePolicy.model_validate(
    {
        **LOCAL_POLICY.model_dump(),
        "compute_margin_bps": 0,
        "data_margin_bps": 0,
        "min_calibration_samples": 16,
    }
)


class HybridSplit(StrictModel):
    fit_ids: tuple[str, ...]
    calibration_ids: tuple[str, ...]
    qualification_ids: tuple[str, ...]
    fit_cohorts: tuple[str, ...]
    calibration_cohorts: tuple[str, ...]
    qualification_cohorts: tuple[str, ...]
    calibration_boundary_slot: PortableSlot
    qualification_boundary_slot: PortableSlot

    @model_validator(mode="after")
    def consistent(self) -> Self:
        for partitions in (
            [self.fit_ids, self.calibration_ids, self.qualification_ids],
            [self.fit_cohorts, self.calibration_cohorts, self.qualification_cohorts],
        ):
            if any(not p or len(p) != len(set(p)) for p in partitions) or any(
                set(partitions[i]) & set(partitions[j]) for i in range(3) for j in range(i)
            ):
                raise ValueError("fit, calibration and qualification must be nonempty and disjoint")
        if self.calibration_boundary_slot >= self.qualification_boundary_slot:
            raise ValueError("qualification must follow conformal calibration")
        return self


def _cohorts(rows: list[PayoutObservation], window: int) -> list[list[PayoutObservation]]:
    """Join queue/snapshot/recipient identities and overlapping slot windows."""
    parents: dict[str, str] = {}

    def root(key: str) -> str:
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for row in rows:
        keys = (
            "g:" + row.queue_group,
            "q:" + row.state.queue_address,
            "s:" + row.state.snapshot_digest,
            *("r:" + a.recipient for a in row.state.recipient_accounts),
        )
        for key in keys[1:]:
            parents[root(key)] = root(keys[0])
    joined: dict[str, list[PayoutObservation]] = defaultdict(list)
    for row in rows:
        joined[root("g:" + row.queue_group)].append(row)
    spans = sorted(
        (
            min(r.observation.slot // window for r in group),
            max(r.observation.slot // window for r in group),
            key,
            group,
        )
        for key, group in joined.items()
    )
    result: list[list[PayoutObservation]] = []
    end = -1
    for first, last, _, group in spans:
        if result and first <= end:
            result[-1].extend(group)
        else:
            result.append(list(group))
        end = max(end, last)
    return result


def _range(values: list[int | None]) -> ResourceRange:
    present = [v for v in values if v is not None]
    return ResourceRange(
        minimum=min(present) if present else None,
        maximum=max(present) if present else None,
        missing_seen=len(present) != len(values),
    )


def inputs_for(state: PayoutStateEnvelope, features: Features) -> FormulaInputs:
    # Full envelope validation is intentionally repeated after possible model_copy use.
    state = PayoutStateEnvelope.model_validate_json(state.model_dump_json())
    return FormulaInputs(
        prepared_identity=state.prepared_identity,
        bound_features_digest=features_digest(features),
        state_digest=state.snapshot_digest,
        cell_key=state.state_key,
        observed_slot=state.observation_slot,
        max_age_slots=state.max_age_slots,
        mint=state.mint,
        recipients=tuple(
            FormulaRecipient(recipient=a.recipient, address=a.address, exists=a.exists)
            for a in state.recipient_accounts
        ),
    )


class PayoutHybridBundle(StrictModel):
    artifact_version: Literal["cu-pilot-payout-hybrid-v1"] = "cu-pilot-payout-hybrid-v1"
    release_status: Literal["candidate"] = "candidate"
    context: str
    cluster_identity: str
    runtime_identity: str
    deployment_bindings: dict[str, str]
    development_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: ResourcePolicy
    split: HybridSplit
    models: dict[str, FormulaArtifact] = Field(min_length=1)
    diagnostics: dict[str, Any]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        for key, model in self.models.items():
            if (
                model.cell_key != key
                or model.context != self.context
                or model.cluster_identity != self.cluster_identity
                or model.runtime_identity != self.runtime_identity
                or model.deployment_bindings != self.deployment_bindings
                or model.policy != self.policy
                or model.development_digest != self.development_digest
            ):
                raise ValueError("hybrid model differs from bundle scope")
            if (
                model.fit_cohorts != self.split.fit_cohorts
                or model.calibration_cohorts != self.split.calibration_cohorts
                or model.qualification_cohorts != self.split.qualification_cohorts
                or model.calibration_min_slot != self.split.calibration_boundary_slot
                or model.qualification_min_slot != self.split.qualification_boundary_slot
            ):
                raise ValueError("hybrid model differs from frozen group split")
        first = next(iter(self.models.values()))
        for model in self.models.values():
            for field in (
                "compute_coefficients",
                "data_coefficients",
                "method",
                "alpha",
                "calibration_scores",
                "correction",
                "source",
                "label_source",
                "evidence_origin",
            ):
                if getattr(model, field) != getattr(first, field):
                    raise ValueError("cell artifacts must share one frozen global formula")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    def profile_id(self, key: str) -> str:
        if key not in self.models:
            raise KeyError(key)
        scope = canonical_digest(
            {
                "context": self.context,
                "cluster": self.cluster_identity,
                "runtime": self.runtime_identity,
                "deployments": self.deployment_bindings,
                "method": self.models[key].method,
            }
        )
        return "payout-hybrid-" + key + "-" + scope[:16]

    def revision_for(self, key: str) -> int:
        return self.models[key].max_slot + 1

    def estimator_for(
        self,
        state: PayoutStateEnvelope,
        *,
        current_slot: int,
        deployment_bindings: Mapping[str, str],
        prepared_identity: str | None = None,
        features: Features | None = None,
    ) -> tuple[str | None, FormulaEstimator | None, str]:
        try:
            state = PayoutStateEnvelope.model_validate_json(state.model_dump_json())
        except ValueError:
            return None, None, "invalid_state_seal"
        reason = state.risk(
            current_slot=current_slot,
            deployment_bindings=deployment_bindings,
            prepared_identity=prepared_identity,
        )
        if reason:
            return None, None, reason
        if (
            state.cluster_identity != self.cluster_identity
            or state.runtime_identity != self.runtime_identity
        ):
            return None, None, "runtime_mismatch"
        if state.deployment_bindings != self.deployment_bindings:
            return None, None, "deployment_changed"
        if state.state_key not in self.models:
            return None, None, "unseen_count_or_state"
        if features is None or prepared_identity is None:
            return None, None, "formula_features_unbound"
        return (
            self.profile_id(state.state_key),
            FormulaEstimator(self.models[state.state_key], inputs_for(state, features)),
            "candidate_profile_requires_lifecycle_check",
        )

    def predict(
        self,
        state: PayoutStateEnvelope,
        features: Features,
        *,
        current_slot: int,
        deployment_bindings: Mapping[str, str],
        prepared_identity: str | None = None,
    ) -> Prediction:
        _, estimator, reason = self.estimator_for(
            state,
            current_slot=current_slot,
            deployment_bindings=deployment_bindings,
            prepared_identity=prepared_identity,
            features=features,
        )
        if estimator is None:
            return Prediction(
                pattern_id=features.pattern_id,
                simulation_recommended=True,
                reason=reason,
                explanation="Bound formula fallback: " + reason,
            )
        return estimator.predict(features, context=self.context, current_slot=current_slot)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def fit_hybrid(
    observations: list[PayoutObservation],
    *,
    alpha: float = 0.05,
    method: Literal["count_missing", "count_missing_derivation"] = "count_missing_derivation",
    policy: ResourcePolicy = HYBRID_LOCAL_POLICY,
    fit_fraction: float = 0.5,
    calibration_fraction: float = 0.3,
) -> PayoutHybridBundle:
    if method not in METHOD_TERMS:
        raise ValueError("unknown formula method")
    if not 0 < fit_fraction < 1 or not 0 < calibration_fraction < 1 - fit_fraction:
        raise ValueError("split fractions must leave independent qualification data")
    # Revalidate loaded or constructed records; model_copy is not a validation API.
    rows = unique_rows(
        [PayoutObservation.model_validate_json(r.model_dump_json()) for r in observations]
    )
    if not rows:
        raise ValueError("formula fitting requires observations")
    groups = _cohorts(rows, policy.independence_window_slots)
    if len(groups) < 3:
        raise ValueError("need three separate cohorts without shared queue or recipient identities")
    fit_end = max(1, min(len(groups) - 2, math.floor(len(groups) * fit_fraction)))
    cal_end = max(
        fit_end + 1,
        min(len(groups) - 1, math.floor(len(groups) * (fit_fraction + calibration_fraction))),
    )
    fitting = [r for g in groups[:fit_end] for r in g]
    calibration = [r for g in groups[fit_end:cal_end] for r in g]
    qualification = [r for g in groups[cal_end:] for r in g]
    cohort_ids = [canonical_digest(sorted(r.record_id for r in g)) for g in groups]
    group_by_id = {r.record_id: cohort_ids[i] for i, g in enumerate(groups) for r in g}
    split = HybridSplit(
        fit_ids=tuple(sorted(r.record_id for r in fitting)),
        calibration_ids=tuple(sorted(r.record_id for r in calibration)),
        qualification_ids=tuple(sorted(r.record_id for r in qualification)),
        fit_cohorts=tuple(cohort_ids[:fit_end]),
        calibration_cohorts=tuple(cohort_ids[fit_end:cal_end]),
        qualification_cohorts=tuple(cohort_ids[cal_end:]),
        calibration_boundary_slot=min(r.observation.slot for r in calibration),
        qualification_boundary_slot=min(r.observation.slot for r in qualification),
    )
    first = rows[0]
    scope = {
        (
            r.observation.context,
            r.observation.source,
            r.observation.label_source or r.observation.source,
            r.observation.evidence_origin,
            r.state.cluster_identity,
            r.state.runtime_identity,
            canonical_digest(r.state.deployment_bindings),
        )
        for r in rows
    }
    if len(scope) != 1:
        raise ValueError("fit one context, runtime, deployment and label provenance at a time")
    if first.observation.source not in {"simulation", "synthetic"} or (
        first.observation.label_source or first.observation.source
    ) not in {"simulation", "synthetic"}:
        raise ValueError("hybrid resource fitting requires paired simulation labels")
    # Complete cohorts preserve the predeclared candidate menu. Missing or failed
    # labels abort fitting rather than hiding unfavorable cases from calibration.
    menu = Counter((r.state.state_key, r.observation.features.pattern_id) for r in groups[0])
    if any(
        Counter((r.state.state_key, r.observation.features.pattern_id) for r in g) != menu
        for g in groups
    ):
        raise ValueError("calibration needs complete cohorts with the same candidate menu")
    vectors: dict[str, tuple[int, ...]] = {}
    for row in rows:
        if not paired_label(row.observation):
            raise ValueError("failed or missing paired resource label: " + row.record_id)
        risk = row.state.risk(
            current_slot=row.observation.slot, deployment_bindings=first.state.deployment_bindings
        )
        risk = risk or feature_risk(row.observation.features, policy)
        if risk:
            raise ValueError("unsafe training state: " + risk)
        vectors[row.record_id] = derive_features(row.state).vector(method)
    cu = nonnegative_least_squares(
        [vectors[r.record_id] for r in fitting],
        [int(r.observation.label.compute_units or 0) for r in fitting],
    )
    data = nonnegative_least_squares(
        [vectors[r.record_id] for r in fitting],
        [int(r.observation.label.loaded_accounts_bytes or 0) for r in fitting],
    )
    scores: dict[str, float] = {key: 0.0 for key in split.calibration_cohorts}
    for row in calibration:
        vector = vectors[row.record_id]
        score = joint_score(
            int(row.observation.label.compute_units or 0),
            int(row.observation.label.loaded_accounts_bytes or 0),
            point_prediction(cu, vector),
            point_prediction(data, vector),
        )
        cohort = group_by_id[row.record_id]
        scores[cohort] = max(scores[cohort], score)
    ordered_scores = tuple(scores[key] for key in split.calibration_cohorts)
    correction = conformal_quantile(ordered_scores, alpha)
    development_digest = canonical_digest([r.model_dump(mode="json") for r in rows])
    models = {}
    fallback_counts: Counter[str] = Counter()
    for cell in sorted({r.state.state_key for r in fitting}):
        fit_cell = [r for r in fitting if r.state.state_key == cell]
        qualified_cell = [r for r in qualification if r.state.state_key == cell]
        patterns = {}
        for pattern in sorted({r.observation.features.pattern_id for r in fit_cell}):
            fit_pattern = [r for r in fit_cell if r.observation.features.pattern_id == pattern]
            feature = fit_pattern[0].observation.features
            if any(
                r.observation.features.version != feature.version
                or r.observation.features.program_ids != feature.program_ids
                or len(r.observation.features.instruction_data_lengths)
                != len(feature.instruction_data_lengths)
                for r in fit_pattern
            ):
                raise ValueError("one pattern contains incompatible instruction shapes")
            stats = FormulaPattern(
                train_count=len({group_by_id[r.record_id] for r in fit_pattern}),
                qualification_count=0,
                qualification_joint_exceedances=0,
                numerical_ranges={
                    name: _range([getattr(r.observation.features, name) for r in fit_pattern])
                    for name in NUMERICAL_FEATURES
                },
                instruction_data_length_ranges=tuple(
                    _range(
                        [r.observation.features.instruction_data_lengths[i] for r in fit_pattern]
                    )
                    for i in range(len(feature.instruction_data_lengths))
                ),
                program_ids=feature.program_ids,
                version=feature.version,
                vector_ranges=tuple(
                    _range([vectors[r.record_id][i] for r in fit_pattern])
                    for i in range(len(METHOD_TERMS[method]))
                ),
            )
            outcomes: dict[str, bool] = {}
            for row in qualified_cell:
                if row.observation.features.pattern_id != pattern:
                    continue
                risk = stats.risk(row.observation.features, vectors[row.record_id], policy)
                if correction is None:
                    risk = "insufficient_conformal_cohorts"
                if risk:
                    fallback_counts[risk] += 1
                    continue
                assert correction is not None
                cu_limit = formula_limit(
                    point_prediction(cu, vectors[row.record_id]),
                    correction,
                    policy.compute_rounding,
                )
                data_limit = formula_limit(
                    point_prediction(data, vectors[row.record_id]), correction, policy.data_rounding
                )
                exceeded = (
                    int(row.observation.label.compute_units or 0) > cu_limit
                    or int(row.observation.label.loaded_accounts_bytes or 0) > data_limit
                )
                group = group_by_id[row.record_id]
                outcomes[group] = outcomes.get(group, False) or exceeded
            count, failures = len(outcomes), sum(outcomes.values())
            patterns[pattern] = FormulaPattern.model_validate(
                {
                    **stats.model_dump(),
                    "qualification_count": count,
                    "qualification_joint_exceedances": failures,
                    "qualification_upper_bound": wilson_upper_bound(failures, count)
                    if count
                    else None,
                }
            )
        models[cell] = FormulaArtifact(
            context=first.observation.context,
            source=first.observation.source,
            label_source=first.observation.label_source,
            evidence_origin=first.observation.evidence_origin or "synthetic",
            cluster_identity=first.state.cluster_identity,
            runtime_identity=first.state.runtime_identity,
            deployment_bindings=first.state.deployment_bindings,
            cell_key=cell,
            evidence_min_slot=min(r.observation.slot for r in rows),
            max_slot=max(r.observation.slot for r in rows),
            training_max_slot=max(r.observation.slot for r in fitting),
            calibration_min_slot=split.calibration_boundary_slot,
            calibration_max_slot=max(r.observation.slot for r in calibration),
            qualification_min_slot=split.qualification_boundary_slot,
            policy=policy,
            alpha=alpha,
            method=method,
            compute_coefficients=cu,
            data_coefficients=data,
            calibration_scores=ordered_scores,
            calibration_cohorts=split.calibration_cohorts,
            fit_cohorts=split.fit_cohorts,
            qualification_cohorts=split.qualification_cohorts,
            correction=correction,
            development_digest=development_digest,
            patterns=patterns,
        )
    return PayoutHybridBundle(
        context=first.observation.context,
        cluster_identity=first.state.cluster_identity,
        runtime_identity=first.state.runtime_identity,
        deployment_bindings=first.state.deployment_bindings,
        development_digest=development_digest,
        policy=policy,
        split=split,
        models=models,
        diagnostics={
            "input_rows": len(observations),
            "unique_rows": len(rows),
            "cohort_count": len(groups),
            "fit_groups": len(split.fit_cohorts),
            "calibration_groups": len(split.calibration_cohorts),
            "qualification_groups": len(split.qualification_cohorts),
            "qualification_fallbacks": dict(fallback_counts),
            "qualification_gate": "one-sided-95%-Wilson-local-development-only",
            "untouched_comparison_required": True,
            "production_risk_guarantee": False,
        },
    )


def qualify_hybrid_bundle(
    bundle: PayoutHybridBundle,
    registry: ProfileRegistry,
    *,
    deployments: Sequence[DeploymentEvidence],
    dependencies: dict[str, tuple[str, ...]],
    current_slot: int,
    workload: str = "payout-queue-v1",
    actor: str = "local-demo-operator",
    control_probability: float = 0.05,
) -> dict[str, Any]:
    bundle = PayoutHybridBundle.model_validate_json(bundle.model_dump_json())
    if any(
        m.source != "simulation" or m.evidence_origin != "local-runtime"
        for m in bundle.models.values()
    ):
        raise ValueError(
            "only actual local-runtime simulation evidence may release this experiment"
        )
    if {d.program_id: d.fingerprint for d in deployments} != bundle.deployment_bindings:
        raise ValueError("qualification deployments differ from collected evidence")
    if set(dependencies) != set(bundle.deployment_bindings):
        raise ValueError("declare the full CPI dependency closure including every program")
    registry.record_deployments(deployments)
    active, failed = [], {}
    for key, model in bundle.models.items():
        profile_id, revision = bundle.profile_id(key), bundle.revision_for(key)
        manifest = ProfileManifest(
            profile_id=profile_id,
            revision=revision,
            artifact_sha256=artifact_digest(model.model_dump_json()),
            context=bundle.context,
            cluster_identity=bundle.cluster_identity,
            runtime_identity=bundle.runtime_identity,
            workload_allowlist=(workload,),
            deployment_bindings=bundle.deployment_bindings,
            dependencies=dependencies,
            dependency_closure_verified=True,
            budget_independent=True,
            evidence_min_slot=model.evidence_min_slot,
            evidence_max_slot=model.max_slot,
            provenance=model.source,
            max_observation_age_slots=model.policy.max_age_slots,
            control_probability=control_probability,
        )
        try:
            registry.register(manifest, model.model_dump_json(), actor=actor)
            existing = registry.check(
                profile_id,
                current_slot=current_slot,
                context=bundle.context,
                cluster_identity=bundle.cluster_identity,
                runtime_identity=bundle.runtime_identity,
                workload=workload,
                program_ids=tuple(bundle.deployment_bindings),
            )
            if existing.eligible and existing.revision == revision:
                active.append(profile_id)
                continue
            history = [
                e
                for e in registry.audit_events()
                if e["id"] == profile_id and e["revision"] == revision
            ]
            if not any(e["action"] == "shadow" for e in history):
                registry.transition(
                    profile_id,
                    revision,
                    "shadow",
                    actor=actor,
                    reason="explicit local independent formula qualification",
                )
            registry.activate(
                profile_id,
                revision,
                actor=actor,
                reason="separate grouped formula qualification",
                current_slot=current_slot,
                context=bundle.context,
                cluster_identity=bundle.cluster_identity,
                runtime_identity=bundle.runtime_identity,
                workload=workload,
                program_ids=tuple(bundle.deployment_bindings),
            )
            active.append(profile_id)
        except ValueError as exc:
            failed[key] = str(exc)
    return {
        "bundle_digest": bundle.digest,
        "scope": "local-runtime-experiment-only",
        "active_count": len(active),
        "candidate_cell_count": len(bundle.models),
        "active_profiles": active,
        "qualification_failures": failed,
        "production_risk_guarantee": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--method", choices=tuple(METHOD_TERMS), default="count_missing_derivation")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; preserve frozen experiment artifacts")
    rows = [PayoutObservation.model_validate(value) for value in read_jsonl(args.data)]
    bundle = fit_hybrid(rows, alpha=args.alpha, method=args.method)
    bundle.save(args.output)
    print(
        json.dumps(
            {
                "artifact": str(args.output),
                "digest": bundle.digest,
                "statistically_qualified_cells": sum(
                    m.qualified_for_activation() for m in bundle.models.values()
                ),
                "release_status": "candidate",
                **bundle.diagnostics,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
