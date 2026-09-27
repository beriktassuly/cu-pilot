"""Application baseline estimates; uncertainty follows the same bounded fallback."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from cu_pilot.lifecycle import ProfileRegistry, artifact_digest
from cu_pilot.resources import ResourceEstimator, rounded_limit, support_risk
from cu_pilot.schemas import Features, ResourceLabel
from examples.payouts.model import PayoutModelBundle, PayoutStateEnvelope, canonical_digest
from examples.payouts.planning import BaselineArtifact

CACHE_MAX_AGE_SLOTS = 40
CACHE_MAX_USES = 8


def baseline_invalidation_key(
    method: str,
    bundle: PayoutModelBundle,
    fitted_baselines: BaselineArtifact | dict[str, Any],
    count: int,
) -> str:
    """Scope durable failure evidence across queues using the same fitted limit.

    The application persists this key before signing and invalidates that original
    key after confirmed resource exhaustion. Neither an address change nor another
    state cell of the same count clears it. A new fitted artifact has a new key;
    its ordinary support, freshness and quarantine checks still apply.
    """
    if method not in {"fixed_batch", "formula"}:
        raise ValueError("invalidation requires a fitted baseline method")
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("baseline count must be from one through eight")
    fitted = BaselineArtifact.model_validate(fitted_baselines)
    if fitted.bundle_digest != bundle.digest:
        raise ValueError("baseline artifact belongs to a different frozen fit/holdout split")
    return "fitted-baseline-v1:" + canonical_digest(
        {
            "method": method,
            "count": count,
            "fitted_artifact": fitted.model_dump(mode="json"),
            "context": bundle.context,
            "cluster": bundle.cluster_identity,
            "runtime": bundle.runtime_identity,
            "deployments": bundle.deployment_bindings,
        }
    )


def fitted_baseline_risk(
    method: str,
    bundle: PayoutModelBundle,
    state: PayoutStateEnvelope,
    features: Features,
    *,
    current_slot: int,
    fitted_baselines: BaselineArtifact | dict[str, Any],
    registry: ProfileRegistry | None = None,
    invalidated_keys: Collection[str] = (),
) -> str | None:
    """Check fitted-estimate scope without requiring a learned release.

    Without a registry this checks an offline statistical proposal only. Live
    callers must provide their registry, retain the returned failure reason, and
    repeat final state, deployment and exact-message checks before signing.
    """
    fitted = BaselineArtifact.model_validate(fitted_baselines)
    key = baseline_invalidation_key(method, bundle, fitted, state.candidate_count)
    if key in invalidated_keys:
        return "baseline_invalidated"
    reason = state.risk(current_slot=current_slot, deployment_bindings=bundle.deployment_bindings)
    if reason:
        return reason
    if (
        state.cluster_identity != bundle.cluster_identity
        or state.runtime_identity != bundle.runtime_identity
    ):
        return "baseline_runtime_mismatch"
    if state.candidate_count not in fitted.supported_counts:
        return "baseline_unsupported_count"
    if not fitted.fit_record_ids or not set(fitted.fit_record_ids) <= set(bundle.split.fit_ids):
        return "baseline_fitting_provenance_mismatch"
    model = bundle.models.get(state.state_key)
    if model is None:
        return "baseline_unsupported_state"
    stats = model.patterns.get(features.pattern_id)
    if stats is None:
        return "baseline_unknown_pattern"
    reason = support_risk(features, stats, bundle.policy)
    if reason:
        return reason
    if stats.train_count < bundle.policy.min_samples:
        return "baseline_insufficient_fitting_support"
    if current_slot < model.max_slot:
        return "baseline_future_evidence"
    # Constant values were fitted before calibration. Later calibration labels do
    # not renew the age of those values or manufacture new fitting evidence.
    if current_slot - stats.training_max_slot > bundle.policy.max_age_slots:
        return "baseline_stale_fitting_evidence"
    if (
        features.version != fitted.policy.allowed_version
        or features.serialized_size > fitted.policy.wire_bytes_cap
        or features.account_count > fitted.policy.account_cap
    ):
        return "baseline_structural_cap"
    if registry is not None:
        snapshot = registry.cached_deployment_snapshot(
            tuple(bundle.deployment_bindings),
            current_slot=current_slot,
            context=bundle.context,
            cluster_identity=bundle.cluster_identity,
            runtime_identity=bundle.runtime_identity,
        )
        if snapshot["force_simulation"]:
            return "force_simulation"
        if snapshot["status"] != "observed_unreleased":
            return "baseline_deployment_evidence_unavailable"
        if {d["program_id"]: d["fingerprint"] for d in snapshot["deployments"]} != (
            bundle.deployment_bindings
        ):
            return "deployment_changed"
        # The frozen source has its own lifecycle. Another active revision must
        # never authorize this artifact or hide its retirement/quarantine.
        return registry.revision_risk(
            bundle.profile_id(state.state_key),
            bundle.revision_for(state.state_key),
            artifact_sha256=artifact_digest(model.model_dump_json()),
            calibration_min_slot=model.calibration_min_slot,
        )
    return None


def cache_key(state: PayoutStateEnvelope, features: Features) -> str:
    return canonical_digest(
        {
            "state": state.state_key,
            "shape": features.pattern_id,
            "runtime": state.runtime_identity,
            "cluster": state.cluster_identity,
            "deployments": state.deployment_bindings,
        }
    )


def update_cache(
    cache: dict[str, Any],
    state: PayoutStateEnvelope,
    features: Features,
    label: ResourceLabel,
    *,
    observed_slot: int,
) -> dict[str, Any]:
    """Only measured successful paired simulations refresh the cache."""
    result = dict(cache)
    if (
        label.success
        and label.error is None
        and label.compute_units is not None
        and label.loaded_accounts_bytes is not None
    ):
        result[cache_key(state, features)] = {
            "compute_units": label.compute_units,
            "loaded_accounts_bytes": label.loaded_accounts_bytes,
            "observed_slot": observed_slot,
            "uses": 0,
        }
    return result


def baseline_estimate(
    method: str,
    bundle: PayoutModelBundle | None,
    state: PayoutStateEnvelope,
    features: Features,
    *,
    current_slot: int,
    cache: dict[str, Any],
    fitted_baselines: BaselineArtifact | dict[str, Any] | None = None,
    registry: ProfileRegistry | None = None,
    invalidated_keys: Collection[str] = (),
) -> tuple[int, int] | None:
    """Return an estimate, never submission authority or a released prediction.

    ``fixed_batch`` refuses counts above the pre-fit chosen batch. The caller must
    retain that count cap in fallback too, otherwise it is a different baseline.
    Cache use is incremented by the caller only for the selected/executed entry;
    candidate lookups do not spend uses or receive free labels.
    """
    if method == "always_simulate":
        return None
    if (
        state.risk(current_slot=current_slot, deployment_bindings=state.deployment_bindings)
        is not None
    ):
        return None
    if method == "cache":
        entry = cache.get(cache_key(state, features))
        if (
            entry is None
            or not 0 <= current_slot - entry["observed_slot"] <= CACHE_MAX_AGE_SLOTS
            or entry["uses"] >= CACHE_MAX_USES
        ):
            return None
        return (
            rounded_limit(entry["compute_units"], 1000, 100),
            rounded_limit(entry["loaded_accounts_bytes"], 1000, 1024),
        )
    if bundle is None:
        return None
    if (
        state.deployment_bindings != bundle.deployment_bindings
        or state.cluster_identity != bundle.cluster_identity
        or state.runtime_identity != bundle.runtime_identity
    ):
        return None
    if method == "pattern_p99":
        prediction = ResourceEstimator(bundle.pattern_p99).predict(
            features,
            context=bundle.context,
            current_slot=current_slot,
        )
        if prediction.simulation_recommended:
            return None
        assert prediction.compute_unit_limit is not None
        assert prediction.loaded_accounts_data_size_limit is not None
        return prediction.compute_unit_limit, prediction.loaded_accounts_data_size_limit
    if method not in {"fixed_batch", "formula"}:
        raise ValueError("unknown baseline policy: " + method)
    if fitted_baselines is None:
        return None
    fitted = BaselineArtifact.model_validate(fitted_baselines)
    if fitted_baseline_risk(
        method,
        bundle,
        state,
        features,
        current_slot=current_slot,
        fitted_baselines=fitted,
        registry=registry,
        invalidated_keys=invalidated_keys,
    ):
        return None
    if method == "fixed_batch":
        if fitted.fixed_batch_count is None or state.candidate_count > fitted.fixed_batch_count:
            return None
        limits = fitted.fixed_count_limits.get(str(state.candidate_count))
    else:
        limits = fitted.formula(state)
    if limits is None or not (
        0 < limits[0] <= fitted.policy.compute_cap
        and 0 < limits[1] <= fitted.policy.loaded_bytes_cap
    ):
        return None
    return limits
