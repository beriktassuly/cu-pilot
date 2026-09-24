import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cu_pilot.estimator import PatternEstimator, Policy, empirical_quantile, wilson_upper_bound
from cu_pilot.schemas import Features, Observation, ResourceLabel


def features(**changes: object) -> Features:
    values: dict[str, object] = {
        "pattern_id": "shape-v1:transfer",
        "version": "legacy",
        "signature_count": 1,
        "account_count": 3,
        "signer_count": 1,
        "writable_count": 2,
        "instruction_count": 1,
        "program_ids": ("11111111111111111111111111111111",),
        "instruction_data_lengths": (12,),
        "total_instruction_data_bytes": 12,
        "lookup_table_count": 0,
        "lookup_writable_count": 0,
        "lookup_readonly_count": 0,
        "serialized_size": 215,
        "requested_compute_units": 1_400_000,
    }
    values.update(changes)
    return Features.model_validate(values)


def observations(count: int = 400, units: int = 100_000) -> list[Observation]:
    return [
        Observation(
            record_id=f"fixture-{slot}",
            slot=slot,
            context="synthetic:epoch-1",
            source="synthetic",
            features=features(),
            label=ResourceLabel(compute_units=units, success=True),
        )
        for slot in range(count)
    ]


@pytest.fixture
def fitted() -> PatternEstimator:
    return PatternEstimator.fit(observations())


def test_nearest_rank_quantiles_and_decimal_margin() -> None:
    assert empirical_quantile(list(range(1, 101)), 0.95) == 95
    assert empirical_quantile(list(range(1, 101)), 0.99) == 99
    assert empirical_quantile([5], 0.99) == 5
    fitted = PatternEstimator.fit(observations(units=100))
    assert (
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=400).compute_unit_limit
        == 110
    )
    with pytest.raises(ValueError):
        empirical_quantile([], 0.99)


def test_wilson_bound_is_conservative_and_one_sided() -> None:
    assert wilson_upper_bound(0, 100) == pytest.approx(0.02634272, abs=1e-7)
    assert wilson_upper_bound(0, 30) > 0.05
    assert wilson_upper_bound(2, 100) > wilson_upper_bound(0, 100)
    assert wilson_upper_bound(100, 100) == pytest.approx(1)
    with pytest.raises(ValueError):
        wilson_upper_bound(0, 0)


def test_prediction_returns_calibration_evidence(fitted: PatternEstimator) -> None:
    prediction = fitted.predict(features(), context="synthetic:epoch-1", current_slot=400)
    assert not prediction.simulation_recommended
    assert prediction.compute_unit_limit == 110_000
    assert prediction.sample_count == 280
    assert prediction.calibration_count == 120
    assert prediction.calibration_underestimation_upper_bound is not None
    assert prediction.calibration_underestimation_upper_bound < 0.05
    assert prediction.loaded_accounts_data_size_limit is None


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"pattern_id": "unknown"}, "unknown_pattern"),
        ({"version": 1}, "unsupported_version"),
        ({"risk_flags": ("unresolved_lookup",)}, "risky_features"),
        ({"requested_loaded_accounts_bytes": 1_000_000}, "loaded_accounts_limit"),
        ({"requested_loaded_accounts_bytes": 100_000_000}, "loaded_accounts_limit"),
        ({"signature_count": 0}, "incomplete_features"),
        ({"requested_compute_units": 0}, "invalid_compute_budget"),
        ({"requested_compute_units": None}, "missing_compute_limit"),
        ({"requested_heap_bytes": 32_769}, "invalid_heap_budget"),
        ({"account_count": 4}, "out_of_distribution"),
        ({"serialized_size": None}, "out_of_distribution"),
        ({"requested_heap_bytes": 65_536}, "out_of_distribution"),
        ({"program_ids": ("different",)}, "shape_mismatch"),
        (
            {"instruction_data_lengths": (13,), "total_instruction_data_bytes": 13},
            "out_of_distribution",
        ),
    ],
)
def test_feature_fallbacks(
    fitted: PatternEstimator, changes: dict[str, object], expected: str
) -> None:
    prediction = fitted.predict(features(**changes), context="synthetic:epoch-1", current_slot=400)
    assert prediction.reason == expected
    assert prediction.simulation_recommended
    assert prediction.compute_unit_limit is None


def test_context_age_and_backwards_guards(fitted: PatternEstimator) -> None:
    assert (
        fitted.predict(features(), context="other", current_slot=400).reason == "context_mismatch"
    )
    assert (
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=398).reason
        == "backwards_slot"
    )
    assert (
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=216_400).reason
        == "stale_pattern"
    )


def test_insufficient_training_and_calibration() -> None:
    fitted = PatternEstimator.fit(observations(10))
    assert (
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=10).reason
        == "insufficient_training"
    )
    fitted = PatternEstimator.fit(observations(100))
    assert (
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=100).reason
        == "insufficient_calibration"
    )


