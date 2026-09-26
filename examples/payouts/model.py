"""State-conditioned learned quantiles using CU Pilot's existing resource profiles.

The application selects a resource profile from pre-execution account evidence.
The core still owns fitting, calibration, message binding, release eligibility,
sampled controls and fallback. A bundle is a candidate, never a release token.

Run ``uv run python -m examples.payouts.model train DATA --output ARTIFACT``.
Only the collection/application layer reads RPC state, signs or submits anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, StrictBool, StrictInt, TypeAdapter, model_validator
from solders.pubkey import Pubkey

from cu_pilot.binding import bind_message
from cu_pilot.data import read_jsonl
from cu_pilot.lifecycle import DeploymentEvidence, ProfileManifest, ProfileRegistry, artifact_digest
from cu_pilot.resources import (
    PortableSlot,
    ResourceArtifact,
    ResourceEstimator,
    ResourcePolicy,
    paired_label,
)
from cu_pilot.schemas import MAX_COMPUTE_UNITS, Features, Observation, Prediction, StrictModel

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SUPPORTED_COUNTS = (1, 2, 3, 4, 5, 6, 7, 8)
SHA256 = r"^[0-9a-f]{64}$"

# Fixed before observing workload data. This is a bounded local demonstration
# policy, substantially weaker than the library's research default (30/100, 5%).
# The empirical upper bound is explicitly not a production rare-failure SLO.
LOCAL_POLICY = ResourcePolicy(
    quantile=0.99,
    compute_margin_bps=1000,
    data_margin_bps=1000,
    min_samples=12,
    min_calibration_samples=20,
    max_joint_underestimation_rate=0.15,
    max_age_slots=216_000,
)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def recipient_ata(recipient: str, mint: str) -> str:
    return str(
        Pubkey.find_program_address(
            [
                bytes(Pubkey.from_string(recipient)),
                bytes(Pubkey.from_string(TOKEN_PROGRAM)),
                bytes(Pubkey.from_string(mint)),
            ],
            Pubkey.from_string(ATA_PROGRAM),
        )[0]
    )


class RecipientAccountState(StrictModel):
    """Account evidence read before building/simulating this candidate."""

    address: str
    recipient: str
    exists: StrictBool
    data_bytes: StrictInt = Field(ge=0)
    initialized: StrictBool
    frozen: StrictBool
    token_mint: str | None = None
    token_authority: str | None = None
    program_owner: str | None = None

    def supported(self, mint: str) -> bool:
        if self.address != recipient_ata(self.recipient, mint):
            return False
        if not self.exists:
            return (
                self.data_bytes == 0
                and not self.initialized
                and not self.frozen
                and self.token_mint is None
                and self.token_authority is None
                and self.program_owner is None
            )
        return (
            self.data_bytes == 165
            and self.initialized
            and not self.frozen
            and self.token_mint == mint
            and self.token_authority == self.recipient
            and self.program_owner == TOKEN_PROGRAM
        )


class PayoutStateEnvelope(StrictModel):
    schema_version: Literal["cu-pilot-payout-state-v1"] = "cu-pilot-payout-state-v1"
    queue_address: str
    queue_identity: str = Field(min_length=1)
    mint: str
    executor: str
    cursor: StrictInt = Field(ge=0, le=16)
    remaining: StrictInt = Field(ge=0, le=16)
    candidate_count: StrictInt = Field(ge=1, le=8)
    queue_data_bytes: StrictInt = Field(gt=0)
    vault_data_bytes: StrictInt = Field(default=165, ge=0)
    mint_data_bytes: StrictInt = Field(default=82, ge=0)
    vault_initialized: StrictBool = True
    vault_frozen: StrictBool = False
    mint_initialized: StrictBool = True
    paused: StrictBool = False
    terminal: StrictBool = False
    recipient_accounts: tuple[RecipientAccountState, ...]
    deployment_bindings: dict[str, str] = Field(min_length=1)
    cluster_identity: str = Field(min_length=1)
    runtime_identity: str = Field(min_length=1)
    observation_slot: PortableSlot
    max_age_slots: PortableSlot = 8
    prepared_identity: str = Field(min_length=1)
    approved_payments_digest: str = Field(pattern=SHA256)
    snapshot_digest: str = Field(pattern=SHA256)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.cursor + self.remaining > 16 or self.candidate_count > self.remaining:
            raise ValueError("candidate is not a pending queue prefix")
        if len(self.recipient_accounts) != self.candidate_count:
            raise ValueError("candidate account count does not match approved prefix")
        if len({account.recipient for account in self.recipient_accounts}) != self.candidate_count:
            raise ValueError("duplicate recipients are unsupported")
        for address in (self.queue_address, self.mint, self.executor, *self.deployment_bindings):
            Pubkey.from_string(address)
        for fingerprint in self.deployment_bindings.values():
            if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
                raise ValueError("deployment identity must be SHA-256 hex")
        expected = canonical_digest(self.model_dump(mode="json", exclude={"snapshot_digest"}))
        if expected != self.snapshot_digest:
            raise ValueError("pre-execution snapshot digest mismatch")
        return self

    @classmethod
    def seal(cls, **values: Any) -> Self:
        """Normalize fields before hashing, including portable slot encodings."""
        value = dict(values)
        value.pop("snapshot_digest", None)
        value["recipient_accounts"] = tuple(
            RecipientAccountState.model_validate(item) for item in value["recipient_accounts"]
        )
        # Validation with a temporary digest normalizes defaults and nested values
        # without permitting a caller to bypass the final immutable digest check.
        provisional = cls.model_construct(**value, snapshot_digest="0" * 64)
        normalized = provisional.model_dump(mode="json", exclude={"snapshot_digest"})
        normalized["observation_slot"] = str(value["observation_slot"])
        normalized["max_age_slots"] = str(value.get("max_age_slots", 8))
        normalized["recipient_accounts"] = [
            RecipientAccountState.model_validate(item).model_dump(mode="json")
            for item in value["recipient_accounts"]
        ]
        return cls.model_validate({**normalized, "snapshot_digest": canonical_digest(normalized)})

    @property
    def missing_atas(self) -> int:
        return sum(not account.exists for account in self.recipient_accounts)

    @property
    def state_key(self) -> str:
        """Addresses, queue identity, cursor and labels never become model features."""
        return (
            f"n{self.candidate_count}-m{self.missing_atas}-q{self.queue_data_bytes}"
            f"-v{self.vault_data_bytes}-t{self.mint_data_bytes}"
        )

    def risk(
        self,
        *,
        current_slot: int,
        deployment_bindings: Mapping[str, str],
        prepared_identity: str | None = None,
    ) -> str | None:
        if current_slot < self.observation_slot:
            return "future_state"
        if current_slot - self.observation_slot > self.max_age_slots:
            return "stale_state"
        if dict(deployment_bindings) != self.deployment_bindings:
            return "deployment_changed"
        if prepared_identity is not None and prepared_identity != self.prepared_identity:
            return "state_message_mismatch"
        if self.paused or self.terminal:
            return "queue_not_executable"
        if self.candidate_count not in (1, 2, 4, 8) and self.candidate_count != self.remaining:
            return "unsupported_nonterminal_count"
        if (
            self.vault_data_bytes != 165
            or self.mint_data_bytes != 82
            or not self.vault_initialized
            or self.vault_frozen
            or not self.mint_initialized
        ):
            return "unsupported_token_state"
        if any(not account.supported(self.mint) for account in self.recipient_accounts):
            return "unsupported_recipient_state"
        return None

    def verify_wire(self, wire_base64: str, current_slot: int) -> Features:
        """Bind state to the core's exact prepared message, never to a loose shape."""
        bound = bind_message(wire_base64, current_slot=current_slot)
        reason = self.risk(
            current_slot=current_slot,
            deployment_bindings=self.deployment_bindings,
            prepared_identity=bound.prepared_identity,
        )
        if reason:
            raise ValueError(reason)
        return bound.features


