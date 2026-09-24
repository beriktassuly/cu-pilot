"""Chronological comparison with explicit denominators and assumed RPC latency."""

import math
import statistics
import time
from collections import Counter
from typing import Any

from cu_pilot.data import split_by_slot, unique_observations
from cu_pilot.estimator import PatternEstimator, Policy
from cu_pilot.schemas import MAX_COMPUTE_UNITS, Observation, usable_compute_label


def quantile(values: list[int], probability: float) -> int:
    if not values:
        raise ValueError("Cannot take a quantile of empty data")
    return sorted(values)[max(0, math.ceil(probability * len(values)) - 1)]


def summarize(
    rows: list[Observation], limits: list[int | None], *, simulation_ms: float, inference_ms: float
) -> dict[str, Any]:
    if len(rows) != len(limits) or not rows:
        raise ValueError("Metrics need aligned, nonempty observations and predictions")
    accepted = sum(limit is not None for limit in limits)
    scored = [
        (row.label.compute_units, limit)
        for row, limit in zip(rows, limits, strict=True)
        if limit is not None
        and usable_compute_label(row.label)
        and row.label.compute_units is not None
    ]
    excess = [max(0, limit - actual) for actual, limit in scored]
    under = sum(limit < actual for actual, limit in scored)
    coverage = accepted / len(rows)
    preparation = inference_ms + (1 - coverage) * simulation_ms
    return {
        "total": len(rows),
        "accepted": accepted,
        "scored_predictions": len(scored),
        "accepted_failed_or_unlabeled_count": accepted - len(scored),
        "underestimations": under,
        "underestimation_rate": under / len(scored) if scored else None,
        "mean_excess_cu": statistics.mean(excess) if excess else None,
        "median_excess_cu": statistics.median(excess) if excess else None,
        "p95_excess_cu": quantile(excess, 0.95) if excess else None,
        "coverage": coverage,
        "fallback_rate": 1 - coverage,
        "assumed_preparation_ms": preparation,
        "assumed_ms_saved_per_transaction": simulation_ms - preparation,
        "measured_inference_ms_per_transaction": inference_ms,
    }


def evaluate(
    observations: list[Observation],
    *,
    test_fraction: float = 0.2,
    policy: Policy | None = None,
    simulation_ms: float = 100,
) -> dict[str, Any]:
    if not math.isfinite(simulation_ms) or simulation_ms < 0:
        raise ValueError("Simulation latency must be finite and nonnegative")
    policy = policy or Policy()
    rows = unique_observations(observations)
    if (
        len({row.context for row in rows}) != 1
        or len({row.source for row in rows}) != 1
        or len({r.label_source or r.source for r in rows}) != 1
        or len({r.evidence_origin for r in rows}) != 1
    ):
        raise ValueError("Evaluation requires one context and one label source")
    development, test = split_by_slot(rows, 1 - test_fraction)
    estimator = PatternEstimator.fit(development, policy=policy)
    p95 = PatternEstimator.fit(development, policy=policy.model_copy(update={"quantile": 0.95}))
    p99 = PatternEstimator.fit(development, policy=policy.model_copy(update={"quantile": 0.99}))
    started = time.perf_counter()
    predictions = [
        estimator.predict(r.features, context=r.context, current_slot=r.slot) for r in test
    ]
    inference_ms = (time.perf_counter() - started) * 1000 / len(test)
    fitted, _ = split_by_slot(development, 1 - policy.calibration_fraction)
    labels = [
        r.label.compute_units
        for r in fitted
        if usable_compute_label(r.label) and r.label.compute_units is not None
    ]
    global_limit = (
        min(MAX_COMPUTE_UNITS, math.ceil(quantile(labels, 0.99) * (1 + policy.safety_margin)))
        if labels
        else None
    )
    methods: dict[str, list[int | None]] = {
        "always_simulate": [None] * len(test),
        "fixed_max_cu": [MAX_COMPUTE_UNITS] * len(test),
        "global_p99_margin": [global_limit] * len(test),
    }
    for name, model in [("pattern_p95_margin_ungated", p95), ("pattern_p99_margin_ungated", p99)]:
        methods[name] = [
            min(MAX_COMPUTE_UNITS, model.model.patterns[r.features.pattern_id].proposed_limit)
            if r.features.pattern_id in model.model.patterns
            else None
            for r in test
        ]
    methods["cu_pilot_gated"] = [
        p.compute_unit_limit if not p.simulation_recommended else None for p in predictions
    ]
    metrics = {
        name: summarize(
            test,
            limits,
            simulation_ms=simulation_ms,
            inference_ms=inference_ms if name == "cu_pilot_gated" else 0,
        )
        for name, limits in methods.items()
    }
    per_pattern = []
    for pattern_id in sorted({r.features.pattern_id for r in test}):
        positions = [i for i, r in enumerate(test) if r.features.pattern_id == pattern_id]
        per_pattern.append(
            {
                "pattern_id": pattern_id,
                **summarize(
                    [test[i] for i in positions],
                    [methods["cu_pilot_gated"][i] for i in positions],
                    simulation_ms=simulation_ms,
                    inference_ms=inference_ms,
                ),
                "reasons": dict(Counter(predictions[i].reason for i in positions)),
            }
        )
    return {
        "report_version": 1,
        "context": rows[0].context,
        "source": rows[0].source,
        "input_count": len(observations),
        "unique_count": len(rows),
        "development_count": len(development),
        "test_count": len(test),
        "development_max_slot": max(r.slot for r in development),
        "test_min_slot": min(r.slot for r in test),
        "failed_or_unlabeled_test_count": sum(not usable_compute_label(r.label) for r in test),
        "policy": policy.model_dump(),
        "assumed_simulation_ms": simulation_ms,
        "methods": metrics,
        "patterns": per_pattern,
        "fallback_reasons": dict(
            Counter(p.reason for p in predictions if p.simulation_recommended)
        ),
        "limitations": [
            "Synthetic results validate plumbing, not production accuracy.",
            "Underestimation uses successful labeled accepted predictions; zero scored is null.",
            "Coverage includes all test rows. Failed transactions are not successful CU labels.",
            "Excess is max(limit - actual, 0); fallback resource usage is not imputed.",
            "Always-simulate is unscored: historical replay is not a simulation oracle.",
            "Latency assumes supplied RPC time plus measured inference; baselines omit overhead.",
            "Calibration counts distinct slots, which are not guaranteed independent trials.",
            "Ungated and fixed baselines may be unsafe; CU coverage is not total-resource safety.",
        ],
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# CU Pilot evaluation",
        "",
        f"Source: **{report['source']}**. Synthetic data is not a production benchmark.",
        "",
        f"Chronological holdout: {report['development_count']} development / "
        f"{report['test_count']} test rows.",
        "",
        "| Method | Coverage | Underestimation | Mean excess CU | p95 excess CU | ms saved* |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, metric in report["methods"].items():
        under = metric["underestimation_rate"]
        formatted_under = "n/a" if under is None else f"{under:.2%}"
        lines.append(
            f"| {name} | {metric['coverage']:.1%} | {formatted_under} | "
            f"{metric['mean_excess_cu']} | {metric['p95_excess_cu']} | "
            f"{metric['assumed_ms_saved_per_transaction']:.2f} |"
        )
    lines.extend(["", "Fallback reasons: " + str(report["fallback_reasons"]), ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"