def test_calibration_labels_never_increase_training_quantile() -> None:
    rows = observations()
    rows = [
        row.model_copy(update={"label": ResourceLabel(compute_units=200_000, success=True)})
        if row.slot >= 280
        else row
        for row in rows
    ]
    fitted = PatternEstimator.fit(rows)
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.quantile_units == 100_000
    assert stats.proposed_limit == 110_000
    assert stats.calibration_underestimations == 120
    assert (
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=400).reason
        == "calibration_risk"
    )


def test_numerical_support_uses_training_only() -> None:
    rows = [
        row.model_copy(update={"features": features(serialized_size=300)})
        if row.slot >= 280
        else row
        for row in observations()
    ]
    fitted = PatternEstimator.fit(rows)
    assert (
        fitted.predict(
            features(serialized_size=300), context="synthetic:epoch-1", current_slot=400
        ).reason
        == "out_of_distribution"
    )


def test_deduplication_and_distinct_slot_support() -> None:
    rows = observations()
    # Extra transactions in an already observed slot cannot inflate support.
    rows += [row.model_copy(update={"record_id": row.record_id + "-second"}) for row in rows]
    rows += rows[:30]
    fitted = PatternEstimator.fit(rows)
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.train_count == 280
    assert stats.calibration_count == 120
    assert stats.training_max_slot == 279
    assert stats.calibration_min_slot == 280
    assert fitted.model.diagnostics.duplicate_count == 30
    rows.append(rows[0].model_copy(update={"slot": 500}))
    with pytest.raises(ValueError, match="conflicting observations"):
        PatternEstimator.fit(rows)


def test_slot_maximum_counts_calibration_underestimate_once() -> None:
    rows = observations()
    rows.append(
        rows[-1].model_copy(
            update={
                "record_id": "same-slot-higher",
                "label": ResourceLabel(compute_units=120_000, success=True),
            }
        )
    )
    fitted = PatternEstimator.fit(rows)
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.calibration_count == 120
    assert stats.calibration_underestimations == 1


@pytest.mark.parametrize(
    ("units", "reason"), [(1_200_000, "near_compute_limit"), (1_300_000, "hard_compute_limit")]
)
def test_near_and_hard_limits_are_never_clamped(units: int, reason: str) -> None:
    fitted = PatternEstimator.fit(observations(units=units))
    prediction = fitted.predict(features(), context="synthetic:epoch-1", current_slot=400)
    assert prediction.reason == reason
    assert prediction.compute_unit_limit is None


def test_discard_failed_missing_invalid_and_risky_rows() -> None:
    rows = observations()
    variants = [
        {"label": ResourceLabel(compute_units=999_999, success=False)},
        {"label": ResourceLabel(compute_units=999_999, success=True, error={"failure": 1})},
        {"label": ResourceLabel(compute_units=None, success=True)},
        {"label": ResourceLabel(compute_units=1_500_000, success=True)},
        {"features": features(risk_flags=("bad",))},
    ]
    for index, variant in enumerate(variants):
        rows.append(rows[0].model_copy(update={"record_id": f"excluded-{index}", **variant}))
    fitted = PatternEstimator.fit(rows)
    diagnostics = fitted.model.diagnostics
    assert diagnostics.failed_count == 2
    assert diagnostics.missing_label_count == 1
    assert diagnostics.invalid_label_count == 1
    assert diagnostics.unsafe_feature_count == 1
    assert diagnostics.usable_count == 400
    assert fitted.model.patterns[features().pattern_id].quantile_units == 100_000


def test_do_not_mix_contexts_or_sources() -> None:
    rows = observations(2)
    with pytest.raises(ValueError, match="one context"):
        PatternEstimator.fit(rows + [rows[0].model_copy(update={"context": "mainnet"})])
    with pytest.raises(ValueError, match="provenance"):
        PatternEstimator.fit(rows + [rows[0].model_copy(update={"source": "historical"})])
    with pytest.raises(ValueError, match="requires observations"):
        PatternEstimator.fit([])


def test_one_slot_cannot_calibrate_itself() -> None:
    fitted = PatternEstimator.fit(observations(1))
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.train_count == 1
    assert stats.calibration_count == 0
    assert stats.calibration_upper_bound is None


def test_new_calibration_pattern_stays_unknown() -> None:
    rows = observations()
    rows.append(
        rows[-1].model_copy(
            update={
                "record_id": "new-pattern",
                "features": features(pattern_id="new-pattern"),
            }
        )
    )
    fitted = PatternEstimator.fit(rows)
    assert fitted.model.diagnostics.calibration_only_pattern_count == 1
    assert "new-pattern" not in fitted.model.patterns


