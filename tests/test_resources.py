import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cu_pilot.resources import ResourceArtifact, ResourceEstimator, ResourcePolicy, rounded_limit
from cu_pilot.schemas import Features, Observation, ResourceLabel


def features(**changes: object) -> Features:
    return Features.model_validate(
        {
            "pattern_id": "shape-v1:paired-fixture",
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
            "requested_compute_units": 1400000,
            "requested_loaded_accounts_bytes": 67108864,
            **changes,
        }
    )


def observations(count: int = 400, *, cu: int = 1000, data: int = 2000) -> list[Observation]:
    return [
        Observation(
            record_id=f"paired-{slot}",
            slot=slot,
            context="synthetic:paired",
            source="synthetic",
            features=features(),
            label=ResourceLabel(compute_units=cu, loaded_accounts_bytes=data, success=True),
        )
        for slot in range(count)
    ]


def test_paired_fit_rounding_and_portable_roundtrip(tmp_path: Path) -> None:
    estimator = ResourceEstimator.fit(observations())
    prediction = estimator.predict(features(), context="synthetic:paired", current_slot=400)
    assert not prediction.simulation_recommended
    assert prediction.compute_unit_limit == 1100
    assert prediction.loaded_accounts_data_size_limit == 3072
    assert prediction.sample_count == 280
    assert prediction.calibration_count == 120
    path = tmp_path / "resource.json"
    estimator.save(path)
    assert ResourceEstimator.load(path).model == estimator.model
    assert json.loads(path.read_text())["max_slot"] == "399"
    assert rounded_limit(100, 1000, 1) == 110
    assert rounded_limit(0, 0, 1024) == 1024


def test_joint_event_counts_disjoint_resource_failures() -> None:
    rows = observations()
    rows[-1] = rows[-1].model_copy(
        update={
            "label": ResourceLabel(compute_units=2000, loaded_accounts_bytes=2000, success=True)
        }
    )
    rows[-2] = rows[-2].model_copy(
        update={
            "label": ResourceLabel(compute_units=1000, loaded_accounts_bytes=4000, success=True)
        }
    )
    stats = ResourceEstimator.fit(rows).model.patterns[features().pattern_id]
    assert stats.calibration_compute_exceedances == 1
    assert stats.calibration_data_exceedances == 1
    assert stats.calibration_joint_exceedances == 2
    # Same-slot rows increase either-resource event once, not sample support.
    rows.append(
        rows[-1].model_copy(
            update={
                "record_id": "same-slot",
                "label": ResourceLabel(
                    compute_units=2000, loaded_accounts_bytes=5000, success=True
                ),
            }
        )
    )
    revised = ResourceEstimator.fit(rows).model.patterns[features().pattern_id]
    assert revised.calibration_joint_exceedances == 2
    assert revised.calibration_count == 120


def test_missing_data_not_imputed_and_boundary_frozen() -> None:
    rows = observations()
    for index in range(280, 399):
        rows[index] = rows[index].model_copy(
            update={"label": ResourceLabel(compute_units=1000, success=True)}
        )
    fitted = ResourceEstimator.fit(rows)
    assert fitted.model.calibration_min_slot == 280
    assert fitted.model.training_max_slot == 279
    assert fitted.model.diagnostics.missing_data_count == 119
    assert fitted.model.patterns[features().pattern_id].calibration_count == 1
    assert (
        fitted.predict(features(), context="synthetic:paired", current_slot=400).reason
        == "insufficient_calibration"
    )


def test_ood_cannot_dilute_joint_risk_or_refresh_evidence() -> None:
    rows = observations(1000)
    for index in range(700, 999):
        rows[index] = rows[index].model_copy(update={"features": features(serialized_size=300)})
    rows[-1] = rows[-1].model_copy(
        update={
            "label": ResourceLabel(compute_units=1000, loaded_accounts_bytes=9000, success=True)
        }
    )
    fitted = ResourceEstimator.fit(rows)
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.calibration_count == stats.calibration_joint_exceedances == 1
    assert fitted.model.diagnostics.ineligible_calibration_count == 299
    assert stats.calibration_upper_bound == pytest.approx(1)


def test_burst_windows_do_not_cross_partitions_or_inflate_support() -> None:
    fitted = ResourceEstimator.fit(observations(), ResourcePolicy(independence_window_slots=10))
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.train_count == 28
    assert stats.calibration_count == 0  # same minimum fitting support gate as inference
    assert fitted.model.training_max_slot == 279
    assert fitted.model.calibration_min_slot == 280


