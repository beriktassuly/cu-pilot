"""Synthetic offline contracts, never deployment or performance evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from solders.pubkey import Pubkey

from cu_pilot.schemas import Observation, ResourceLabel
from examples.payouts.derivation_features import DerivationFeatures, derive_features, run_experiment
from examples.payouts.model import ATA_PROGRAM, LOCAL_POLICY, TOKEN_PROGRAM, PayoutObservation
from tests.test_payout_model import features, state


@pytest.fixture(scope="module")
def synthetic_rows():
    rows = []
    for group in range(80):
        for count in range(1, 9):
            for missing in range(count + 1):
                snapshot = state(
                    group,
                    count=count,
                    missing=missing,
                    remaining=count if count not in (1, 2, 4, 8) else 8,
                    observation_slot=group * 20,
                )
                derived = derive_features(snapshot)
                cu = (
                    5000
                    + 4000 * count
                    + 11000 * missing
                    + 2000 * derived.total_ata_attempts
                    + 3500 * derived.missing_ata_attempts
                )
                identifier = f"synthetic-{group}-{count}-{missing}"
                rows.append(
                    PayoutObservation(
                        record_id=identifier,
                        queue_group=f"group-{group}",
                        state=snapshot,
                        observation=Observation(
                            record_id=identifier,
                            slot=group * 20 + missing,
                            context="synthetic:payout",
                            source="synthetic",
                            label_source="simulation",
                            evidence_origin="synthetic",
                            features=features(count),
                            label=ResourceLabel(
                                success=True,
                                compute_units=cu,
                                loaded_accounts_bytes=4000 + count * 165,
                            ),
                        ),
                    )
                )
    return rows


@pytest.fixture(scope="module")
def evaluated(synthetic_rows):
    return run_experiment(synthetic_rows)


def test_public_derivation_attempts_match_explicit_descending_sdk_search():
    snapshot = state(79, count=8, missing=4)
    derived = derive_features(snapshot)
    attempts = []
    for account, found_bump in zip(snapshot.recipient_accounts, derived.ata_bumps, strict=True):
        seeds = [
            bytes(Pubkey.from_string(account.recipient)),
            bytes(Pubkey.from_string(TOKEN_PROGRAM)),
            bytes(Pubkey.from_string(snapshot.mint)),
        ]
        tried = 0
        for bump in range(255, -1, -1):
            tried += 1
            try:
                address = Pubkey.create_program_address(
                    [*seeds, bytes([bump])], Pubkey.from_string(ATA_PROGRAM)
                )
            except Exception as exc:
                assert "seeds" in str(exc).lower() or "curve" in str(exc).lower()
                continue
            assert str(address) == account.address and bump == found_bump
            break
        assert tried == 256 - found_bump
        attempts.append(tried)
    assert derived.total_ata_attempts == sum(attempts)
    assert derived.missing_ata_attempts == sum(attempts[:4])
    assert max(attempts) > 1


def test_feature_contract_rejects_inconsistent_derived_totals():
    value = derive_features(state(0, count=2)).model_dump()
    with pytest.raises(ValidationError, match="total attempts"):
        DerivationFeatures.model_validate(
            {**value, "total_ata_attempts": value["total_ata_attempts"] + 1}
        )
    with pytest.raises(ValidationError, match="missing account count"):
        DerivationFeatures.model_validate({**value, "missing": 3})


def test_frozen_original_policy_and_disjoint_chronological_cohorts(evaluated):
    artifact, report, trials, derived = evaluated
    assert artifact["release_authority"] is False
    assert artifact["use"] == "offline_research_only"
    assert artifact["policy"] == LOCAL_POLICY.model_dump(mode="json")
    assert report["rows"] == len(derived) == 3520
    assert report["split_counts"] == {"fit": 1760, "calibration": 1056, "holdout": 704}
    assert report["split_cohorts"] == {"fit": 40, "calibration": 24, "holdout": 16}
    assert all(
        overlap == {"cohorts": 0, "recipients": 0}
        for overlap in report["cross_partition_overlap"].values()
    )
    assert len(trials) == 704
    extended = artifact["methods"]["count_missing_derivation"]
    assert extended["coefficients"] == pytest.approx([5000, 4000, 11000, 2000, 3500])
    assert report["methods"]["count_missing_derivation"]["joint_exceedances"] == 0
    assert report["matched_accepted_paired_rows"] > 0
    matched = report["matched_compute_over_allocation"]
    assert matched["count_missing_derivation"]["mean"] < matched["count_missing"]["mean"]


def _replace_labels(rows, identifiers, label):
    return [
        row.model_copy(update={"observation": row.observation.model_copy(update={"label": label})})
        if row.record_id in identifiers
        else row
        for row in rows
    ]


def test_holdout_labels_cannot_change_fit_calibration_or_features(synthetic_rows, evaluated):
    artifact, _, _, features_before = evaluated
    held = set(artifact["split"]["holdout_ids"])
    changed = _replace_labels(
        synthetic_rows,
        held,
        ResourceLabel(success=True, compute_units=1_300_000, loaded_accounts_bytes=60_000),
    )
    result, report, _, features_after = run_experiment(changed)
    assert result["methods"] == artifact["methods"]
    assert result["resource_scope_bundle"] == artifact["resource_scope_bundle"]
    assert features_before == features_after
    assert report["methods"]["count_missing_derivation"]["joint_exceedances"] > 0
    matched = report["matched_exceedances"]["count_missing_derivation"]
    assert matched["paired_denominator"] == matched["joint"] > 0


def test_holdout_effective_label_provenance_must_match_development(synthetic_rows, evaluated):
    artifact, _, _, _ = evaluated
    identifier = artifact["split"]["holdout_ids"][0]
    changed = [
        row.model_copy(
            update={"observation": row.observation.model_copy(update={"label_source": None})}
        )
        if row.record_id == identifier
        else row
        for row in synthetic_rows
    ]
    with pytest.raises(ValueError, match="one original environment and evidence origin"):
        run_experiment(changed)


def test_unknown_calibration_and_holdout_labels_are_retained(synthetic_rows, evaluated):
    artifact, _, _, _ = evaluated
    affected_cell = next(
        row.state.state_key
        for row in synthetic_rows
        if row.state.candidate_count == 1 and row.state.missing_atas == 0
    )
    calibration = set(artifact["split"]["calibration_ids"])
    unknown_ids = {
        row.record_id
        for row in synthetic_rows
        if row.record_id in calibration and row.state.state_key == affected_cell
    }
    unknown_ids = set(sorted(unknown_ids)[:5])
    holdout = set(artifact["split"]["holdout_ids"])
    unknown_holdout = next(
        row.record_id
        for row in synthetic_rows
        if row.record_id in holdout and row.state.candidate_count == 2
    )
    changed = _replace_labels(
        synthetic_rows,
        unknown_ids | {unknown_holdout},
        ResourceLabel(success=False, compute_units=10_000),
    )
    result, report, trials, _ = run_experiment(changed)
    for method, formula in result["methods"].items():
        for key in ("coefficients", "upper_fitting_residual", "feature_ranges", "fitting_support"):
            assert formula[key] == artifact["methods"][method][key]
        cell_key = next(
            key
            for key, support in formula["fitting_support"].items()
            if support["state_key"] == affected_cell
        )
        calibration = formula["calibration"][cell_key]
        assert calibration["eligible_paired_cohorts"] == 19
        assert calibration["unknown_cohorts"] == 5
        assert calibration["qualified"] is False
    unknown = next(row for row in trials if row["record_id"] == unknown_holdout)
    assert unknown["label"]["success"] is False
    assert all(
        d["compute_exceeded"] is None and not d["paired_label"] for d in unknown["methods"].values()
    )
    assert report["partition_labels"]["calibration"]["failed"] == 5
    assert report["partition_labels"]["holdout"]["missing_loaded_data"] == 1
