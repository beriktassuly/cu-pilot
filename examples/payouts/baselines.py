"""Application baseline estimates; uncertainty follows the same bounded fallback."""

from __future__ import annotations

from typing import Any

from cu_pilot.resources import ResourceEstimator, rounded_limit
from cu_pilot.schemas import Features, ResourceLabel
from examples.payouts.model import PayoutModelBundle, PayoutStateEnvelope, canonical_digest
from examples.payouts.planning import BaselineArtifact

CACHE_MAX_AGE_SLOTS = 40
CACHE_MAX_USES = 8


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
    if fitted.bundle_digest != bundle.digest:
        raise ValueError("baseline artifact belongs to a different frozen fit/holdout split")
    if method == "fixed_batch":
        if fitted.fixed_batch_count is None or state.candidate_count > fitted.fixed_batch_count:
            return None
        return fitted.fixed_count_limits.get(str(state.candidate_count))
    return fitted.formula(state)