def test_freshness_nonlabel_gate_applies_to_calibration() -> None:
    fitted = ResourceEstimator.fit(observations(), ResourcePolicy(max_age_slots=10))
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.calibration_count == 10
    assert stats.max_slot == 289
    assert (
        fitted.predict(features(), context="synthetic:paired", current_slot=400).reason
        == "stale_pattern"
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"version": 1}, "unsupported_version"),
        ({"risk_flags": ("bad",)}, "risky_features"),
        ({"requested_compute_units": None}, "missing_compute_limit"),
        ({"requested_loaded_accounts_bytes": None}, "missing_loaded_accounts_limit"),
        ({"requested_loaded_accounts_bytes": 0}, "invalid_loaded_accounts_budget"),
        ({"requested_loaded_accounts_bytes": 67108865}, "invalid_loaded_accounts_budget"),
        ({"requested_compute_units": 1400001}, "invalid_compute_budget"),
        ({"pattern_id": "unseen"}, "unknown_pattern"),
        ({"requested_heap_bytes": 32769}, "invalid_heap_budget"),
        ({"serialized_size": None}, "out_of_distribution"),
    ],
)
def test_fallbacks(changes: dict[str, object], reason: str) -> None:
    prediction = ResourceEstimator.fit(observations()).predict(
        features(**changes), context="synthetic:paired", current_slot=400
    )
    assert prediction.reason == reason
    assert (
        prediction.compute_unit_limit is None and prediction.loaded_accounts_data_size_limit is None
    )


def test_v1_requires_explicit_policy_and_full_joint_evidence() -> None:
    rows = [r.model_copy(update={"features": features(version=1)}) for r in observations()]
    with pytest.raises(ValueError, match="no safe paired"):
        ResourceEstimator.fit(rows)
    fitted = ResourceEstimator.fit(rows, ResourcePolicy(allow_v1=True))
    assert not fitted.predict(
        features(version=1), context="synthetic:paired", current_slot=400
    ).simulation_recommended


@pytest.mark.parametrize("slot", [True, 400.0, "400", -1, float("nan"), 2**64])
def test_invalid_current_slots_rejected(slot: object) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        ResourceEstimator.fit(observations()).predict(
            features(), context="synthetic:paired", current_slot=slot
        )  # type: ignore[arg-type]


def test_uint64_slots_survive_json_and_prediction() -> None:
    offset = 2**53 + 100
    rows = [r.model_copy(update={"slot": r.slot + offset}) for r in observations()]
    fitted = ResourceEstimator.fit(rows)
    loaded = ResourceArtifact.model_validate_json(fitted.model.model_dump_json())
    assert loaded.max_slot == offset + 399
    assert (
        not ResourceEstimator(loaded)
        .predict(features(), context="synthetic:paired", current_slot=offset + 400)
        .simulation_recommended
    )


def test_tampering_old_artifact_and_coercion_rejected() -> None:
    artifact = ResourceEstimator.fit(observations()).model.model_dump(mode="json")
    artifact["patterns"][features().pattern_id]["compute_unit_limit"] += 1
    with pytest.raises(ValidationError, match="resource limits"):
        ResourceArtifact.model_validate(artifact)
    artifact["artifact_version"] = "cu-pilot-pattern-estimator-v1"
    with pytest.raises(ValidationError):
        ResourceArtifact.model_validate(artifact)
    for changes in (
        {"allow_v1": "false"},
        {"min_samples": True},
        {"compute_margin_bps": 1.0},
        {"max_age_slots": "01"},
    ):
        with pytest.raises(ValidationError):
            ResourcePolicy.model_validate(changes)


def test_failed_labels_and_conflicting_duplicates_are_not_evidence() -> None:
    rows = observations()
    rows.append(rows[0])
    rows.append(
        rows[-2].model_copy(
            update={
                "record_id": "failure",
                "label": ResourceLabel(
                    compute_units=900000, loaded_accounts_bytes=60000000, success=False
                ),
            }
        )
    )
    fitted = ResourceEstimator.fit(rows)
    assert fitted.model.diagnostics.duplicate_count == 1
    assert fitted.model.diagnostics.failed_count == 1
    rows.append(rows[0].model_copy(update={"slot": 99}))
    with pytest.raises(ValueError, match="Conflicting"):
        ResourceEstimator.fit(rows)


def test_cap_is_not_clamped() -> None:
    fitted = ResourceEstimator.fit(observations(cu=1400000, data=67108864))
    stats = fitted.model.patterns[features().pattern_id]
    assert stats.compute_unit_limit > 1400000
    assert stats.loaded_accounts_data_size_limit > 67108864
    assert fitted.predict(
        features(), context="synthetic:paired", current_slot=400
    ).simulation_recommended


def test_label_source_and_origin_never_mix() -> None:
    rows = observations()
    simulation = [
        r.model_copy(update={"label_source": "simulation", "evidence_origin": "synthetic"})
        for r in rows
    ]
    fitted = ResourceEstimator.fit(simulation)
    assert fitted.model.label_source == "simulation"
    assert fitted.model.evidence_origin == "synthetic"
    with pytest.raises(ValueError, match="provenance"):
        ResourceEstimator.fit(
            simulation
            + [
                rows[0].model_copy(
                    update={
                        "record_id": "historical",
                        "label_source": "historical",
                        "evidence_origin": "synthetic",
                    }
                )
            ]
        )
    with pytest.raises(ValueError, match="provenance"):
        ResourceEstimator.fit(
            simulation
            + [
                rows[0].model_copy(
                    update={"record_id": "unknown-origin", "label_source": "simulation"}
                )
            ]
        )


def test_resource_range_does_not_coerce_boolean_or_fraction() -> None:
    raw = ResourceEstimator.fit(observations()).model.model_dump(mode="json")
    raw["patterns"][features().pattern_id]["numerical_ranges"]["signature_count"]["minimum"] = True
    with pytest.raises(ValidationError):
        ResourceArtifact.model_validate(raw)
