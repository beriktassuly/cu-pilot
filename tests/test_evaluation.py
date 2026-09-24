import pytest

from cu_pilot.data import split_by_slot, unique_observations
from cu_pilot.demo import make_demo
from cu_pilot.evaluation import evaluate, summarize


def test_gated_demo_rejects_drift_without_changing_test_fit() -> None:
    rows = make_demo()
    report = evaluate(rows)
    assert report["development_max_slot"] < report["test_min_slot"]
    gated = report["methods"]["cu_pilot_gated"]
    assert gated["coverage"] == pytest.approx(2 / 3)
    assert gated["underestimation_rate"] == 0
    assert report["methods"]["pattern_p99_margin_ungated"]["underestimation_rate"] > 0.3
    changed = [
        row.model_copy(update={"label": row.label.model_copy(update={"compute_units": 1_000_000})})
        if row.slot >= report["test_min_slot"]
        else row
        for row in rows
    ]
    other = evaluate(changed)
    assert other["methods"]["cu_pilot_gated"]["coverage"] == gated["coverage"]
    assert other["methods"]["cu_pilot_gated"]["underestimation_rate"] == 1


def test_unscored_is_null_and_metrics_denominators_are_explicit() -> None:
    rows = make_demo(30)[:3]
    unscored = summarize(rows, [None] * 3, simulation_ms=100, inference_ms=1)
    assert unscored["underestimation_rate"] is None
    assert unscored["coverage"] == 0
    assert unscored["assumed_ms_saved_per_transaction"] == -1
    actual = rows[0].label.compute_units
    assert actual is not None
    metrics = summarize(rows, [actual - 1, None, None], simulation_ms=100, inference_ms=0)
    assert metrics["underestimation_rate"] == 1
    assert metrics["mean_excess_cu"] == 0
    assert metrics["scored_predictions"] == 1


def test_dedup_and_slot_groups_do_not_cross_split() -> None:
    rows = make_demo(30)
    assert len(unique_observations(rows + rows)) == 30
    with pytest.raises(ValueError, match="Conflicting"):
        unique_observations(rows + [rows[0].model_copy(update={"slot": 999})])
    paired = [r.model_copy(update={"slot": r.slot // 2}) for r in rows]
    left, right = split_by_slot(paired, 0.5)
    assert max(r.slot for r in left) < min(r.slot for r in right)


def test_contradictory_error_and_invalid_labels_are_not_scored() -> None:
    rows = make_demo(30)[:3]
    labels = [
        rows[0].label.model_copy(update={"error": "failure"}),
        rows[1].label.model_copy(update={"compute_units": 0}),
        rows[2].label.model_copy(update={"compute_units": 1_400_001}),
    ]
    rows = [
        row.model_copy(update={"label": label}) for row, label in zip(rows, labels, strict=True)
    ]
    report = summarize(rows, [1000] * 3, simulation_ms=100, inference_ms=0)
    assert report["scored_predictions"] == 0
    assert report["underestimation_rate"] is None
    assert report["accepted_failed_or_unlabeled_count"] == 3