class PayoutObservation(StrictModel):
    schema_version: Literal["cu-pilot-payout-observation-v1"] = "cu-pilot-payout-observation-v1"
    record_id: str = Field(min_length=1)
    queue_group: str = Field(min_length=1)
    state: PayoutStateEnvelope
    observation: Observation

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.record_id != self.observation.record_id:
            raise ValueError("outer and core observation identities differ")
        if self.observation.slot < self.state.observation_slot:
            raise ValueError("label cannot precede its pre-execution snapshot")
        return self


class SplitManifest(StrictModel):
    fit_ids: tuple[str, ...]
    calibration_ids: tuple[str, ...]
    holdout_ids: tuple[str, ...]
    fit_groups: StrictInt = Field(ge=1)
    calibration_groups: StrictInt = Field(ge=1)
    holdout_groups: StrictInt = Field(ge=1)
    calibration_boundary_slot: PortableSlot
    holdout_boundary_slot: PortableSlot
    fit_fraction: float
    calibration_fraction: float


def unique_rows(rows: list[PayoutObservation]) -> list[PayoutObservation]:
    seen: dict[str, PayoutObservation] = {}
    for row in rows:
        if row.record_id in seen and row != seen[row.record_id]:
            raise ValueError("conflicting payout observations share an identity")
        seen[row.record_id] = row
    return sorted(seen.values(), key=lambda row: (row.observation.slot, row.record_id))


