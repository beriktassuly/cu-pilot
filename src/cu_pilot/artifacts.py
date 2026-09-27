"""Versioned paired-resource artifact dispatch; model families keep distinct contracts."""

from __future__ import annotations

import json

from cu_pilot.formula import FormulaArtifact, FormulaEstimator
from cu_pilot.resources import ResourceArtifact, ResourceEstimator, limit_risk

Artifact = ResourceArtifact | FormulaArtifact
Estimator = ResourceEstimator | FormulaEstimator


def parse_artifact(payload: str | bytes) -> Artifact:
    """Validate every byte against one explicitly supported immutable artifact schema."""
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("resource artifact must be an object")
    if raw.get("artifact_version", "cu-pilot-resources-v1") == "cu-pilot-resources-v1":
        return ResourceArtifact.model_validate_json(payload)
    # Formula validation rejects unknown versions and offline experiment reports.
    return FormulaArtifact.model_validate_json(payload)


def artifact_estimator(model: Artifact) -> Estimator:
    """Formula releases remain unbound until supplied exact pre-execution evidence."""
    if isinstance(model, FormulaArtifact):
        return FormulaEstimator(model)
    return ResourceEstimator(model)


def qualified_for_activation(model: Artifact) -> bool:
    if isinstance(model, FormulaArtifact):
        return model.qualified_for_activation()
    return any(
        stats.train_count >= model.policy.min_samples
        and stats.calibration_count >= model.policy.min_calibration_samples
        and stats.calibration_upper_bound is not None
        and stats.calibration_upper_bound <= model.policy.max_joint_underestimation_rate
        and limit_risk(stats, model.policy) is None
        for stats in model.patterns.values()
    )
