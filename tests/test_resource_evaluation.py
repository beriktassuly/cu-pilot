import pytest
from test_resources import features, observations

from cu_pilot.resource_evaluation import (
    PreparationTrace,
    configured_priority_fee,
    evaluate_resources,
    preparation_report,
)
from cu_pilot.resources import ResourcePolicy
from cu_pilot.schemas import ResourceLabel


def test_priority_fee_rounding_precision_and_v1_absolute_fee() -> None:
    assert configured_priority_fee(features(requested_micro_lamports=1), 100) == 1
    assert configured_priority_fee(features(requested_micro_lamports=200), 100_000) == 20
    high = 2**64 - 1
    assert configured_priority_fee(features(requested_micro_lamports=high), 1_400_000) == high
    v1 = features(version=1, requested_priority_fee_lamports=high)
    assert configured_priority_fee(v1, 100) == configured_priority_fee(v1, 1_400_000) == high
    assert configured_priority_fee(features(), 100) == 0
    invalid = features(risk_flags=("duplicate_compute_budget_instruction",))
    assert configured_priority_fee(invalid, 100) is None


def test_fee_report_uses_final_limits_and_exact_decimal_strings() -> None:
    rows = [
        row.model_copy(update={"features": features(requested_micro_lamports=2**53 + 1)})
        for row in observations(1000)
    ]
    report = evaluate_resources(rows)
    fees = report["methods"]["joint_statistical_policy"]["configured_priority_fees"]
    expected = (2**53 + 1) * 1100
    expected = (expected + 999_999) // 1_000_000
    assert fees["by_version"]["legacy"]["total_lamports"] == str(200 * expected)
    assert fees["observed_fee_savings_lamports"] is None
    assert report["methods"]["always_simulate"]["configured_priority_fees"]["by_version"] == {}


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


@pytest.mark.parametrize("threshold", [1, 3])
@pytest.mark.parametrize(
    "label",
    [
        ResourceLabel(compute_units=2000, loaded_accounts_bytes=9000, success=False),
        ResourceLabel(compute_units=1000, success=True),
        ResourceLabel(compute_units=1000, loaded_accounts_bytes=2000, success=True, error="error"),
    ],
)
def test_failed_or_incomplete_controls_apply_released_failure_threshold(
    threshold: int, label: ResourceLabel
) -> None:
    rows = observations(1000)
    for index in range(800, 800 + threshold):
        rows[index] = rows[index].model_copy(update={"label": label})
    report = evaluate_resources(
        rows,
        lifecycle_eligibility=lambda f, s: None,
        control_probability=1,
        max_control_failure_streak=threshold,
    )
    assert report["methods"]["complete_policy"]["accepted"] == threshold
    assert report["fallback_reasons"] == {"control_deterioration_suspended": 200 - threshold}
    assert report["control_sampling"]["failed_or_incomplete"] == threshold
    assert report["control_sampling"]["final_failure_streak"] == threshold
    assert report["control_sampling"]["known_resource_excesses"] == 0


@pytest.mark.parametrize(
    "label",
    [
        ResourceLabel(compute_units=2000, success=True),
        ResourceLabel(loaded_accounts_bytes=9000, success=True),
    ],
)
def test_known_partial_control_excess_suspends_without_paired_label(label: ResourceLabel) -> None:
    rows = observations(1000)
    rows[800] = rows[800].model_copy(update={"label": label})
    report = evaluate_resources(
        rows, lifecycle_eligibility=lambda f, s: None, control_probability=1
    )
    assert report["methods"]["complete_policy"]["accepted"] == 1
    assert report["control_sampling"]["paired_scored"] == 0
    assert report["control_sampling"]["joint_exceedances"] == 0
    assert report["control_sampling"]["known_resource_excesses"] == 1
    assert report["control_sampling"]["failed_or_incomplete"] == 1
    assert report["fallback_reasons"] == {"control_excess_suspended": 199}


def test_complete_success_resets_failure_streak_and_unselected_labels_are_unobserved() -> None:
    rows = observations(1000)
    for index in (800, 801, 803, 804):
        rows[index] = rows[index].model_copy(update={"label": ResourceLabel(success=False)})
    report = evaluate_resources(
        rows, lifecycle_eligibility=lambda f, s: None, control_probability=1
    )
    assert report["methods"]["complete_policy"]["accepted"] == 200
    assert report["control_sampling"]["failed_or_incomplete"] == 4
    assert report["control_sampling"]["final_failure_streak"] == 0
    assert report["control_sampling"]["suspension_reason"] is None
    unobserved = evaluate_resources(
        rows,
        lifecycle_eligibility=lambda f, s: None,
        control_probability=0,
        max_control_failure_streak=1,
    )
    assert unobserved["methods"]["complete_policy"]["accepted"] == 200
    assert unobserved["control_sampling"]["failed_or_incomplete"] == 0