def grouped_split(
    rows: list[PayoutObservation],
    *,
    fit_fraction: float = 0.5,
    calibration_fraction: float = 0.3,
    independence_window_slots: int = 1,
) -> SplitManifest:
    """Freeze chronology before filtering; related groups and slot windows stay whole.

    Repeated queue identities or snapshots are joined. Overlapping group intervals
    are then merged, so even temporally interleaved queues cannot leak across a
    boundary. The collector must generate sequential independent queue groups.
    """
    if not 0 < fit_fraction < 1 or not 0 < calibration_fraction < 1 - fit_fraction:
        raise ValueError("fit/calibration fractions must leave a nonempty holdout")
    if type(independence_window_slots) is not int or independence_window_slots < 1:
        raise ValueError("evidence window must be positive")
    rows = unique_rows(rows)
    parents: dict[str, str] = {}

    def root(key: str) -> str:
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = root(parents[key])
        return parents[key]

    for row in rows:
        keys = (
            "group:" + row.queue_group,
            "queue:" + row.state.queue_address,
            "snapshot:" + row.state.snapshot_digest,
        )
        for key in keys[1:]:
            parents[root(key)] = root(keys[0])
    grouped: dict[str, list[PayoutObservation]] = defaultdict(list)
    for row in rows:
        grouped[root("group:" + row.queue_group)].append(row)
    spans = sorted(
        (
            min(r.observation.slot // independence_window_slots for r in group),
            max(r.observation.slot // independence_window_slots for r in group),
            key,
            group,
        )
        for key, group in grouped.items()
    )
    groups: list[list[PayoutObservation]] = []
    last_end = -1
    for start, end, _, group in spans:
        if groups and start <= last_end:
            groups[-1].extend(group)
            last_end = max(last_end, end)
        else:
            groups.append(list(group))
            last_end = end
    if len(groups) < 3:
        raise ValueError("need three chronologically separate queue/state groups")
    fit_end = max(1, min(len(groups) - 2, math.floor(len(groups) * fit_fraction)))
    calibration_end = max(
        fit_end + 1,
        min(len(groups) - 1, math.floor(len(groups) * (fit_fraction + calibration_fraction))),
    )
    fitting = [r for group in groups[:fit_end] for r in group]
    calibration = [r for group in groups[fit_end:calibration_end] for r in group]
    holdout = [r for group in groups[calibration_end:] for r in group]
    return SplitManifest(
        fit_ids=tuple(sorted(r.record_id for r in fitting)),
        calibration_ids=tuple(sorted(r.record_id for r in calibration)),
        holdout_ids=tuple(sorted(r.record_id for r in holdout)),
        fit_groups=fit_end,
        calibration_groups=calibration_end - fit_end,
        holdout_groups=len(groups) - calibration_end,
        calibration_boundary_slot=min(r.observation.slot for r in calibration),
        holdout_boundary_slot=min(r.observation.slot for r in holdout),
        fit_fraction=fit_fraction,
        calibration_fraction=calibration_fraction,
    )


class PayoutModelBundle(StrictModel):
    artifact_version: Literal["cu-pilot-payout-quantiles-v1"] = "cu-pilot-payout-quantiles-v1"
    release_status: Literal["candidate"] = "candidate"
    learning_method: Literal["state-conditioned-paired-quantiles"] = (
        "state-conditioned-paired-quantiles"
    )
    context: str
    cluster_identity: str
    runtime_identity: str
    deployment_bindings: dict[str, str]
    development_digest: str = Field(pattern=SHA256)
    split: SplitManifest
    policy: ResourcePolicy
    models: dict[str, ResourceArtifact]
    pattern_p99: ResourceArtifact
    diagnostics: dict[str, Any]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not self.models:
            raise ValueError("bundle must contain at least one fitted state profile")
        partitions = [
            set(self.split.fit_ids),
            set(self.split.calibration_ids),
            set(self.split.holdout_ids),
        ]
        if any(partitions[i] & partitions[j] for i in range(3) for j in range(i)):
            raise ValueError("an observation belongs to more than one frozen partition")
        if self.split.calibration_boundary_slot >= self.split.holdout_boundary_slot:
            raise ValueError("calibration must precede the frozen holdout")
        if any(model.policy != self.policy for model in self.models.values()):
            raise ValueError("state profiles must use the declared bundle policy")
        if self.pattern_p99.policy != self.policy.model_copy(update={"quantile": 0.99}):
            raise ValueError("pattern baseline must use the declared policy with p99")
        minimum_slots = TypeAdapter(dict[str, PortableSlot]).validate_python(
            self.diagnostics.get("cell_evidence_min_slots")
        )
        if minimum_slots.keys() != self.models.keys():
            raise ValueError("record the earliest actual evidence slot for every fitted cell")
        if any(minimum_slots[key] > model.training_max_slot for key, model in self.models.items()):
            raise ValueError("cell evidence minimum must not follow its fitting observations")
        for model in (*self.models.values(), self.pattern_p99):
            if model.context != self.context:
                raise ValueError("bundle profiles require one context")
            if model.max_slot >= self.split.holdout_boundary_slot:
                raise ValueError("profile incorporates frozen holdout evidence")
            if model.training_max_slot >= self.split.calibration_boundary_slot:
                raise ValueError("profile fitting incorporates calibration evidence")
            if (model.source, model.label_source, model.evidence_origin) != (
                self.pattern_p99.source,
                self.pattern_p99.label_source,
                self.pattern_p99.evidence_origin,
            ):
                raise ValueError("bundle profiles mix observation provenance")
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
            }
        )
        return "payout-" + key + "-" + scope[:16]

    def revision_for(self, key: str) -> int:
        # Stable profile IDs preserve quarantine across training runs. Local slot
        # chronology provides monotonic revisions; conflicting same-slot refits
        # correctly fail immutable registration rather than rewriting a release.
        return self.models[key].max_slot + 1

    def estimator_for(
        self,
        state: PayoutStateEnvelope,
        *,
        current_slot: int,
        deployment_bindings: Mapping[str, str],
        prepared_identity: str | None = None,
    ) -> tuple[str | None, ResourceEstimator | None, str]:
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
        return (
            self.profile_id(state.state_key),
            ResourceEstimator(self.models[state.state_key]),
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
        """Statistical proposal only; application must use the bound core adapter."""
        _, estimator, reason = self.estimator_for(
            state,
            current_slot=current_slot,
            deployment_bindings=deployment_bindings,
            prepared_identity=prepared_identity,
        )
        if estimator is None:
            return Prediction(
                pattern_id=features.pattern_id,
                simulation_recommended=True,
                reason=reason,
                explanation="Pre-execution state requires bounded fallback: " + reason,
            )
        return estimator.predict(features, context=self.context, current_slot=current_slot)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def qualify_bundle(
    bundle: PayoutModelBundle,
    registry: ProfileRegistry,
    *,
    deployments: Sequence[DeploymentEvidence],
    dependencies: dict[str, tuple[str, ...]],
    current_slot: int,
    workload: str = "payout-queue-v1",
    actor: str = "local-demo-operator",
    control_probability: float = 0.05,
) -> dict[str, Any]:
    """Explicit local release action using the existing immutable profile lifecycle.

    The caller has refreshed real full-code deployment evidence and supplies the
    reviewed complete CPI dependency closure. Training never calls this function.
    Unqualified cells stay candidates; a return value is not a production claim.
    """
    if bundle.pattern_p99.source != "simulation" or (
        bundle.pattern_p99.evidence_origin != "local-runtime"
    ):
        raise ValueError("only actual local-runtime simulation evidence may release this demo")
    if {d.program_id: d.fingerprint for d in deployments} != bundle.deployment_bindings:
        raise ValueError("qualification deployments differ from collected evidence")
    if set(dependencies) != set(bundle.deployment_bindings):
        raise ValueError("declare the full CPI dependency closure including every program")
    if any(
        not set(stats.program_ids) <= set(bundle.deployment_bindings)
        for model in bundle.models.values()
        for stats in model.patterns.values()
    ):
        raise ValueError("deployment closure omits a program used by a fitted transaction")
    registry.record_deployments(deployments)
    active = []
    failed = {}
    for key, model in bundle.models.items():
        profile_id = bundle.profile_id(key)
        revision = bundle.revision_for(key)
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
            evidence_min_slot=int(bundle.diagnostics["cell_evidence_min_slots"][key]),
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
            # A previous failed activation can already have entered shadow. Audit
            # history is read through the public API; no private DB state mutation.
            history = [
                event
                for event in registry.audit_events()
                if event["id"] == profile_id and event["revision"] == revision
            ]
            if not any(event["action"] == "shadow" for event in history):
                registry.transition(
                    profile_id,
                    revision,
                    "shadow",
                    actor=actor,
                    reason="explicit local payout qualification",
                )
            registry.activate(
                profile_id,
                revision,
                actor=actor,
                reason="paired local payout calibration",
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
        "scope": "local-runtime-only",
        "active_count": len(active),
        "candidate_cell_count": len(bundle.models),
        "active_profiles": active,
        "qualification_failures": failed,
        "minimum_fit_windows": bundle.policy.min_samples,
        "minimum_calibration_windows": bundle.policy.min_calibration_samples,
        "maximum_joint_empirical_upper_bound": bundle.policy.max_joint_underestimation_rate,
        "production_risk_guarantee": False,
        "profile_support": {
            key: {
                pattern: {
                    "fitting_windows": stats.train_count,
                    "calibration_windows": stats.calibration_count,
                    "joint_exceedances": stats.calibration_joint_exceedances,
                    "joint_upper_bound": stats.calibration_upper_bound,
                }
                for pattern, stats in model.patterns.items()
            }
            for key, model in bundle.models.items()
        },
    }


def fit_bundle(
    observations: list[PayoutObservation],
    *,
    policy: ResourcePolicy = LOCAL_POLICY,
    fit_fraction: float = 0.5,
    calibration_fraction: float = 0.3,
) -> PayoutModelBundle:
    rows = unique_rows(observations)
    if not rows:
        raise ValueError("training requires payout observations")
    split = grouped_split(
        rows,
        fit_fraction=fit_fraction,
        calibration_fraction=calibration_fraction,
        independence_window_slots=policy.independence_window_slots,
    )
    # Freeze the holdout before examining any labels, supported state or provenance.
    development_ids = set(split.fit_ids + split.calibration_ids)
    development = [row for row in rows if row.record_id in development_ids]
    first = development[0]
    if (
        len(
            {
                (
                    row.observation.context,
                    row.observation.source,
                    row.observation.label_source or row.observation.source,
                    row.observation.evidence_origin,
                    row.state.cluster_identity,
                    row.state.runtime_identity,
                    canonical_digest(row.state.deployment_bindings),
                )
                for row in development
            }
        )
        != 1
    ):
        raise ValueError("fit one runtime, deployment and label provenance at a time")
    if first.observation.source not in {"simulation", "synthetic"} or (
        first.observation.label_source or first.observation.source
    ) not in {"simulation", "synthetic"}:
        raise ValueError("payout dual-resource training requires paired simulation labels")
    cells: dict[str, list[Observation]] = defaultdict(list)
    unsupported = Counter()
    for row in development:
        reason = row.state.risk(
            current_slot=row.observation.slot,
            deployment_bindings=first.state.deployment_bindings,
        )
        if reason:
            unsupported[reason] += 1
        else:
            cells[row.state.state_key].append(row.observation)
    models: dict[str, ResourceArtifact] = {}
    unfitted = {}
    for key, cell in sorted(cells.items()):
        try:
            models[key] = ResourceEstimator.fit(
                cell,
                policy,
                calibration_boundary_slot=split.calibration_boundary_slot,
            ).model
        except ValueError as exc:
            unfitted[key] = str(exc)
    if not models:
        raise ValueError("no fitted state cells: " + json.dumps(unfitted, sort_keys=True))
    pattern_p99 = ResourceEstimator.fit(
        [r for cell in cells.values() for r in cell],
        policy.model_copy(update={"quantile": 0.99}),
        calibration_boundary_slot=split.calibration_boundary_slot,
    ).model
    return PayoutModelBundle(
        context=first.observation.context,
        cluster_identity=first.state.cluster_identity,
        runtime_identity=first.state.runtime_identity,
        deployment_bindings=first.state.deployment_bindings,
        development_digest=canonical_digest([r.model_dump(mode="json") for r in development]),
        split=split,
        policy=policy,
        models=models,
        pattern_p99=pattern_p99,
        diagnostics={
            "input_count": len(observations),
            "unique_count": len(rows),
            "duplicate_count": len(observations) - len(rows),
            "fit_count": len(split.fit_ids),
            "calibration_count": len(split.calibration_ids),
            "holdout_count": len(split.holdout_ids),
            "cell_evidence_min_slots": {
                key: str(min(row.slot for row in cells[key])) for key in models
            },
            "unsupported_development_state": dict(unsupported),
            "unfitted_cells": unfitted,
            "paired_development_count": sum(paired_label(r.observation) for r in development),
            "failed_development_count": sum(not r.observation.label.success for r in development),
            "missing_data_development_count": sum(
                r.observation.label.loaded_accounts_bytes is None for r in development
            ),
            "local_policy_only": True,
            "independence_caveat": (
                "separate queue groups and slot windows are not a risk guarantee"
            ),
        },
    )


def _distribution(values: list[float | int]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    return {
        "mean": statistics.mean(ordered) if ordered else None,
        "median": statistics.median(ordered) if ordered else None,
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
    }


def capacity_sensitivity(holdout: list[PayoutObservation]) -> dict[str, Any]:
    """Describe measured label capacity, without fitting or running another policy."""
    from examples.payouts.planning import PLANNING_POLICY

    paired = [row for row in holdout if paired_label(row.observation)]

    def at_cap(rows: list[PayoutObservation], cap: int) -> dict[str, int | float | None]:
        fits = sum(
            row.observation.label.compute_units <= cap
            and row.observation.label.loaded_accounts_bytes <= PLANNING_POLICY.loaded_bytes_cap
            for row in rows
        )
        return {
            "compute_cap": cap,
            "loaded_bytes_cap": PLANNING_POLICY.loaded_bytes_cap,
            "within_both": fits,
            "successful_paired_labels": len(rows),
            "within_both_rate": fits / len(rows) if rows else None,
        }

    per_count = {}
    for count in sorted({row.state.candidate_count for row in holdout}):
        labels = [row for row in paired if row.state.candidate_count == count]
        per_count[str(count)] = {
            "holdout_rows": sum(row.state.candidate_count == count for row in holdout),
            "successful_paired_labels": len(labels),
            "maximum_observed_compute_units": max(
                (row.observation.label.compute_units for row in labels), default=None
            ),
            "maximum_observed_loaded_accounts_bytes": max(
                (row.observation.label.loaded_accounts_bytes for row in labels), default=None
            ),
            "declared_policy": at_cap(labels, PLANNING_POLICY.compute_cap),
            "protocol_compute_ceiling": at_cap(labels, MAX_COMPUTE_UNITS),
        }
    eight = per_count.get("8")
    return {
        "scope": "descriptive frozen-holdout resource labels; no alternate executions",
        "holdout_rows": len(holdout),
        "successful_paired_labels": len(paired),
        "failed_or_unpaired_rows": len(holdout) - len(paired),
        "declared_policy": at_cap(paired, PLANNING_POLICY.compute_cap),
        "protocol_compute_ceiling": at_cap(paired, MAX_COMPUTE_UNITS),
        "by_candidate_count": per_count,
        "all_observed_count8_pairs_fit_protocol_ceiling": (
            eight["protocol_compute_ceiling"]["within_both"] == eight["successful_paired_labels"]
            if eight and eight["successful_paired_labels"]
            else None
        ),
        "interpretation": (
            "The 100,000-CU scheduling cap was fixed before labels. A benefit under it does "
            "not establish economic value or superiority at the protocol compute ceiling. "
            "If two valid batches of eight fit, ordinary batching can complete sixteen "
            "obligations in two transactions. This label comparison adds no measured "
            "completion timings, fees, serialized-size support or safety guarantee."
        ),
    }


def evaluate_bundle(
    bundle: PayoutModelBundle,
    observations: list[PayoutObservation],
) -> dict[str, Any]:
    """Frozen holdout resource estimates; never claims queue/RPC/timing savings."""
    by_id = {row.record_id: row for row in unique_rows(observations)}
    if not set(bundle.split.holdout_ids) <= by_id.keys():
        raise ValueError("evaluation is missing frozen holdout records")
    holdout = [by_id[key] for key in bundle.split.holdout_ids]
    if any(
        (
            row.observation.context,
            row.observation.source,
            row.observation.label_source or row.observation.source,
            row.observation.evidence_origin,
        )
        != (
            bundle.context,
            bundle.pattern_p99.source,
            bundle.pattern_p99.label_source or bundle.pattern_p99.source,
            bundle.pattern_p99.evidence_origin,
        )
        for row in holdout
    ):
        raise ValueError("evaluate each context and label provenance separately")
    summaries = {}
    for method in ("state_conditioned_quantiles", "pattern_p99"):
        accepted = paired = compute_fail = data_fail = joint_fail = 0
        compute_over: list[int] = []
        data_over: list[int] = []
        reasons: Counter[str] = Counter()
        for row in holdout:
            if method == "state_conditioned_quantiles":
                prediction = bundle.predict(
                    row.state,
                    row.observation.features,
                    current_slot=row.observation.slot,
                    deployment_bindings=row.state.deployment_bindings,
                )
            else:
                reason = row.state.risk(
                    current_slot=row.observation.slot,
                    deployment_bindings=bundle.deployment_bindings,
                )
                prediction = ResourceEstimator(bundle.pattern_p99).predict(
                    row.observation.features,
                    context=row.observation.context,
                    current_slot=row.observation.slot,
                )
                if reason:
                    prediction = Prediction(
                        pattern_id=row.observation.features.pattern_id,
                        simulation_recommended=True,
                        reason=reason,
                        explanation=reason,
                    )
            if prediction.simulation_recommended:
                reasons[prediction.reason] += 1
                continue
            accepted += 1
            if paired_label(row.observation):
                paired += 1
                cu = prediction.compute_unit_limit
                data = prediction.loaded_accounts_data_size_limit
                actual_cu = row.observation.label.compute_units
                actual_data = row.observation.label.loaded_accounts_bytes
                assert cu is not None and data is not None
                assert actual_cu is not None and actual_data is not None
                compute_fail += actual_cu > cu
                data_fail += actual_data > data
                joint_fail += actual_cu > cu or actual_data > data
                compute_over.append(max(0, cu - actual_cu))
                data_over.append(max(0, data - actual_data))
        summaries[method] = {
            "total": len(holdout),
            "accepted": accepted,
            "paired_scored": paired,
            "accepted_failed_or_unpaired": accepted - paired,
            "coverage": accepted / len(holdout),
            "fallback": len(holdout) - accepted,
            "fallback_rate": 1 - accepted / len(holdout),
            "fallback_reasons": dict(reasons),
            "compute_underestimations": compute_fail,
            "data_underestimations": data_fail,
            "joint_underestimations": joint_fail,
            "compute_underestimation_rate": compute_fail / paired if paired else None,
            "data_underestimation_rate": data_fail / paired if paired else None,
            "joint_underestimation_rate": joint_fail / paired if paired else None,
            "compute_over_allocation": _distribution(compute_over),
            "loaded_bytes_over_allocation": _distribution(data_over),
        }
    return {
        "report_version": "cu-pilot-payout-estimate-evaluation-v1",
        "bundle_digest": bundle.digest,
        "evidence_origin": bundle.pattern_p99.evidence_origin,
        "label_source": bundle.pattern_p99.label_source or bundle.pattern_p99.source,
        "split": bundle.split.model_dump(mode="json"),
        "counts": bundle.diagnostics,
        "methods": summaries,
        "capacity_sensitivity": capacity_sensitivity(holdout),
        "limitations": [
            "Resource-estimate holdout only; queue completion/ablation need actual execution.",
            "No fees, rent, latency, RPC savings or production reliability inferred from replay.",
            "Statistical coverage is separate from active release eligibility and controls.",
            "Local policy is weaker than the reusable core default and scoped to measured runtime.",
            "Descriptive capacity sensitivity does not retune policy or execute another budget.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    train = subparsers.add_parser("train", help="Fit a candidate; does not activate a release")
    train.add_argument("dataset", type=Path)
    train.add_argument("--output", required=True, type=Path)
    train.add_argument("--report", type=Path)
    train.add_argument("--baselines", type=Path, help="Export fit-only baseline parameters")
    evaluate = subparsers.add_parser("evaluate", help="Score only the frozen holdout")
    evaluate.add_argument("dataset", type=Path)
    evaluate.add_argument("--artifact", required=True, type=Path)
    evaluate.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    rows = [PayoutObservation.model_validate(row) for row in read_jsonl(args.dataset)]
    if args.command == "train":
        bundle = fit_bundle(rows)
        bundle.save(args.output)
        if args.baselines is not None:
            from examples.payouts.planning import fit_baselines

            fit_baselines(bundle, rows).save(args.baselines)
    else:
        bundle = PayoutModelBundle.load(args.artifact)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(evaluate_bundle(bundle, rows), indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"artifact_digest": bundle.digest, "counts": bundle.diagnostics}, indent=2))


if __name__ == "__main__":
    main()