def test_json_roundtrip_and_tamper_validation(fitted: PatternEstimator, tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    fitted.save(path)
    loaded = PatternEstimator.load(path)
    assert loaded.model == fitted.model
    assert loaded.predict(
        features(), context="synthetic:epoch-1", current_slot=400
    ) == fitted.predict(features(), context="synthetic:epoch-1", current_slot=400)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["patterns"][features().pattern_id]["proposed_limit"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="proposed limit"):
        PatternEstimator.load(path)
    data["artifact_version"] = "v9000"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError):
        PatternEstimator.load(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"quantile": 0},
        {"quantile": float("nan")},
        {"safety_margin": -0.1},
        {"min_samples": 0},
        {"min_calibration_samples": 0},
        {"max_underestimation_rate": 1},
        {"max_age_slots": -1},
        {"near_limit_ratio": 1.1},
        {"calibration_fraction": 1},
    ],
)
def test_policy_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Policy.model_validate(changes)


def test_ood_calibration_rows_cannot_dilute_eligible_failures() -> None:
    rows = [
        row.model_copy(update={"features": features(serialized_size=None)})
        for row in observations(1_000, units=1_000)
    ]
    for index in range(700, 999):
        rows[index] = rows[index].model_copy(update={"features": features(serialized_size=250)})
    rows[-1] = rows[-1].model_copy(
        update={
            "label": ResourceLabel(compute_units=5_000, success=True),
        }
    )
    fitted = PatternEstimator.fit(rows)
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.train_count == 700
    assert stats.calibration_count == 1
    assert stats.calibration_underestimations == 1
    assert stats.calibration_upper_bound == pytest.approx(1)
    assert fitted.model.diagnostics.ineligible_calibration_count == 299
    prediction = fitted.predict(
        features(serialized_size=None), context="synthetic:epoch-1", current_slot=1_000
    )
    assert prediction.reason == "insufficient_calibration"
    assert prediction.compute_unit_limit is None


@pytest.mark.parametrize(
    "feature_changes",
    [
        {"risk_flags": ("unsafe",)},
        {"version": 1},
        {"requested_loaded_accounts_bytes": 1_000_000},
    ],
)
def test_unsafe_calibration_rows_cannot_shift_chronological_boundary(
    feature_changes: dict[str, object],
) -> None:
    rows = observations(1_000, units=1_000)
    for index in range(700, 999):
        rows[index] = rows[index].model_copy(update={"features": features(**feature_changes)})
    rows[-1] = rows[-1].model_copy(
        update={
            "label": ResourceLabel(compute_units=5_000, success=True),
        }
    )
    fitted = PatternEstimator.fit(rows)
    stats = fitted.model.patterns[features().pattern_id]
    assert fitted.model.training_max_slot == 699
    assert fitted.model.calibration_min_slot == 700
    assert stats.train_count == 700
    assert stats.calibration_count == 1
    assert stats.calibration_underestimations == 1
    assert fitted.model.diagnostics.unsafe_feature_count == 299
    assert (
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=1_000).reason
        == "insufficient_calibration"
    )


def test_calibration_eligibility_precedes_per_slot_maximum() -> None:
    rows = observations()
    rows.append(
        rows[-1].model_copy(
            update={
                "record_id": "same-slot-ood",
                "features": features(serialized_size=300),
                "label": ResourceLabel(compute_units=500_000, success=True),
            }
        )
    )
    fitted = PatternEstimator.fit(rows)
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.calibration_count == 120
    assert stats.calibration_underestimations == 0
    assert fitted.model.diagnostics.ineligible_calibration_count == 1


def test_ood_calibration_cannot_refresh_pattern() -> None:
    rows = [
        row.model_copy(update={"features": features(serialized_size=300)})
        if row.slot >= 280
        else row
        for row in observations()
    ]
    fitted = PatternEstimator.fit(rows, Policy(max_age_slots=100))
    assert fitted.model.patterns[features().pattern_id].max_slot == 279
    assert (
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=400).reason
        == "stale_pattern"
    )


def test_no_compute_limit_rows_fit_but_never_calibrate_or_accept() -> None:
    rows = [
        row.model_copy(update={"features": features(requested_compute_units=None)})
        for row in observations()
    ]
    fitted = PatternEstimator.fit(rows)
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.train_count == 280
    assert stats.calibration_count == 0
    assert fitted.model.diagnostics.ineligible_calibration_count == 120
    assert (
        fitted.predict(
            features(requested_compute_units=None), context="synthetic:epoch-1", current_slot=400
        ).reason
        == "missing_compute_limit"
    )


def test_replacing_existing_compute_limit_preserves_acceptance(fitted: PatternEstimator) -> None:
    original = fitted.predict(features(), context="synthetic:epoch-1", current_slot=400)
    assert original.compute_unit_limit is not None
    changed = fitted.predict(
        features(requested_compute_units=original.compute_unit_limit),
        context="synthetic:epoch-1",
        current_slot=400,
    )
    assert changed == original


@pytest.mark.parametrize("current_slot", [float("nan"), float("inf"), 400.0, True, -1, "400"])
def test_prediction_rejects_invalid_slot_types(
    fitted: PatternEstimator, current_slot: object
) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        fitted.predict(features(), context="synthetic:epoch-1", current_slot=current_slot)