def test_control_state_is_release_wide_and_does_not_leak_within_slot() -> None:
    rows = observations(1000)
    rows += [
        row.model_copy(
            update={
                "record_id": row.record_id + "-other",
                "features": features(pattern_id="shape-v1:second-pattern"),
            }
        )
        for row in rows
    ]
    rows[800] = rows[800].model_copy(update={"label": ResourceLabel(success=False)})
    report = evaluate_resources(
        rows,
        lifecycle_eligibility=lambda f, s: None,
        control_probability=1,
        max_control_failure_streak=1,
    )
    # Both decisions in slot 800 precede either control outcome. The successful
    # other-pattern outcome resets the streak but cannot undo quarantine.
    assert report["methods"]["complete_policy"]["accepted"] == 2
    assert report["fallback_reasons"] == {"control_deterioration_suspended": 398}
    assert {pattern["metrics"]["accepted"] for pattern in report["patterns"]} == {1}
    assert report["control_sampling"]["final_failure_streak"] == 0
    assert report["control_sampling"]["state_scope"] == "single_evaluated_profile_revision"


@pytest.mark.parametrize("threshold", [True, 0, -1, 1.5, 2**32])
def test_invalid_control_failure_threshold_is_rejected(threshold: object) -> None:
    with pytest.raises(ValueError, match="failure threshold"):
        evaluate_resources(observations(1000), max_control_failure_streak=threshold)  # type: ignore[arg-type]


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


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("evidence", "live-simulation", "evidence origin"),
        ("mode", "deployment", "collection semantics"),
        ("collection_method", "offline-replay", "collection semantics"),
    ],
)
def test_trace_join_rejects_mismatched_origin_mode_and_method(
    field: str, value: str, error: str
) -> None:
    rows = [
        row.model_copy(update={"collection_method": "prospective", "collection_mode": "shadow"})
        for row in observations(1000)
    ]
    trace = PreparationTrace(
        observation_id="paired-999",
        evidence="synthetic",
        mode="shadow",
        total_ms=2,
        resource_estimation_calls=1,
    )
    good = evaluate_resources(rows, preparation_traces=[trace])
    assert good["measured_preparation"]["measured_count"] == 1
    assert good["measured_preparation"]["p50_ms"] == 2
    with pytest.raises(ValueError, match=error):
        evaluate_resources(rows, preparation_traces=[trace.model_copy(update={field: value})])


def test_legacy_observations_need_explicit_collection_semantics_for_timing_join() -> None:
    trace = PreparationTrace(
        observation_id="paired-999",
        evidence="synthetic",
        mode="shadow",
        total_ms=2,
        resource_estimation_calls=1,
    )
    rows = observations(1000)
    assert evaluate_resources(rows)["test_count"] == 200
    with pytest.raises(ValueError, match="declared observation collection semantics"):
        evaluate_resources(rows, preparation_traces=[trace])
    # Independently measured timing datasets remain reportable without asserting
    # that ID equality establishes a join to these labels.
    assert preparation_report([trace])["p50_ms"] == 2


def test_historical_label_preserves_original_preparation_provenance() -> None:
    rows = [
        row.model_copy(
            update={
                "source": "historical",
                "label_source": "historical",
                "evidence_origin": "local-runtime",
                "collection_method": "prospective",
                "collection_mode": "shadow",
            }
        )
        for row in observations(1000)
    ]
    trace = PreparationTrace(
        observation_id="paired-999",
        evidence="local-runtime",
        mode="shadow",
        total_ms=2,
        resource_estimation_calls=1,
    )
    report = evaluate_resources(rows, preparation_traces=[trace])
    assert report["label_source"] == "historical"
    assert report["measured_preparation"]["evidence"] == "local-runtime"
    assert report["measured_preparation"]["mode"] == "shadow"
    unknown_origin = [row.model_copy(update={"evidence_origin": None}) for row in rows]
    with pytest.raises(ValueError, match="evidence origin"):
        evaluate_resources(unknown_origin, preparation_traces=[trace])


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
