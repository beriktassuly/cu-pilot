import pytest
from test_resources import features, observations

from cu_pilot.resource_evaluation import PreparationTrace, evaluate_resources, preparation_report
from cu_pilot.resources import ResourcePolicy
from cu_pilot.schemas import ResourceLabel


def test_competitors_and_full_policy_require_lifecycle_evidence() -> None:
    rows = observations(1000)
    report = evaluate_resources(rows)
    assert report["methods"]["joint_statistical_policy"]["accepted"] == 200
    assert report["methods"]["complete_policy"]["accepted"] == 0
    assert report["fallback_reasons"] == {"lifecycle_evidence_unavailable": 200}
    assert report["measured_preparation"]["p95_ms"] is None
    report = evaluate_resources(
        rows, lifecycle_eligibility=lambda f, s: None, control_probability=1
    )
    complete = report["methods"]["complete_policy"]
    assert complete["accepted"] == complete["counterfactual_control_simulation_calls"] == 200
    assert complete["counterfactual_net_calls_avoided"] == 0
    assert complete["measured_avoided_calls"] is None
    assert complete["preflight_or_validation_calls_avoided"] == 0
    assert report["control_sampling"]["selected"] == 200
    assert report["methods"]["last_successful_cache"]["fallback"] > 0


def test_control_excess_suspends_later_decisions_only() -> None:
    rows = observations(1000)
    rows[800] = rows[800].model_copy(
        update={
            "label": ResourceLabel(compute_units=1000, loaded_accounts_bytes=9000, success=True)
        }
    )
    report = evaluate_resources(
        rows, lifecycle_eligibility=lambda f, s: None, control_probability=1
    )
    assert report["methods"]["complete_policy"]["accepted"] == 1
    assert report["methods"]["complete_policy"]["joint_underestimations"] == 1
    assert report["methods"]["joint_statistical_policy"]["accepted"] == 200
    assert report["fallback_reasons"] == {"control_excess_suspended": 199}


def test_holdout_labels_do_not_tune_limits_and_missing_denominator_visible() -> None:
    rows = observations(1000)
    rows[801] = rows[801].model_copy(
        update={"label": ResourceLabel(compute_units=1000, success=True)}
    )
    report = evaluate_resources(
        rows, lifecycle_eligibility=lambda f, s: None, control_probability=0
    )
    assert report["test_missing_data_count"] == 1
    assert report["methods"]["complete_policy"]["paired_scored"] == 199
    assert report["methods"]["complete_policy"]["accepted_failed_or_unpaired"] == 1
    for i in range(802, 1000):
        rows[i] = rows[i].model_copy(
            update={
                "label": ResourceLabel(
                    compute_units=999999, loaded_accounts_bytes=999999, success=True
                )
            }
        )
    other = evaluate_resources(rows, lifecycle_eligibility=lambda f, s: None, control_probability=0)
    assert other["methods"]["complete_policy"]["accepted"] == 200
    assert other["methods"]["complete_policy"]["joint_underestimations"] == 198
    assert (
        other["patterns"][0]["calibration_joint_upper_bound"]
        == report["patterns"][0]["calibration_joint_upper_bound"]
    )


def test_evidence_windows_frozen_and_duplicates_conflict() -> None:
    rows = observations(1000)
    report = evaluate_resources(
        rows + rows[:10],
        policy=ResourcePolicy(independence_window_slots=2, min_calibration_samples=50),
    )
    assert report["unique_count"] == 1000
    assert int(report["training_max_slot"]) // 2 < int(report["calibration_min_slot"]) // 2
    assert int(report["calibration_min_slot"]) // 2 < int(report["test_min_slot"]) // 2
    with pytest.raises(ValueError, match="Conflicting"):
        evaluate_resources(rows + [rows[0].model_copy(update={"slot": 2})])


def test_measured_latency_uses_whole_samples_and_real_call_denominators() -> None:
    traces = [
        PreparationTrace(
            observation_id=f"id-{i}",
            evidence="local-runtime",
            mode="shadow",
            total_ms=float(i),
            inference_ms=0.1,
            state_read_ms=0.2,
            logging_ms=0.1,
            resource_estimation_calls=1,
            preflight_calls=1,
            validation_simulation_calls=1,
            state_reads=2,
        )
        for i in range(1, 101)
    ]
    report = preparation_report(traces + traces[:1])
    assert report["p50_ms"] == 50
    assert report["p95_ms"] == 95
    assert report["p99_ms"] == 99
    assert report["resource_estimation_calls"] == 100
    assert report["preflight_calls"] == report["validation_simulation_calls"] == 100
    assert report["state_reads"] == 200
    with pytest.raises(ValueError, match="conflicting"):
        preparation_report(traces + [traces[0].model_copy(update={"total_ms": 5.0})])
    with pytest.raises(ValueError, match="separately"):
        preparation_report(
            traces
            + [
                traces[0].model_copy(
                    update={"observation_id": "different", "evidence": "synthetic"}
                )
            ]
        )


def test_caller_baseline_validated_and_training_traces_rejected() -> None:
    report = evaluate_resources(observations(1000), caller_policy=lambda f, s: (1200, 4000))
    assert report["methods"]["caller_policy"]["accepted"] == 200
    with pytest.raises(ValueError, match="baseline limits"):
        evaluate_resources(observations(1000), caller_policy=lambda f, s: (2000000, 4000))
    trace = PreparationTrace(
        observation_id="paired-2",
        evidence="synthetic",
        mode="shadow",
        total_ms=1,
        resource_estimation_calls=1,
    )
    with pytest.raises(ValueError, match="frozen test"):
        evaluate_resources(observations(1000), preparation_traces=[trace])


def test_unknown_pattern_and_failed_labels_stay_in_test_denominator() -> None:
    rows = observations(1000)
    rows[-1] = rows[-1].model_copy(
        update={
            "features": features(pattern_id="unknown"),
            "label": ResourceLabel(compute_units=500, loaded_accounts_bytes=500, success=False),
        }
    )
    report = evaluate_resources(
        rows, lifecycle_eligibility=lambda f, s: None, control_probability=0
    )
    assert report["test_count"] == 200
    assert report["test_paired_count"] == 199
    assert report["fallback_reasons"] == {"unknown_pattern": 1}
    assert report["methods"]["always_simulate"]["joint_underestimation_rate"] is None
