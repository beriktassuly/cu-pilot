"""Synthetic formula fitting and state-binding tests; no live qualification evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cu_pilot.formula import FormulaArtifact, FormulaEstimator, FormulaInputs
from cu_pilot.resources import ResourcePolicy
from cu_pilot.schemas import Observation, ResourceLabel
from examples.payouts.derivation_features import derive_features
from examples.payouts.hybrid import (
    PayoutHybridBundle,
    fit_hybrid,
    inputs_for,
    qualify_hybrid_bundle,
)
from examples.payouts.model import PayoutObservation, PayoutStateEnvelope
from tests.test_payout_model import DEPLOYMENTS, features, state

TEST_POLICY = ResourcePolicy(
    min_samples=2,
    min_calibration_samples=2,
    max_joint_underestimation_rate=0.8,
    compute_margin_bps=0,
    data_margin_bps=0,
)


def make_hybrid_rows(groups: int = 80) -> list[PayoutObservation]:
    result = []
    for group in range(groups):
        for count in (1, 2, 4, 8):
            for missing in range(count + 1):
                snapshot = state(group, count=count, missing=missing, observation_slot=group * 20)
                vector = derive_features(snapshot)
                label = ResourceLabel(
                    success=True,
                    compute_units=(
                        5000
                        + 4000 * count
                        + 11000 * missing
                        + 2000 * vector.total_ata_attempts
                        + 3500 * vector.missing_ata_attempts
                    ),
                    loaded_accounts_bytes=4000 + 165 * count,
                )
                identity = f"hybrid-{group}-{count}-{missing}"
                result.append(
                    PayoutObservation(
                        record_id=identity,
                        queue_group=f"group-{group}",
                        state=snapshot,
                        observation=Observation(
                            record_id=identity,
                            slot=group * 20 + missing,
                            context="synthetic:payout",
                            source="synthetic",
                            label_source="simulation",
                            evidence_origin="synthetic",
                            features=features(count),
                            label=label,
                        ),
                    )
                )
    return result


@pytest.fixture(scope="module")
def hybrid_rows():
    return make_hybrid_rows()


@pytest.fixture(scope="module")
def hybrid_bundle(hybrid_rows):
    return fit_hybrid(hybrid_rows, policy=TEST_POLICY)


def test_coefficients_and_distinct_independent_partitions(hybrid_bundle):
    bundle = hybrid_bundle
    assert (
        len(bundle.split.fit_cohorts),
        len(bundle.split.calibration_cohorts),
        len(bundle.split.qualification_cohorts),
    ) == (40, 24, 16)
    model = next(iter(bundle.models.values()))
    assert model.compute_coefficients == pytest.approx((5000, 4000, 11000, 2000, 3500), abs=1e-6)
    assert model.data_coefficients == pytest.approx((4000, 165, 0, 0, 0), abs=1e-6)
    assert len(model.calibration_scores) == 24
    assert model.correction < 1e-8
    assert bundle.diagnostics["untouched_comparison_required"] is True
    assert all(m.qualified_for_activation() for m in bundle.models.values())


def test_qualification_labels_cannot_change_fit_or_conformal_correction(hybrid_rows, hybrid_bundle):
    qualification = set(hybrid_bundle.split.qualification_ids)
    changed = []
    for row in hybrid_rows:
        if row.record_id in qualification:
            label = row.observation.label.model_copy(update={"compute_units": 1_200_000})
            row = row.model_copy(
                update={"observation": row.observation.model_copy(update={"label": label})}
            )
        changed.append(row)
    new = fit_hybrid(changed, policy=TEST_POLICY)
    for key, model in new.models.items():
        old = hybrid_bundle.models[key]
        assert model.compute_coefficients == old.compute_coefficients
        assert model.data_coefficients == old.data_coefficients
        assert model.calibration_scores == old.calibration_scores
        assert model.correction == old.correction
        assert not model.qualified_for_activation()


def test_missing_or_failed_calibration_labels_abort_instead_of_filtering(
    hybrid_rows, hybrid_bundle
):
    identifier = hybrid_bundle.split.calibration_ids[0]
    changed = [
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "label": row.observation.label.model_copy(
                            update={"loaded_accounts_bytes": None}
                        )
                    }
                )
            }
        )
        if row.record_id == identifier
        else row
        for row in hybrid_rows
    ]
    with pytest.raises(ValueError, match="missing paired"):
        fit_hybrid(changed, policy=TEST_POLICY)


def test_duplicated_rows_do_not_inflate_cohort_count(hybrid_rows, hybrid_bundle):
    duplicate = fit_hybrid(hybrid_rows + hybrid_rows, policy=TEST_POLICY)
    assert duplicate.split == hybrid_bundle.split
    for key in hybrid_bundle.models:
        assert duplicate.models[key] == hybrid_bundle.models[key]


def test_rare_target_without_enough_groups_abstains(hybrid_rows):
    bundle = fit_hybrid(hybrid_rows, policy=TEST_POLICY, alpha=0.01)
    assert all(
        m.correction is None and not m.qualified_for_activation() for m in bundle.models.values()
    )


def test_state_binding_support_freshness_and_model_copy_isolation(hybrid_bundle):
    bundle = hybrid_bundle
    slot = max(m.max_slot for m in bundle.models.values()) + 10
    snapshot = state(1, count=2, observation_slot=slot)
    feature = features(2)
    _, estimator, _ = bundle.estimator_for(
        snapshot,
        current_slot=slot,
        deployment_bindings=DEPLOYMENTS,
        prepared_identity=snapshot.prepared_identity,
        features=feature,
    )
    assert estimator is not None
    assert not estimator.predict(
        feature, context=bundle.context, current_slot=slot
    ).simulation_recommended
    estimator.validate_binding(snapshot.prepared_identity)
    with pytest.raises(ValueError, match="message_mismatch"):
        estimator.validate_binding("other-message")
    unbound = FormulaEstimator(estimator.model)
    assert (
        unbound.predict(feature, context=bundle.context, current_slot=slot).reason
        == "formula_state_unbound"
    )
    assert (
        estimator.predict(feature, context=bundle.context, current_slot=slot + 9).reason
        == "stale_state"
    )
    exported = estimator.model
    exported.patterns.clear()
    assert estimator.model.patterns
    altered = feature.model_copy(update={"account_count": feature.account_count + 1})
    assert (
        estimator.predict(altered, context=bundle.context, current_slot=slot).reason
        == "formula_features_mismatch"
    )
    changed = inputs_for(snapshot, feature).model_dump()
    changed["recipients"][0]["address"] = snapshot.mint
    with pytest.raises(ValidationError, match="canonical ATA"):
        FormulaInputs.model_validate(changed)


def test_fit_support_rejects_unseen_derivation_effort(hybrid_bundle):
    bundle = hybrid_bundle
    model = next(m for m in bundle.models.values() if m.cell_key.startswith("n1-m0-"))
    stats = next(iter(model.patterns.values()))
    vector = (1, 1, 0, (stats.vector_ranges[3].maximum or 0) + 1, 0)
    assert stats.risk(features(1), vector, model.policy) == "formula_out_of_support"


def test_artifact_rejects_tampered_quantile_and_overlapping_cohorts(hybrid_bundle):
    model = next(iter(hybrid_bundle.models.values()))
    value = model.model_dump()
    with pytest.raises(ValidationError, match="order statistic"):
        FormulaArtifact.model_validate({**value, "correction": 123.0})
    with pytest.raises(ValidationError, match="overlap"):
        FormulaArtifact.model_validate({**value, "qualification_cohorts": model.fit_cohorts})


def test_bundle_roundtrip_and_synthetic_evidence_cannot_release(hybrid_bundle, tmp_path):
    path = tmp_path / "hybrid.json"
    hybrid_bundle.save(path)
    assert PayoutHybridBundle.load(path) == hybrid_bundle
    with pytest.raises(ValueError, match="actual local-runtime"):
        qualify_hybrid_bundle(
            hybrid_bundle, None, deployments=[], dependencies={}, current_slot=2000
        )


def test_shared_recipient_groups_cannot_leak_across_split(hybrid_rows):
    # Deliberately reuse every cohort's original recipient public keys; joins must
    # collapse the cohorts rather than pretend repeated observations are independent.
    changed = []
    first_by_cell = {r.state.state_key: r.state for r in hybrid_rows if r.queue_group == "group-0"}
    for row in hybrid_rows:
        values = row.state.model_dump()
        values["recipient_accounts"] = first_by_cell[row.state.state_key].recipient_accounts
        snapshot = PayoutStateEnvelope.seal(**values)
        changed.append(row.model_copy(update={"state": snapshot}))
    with pytest.raises(ValueError, match="three separate cohorts"):
        fit_hybrid(changed, policy=TEST_POLICY)


def test_public_formula_inputs_cannot_be_relabelled_for_another_transaction(hybrid_bundle):
    from cu_pilot.schemas import Account, Instruction, TransactionInput

    slot = max(m.max_slot for m in hybrid_bundle.models.values()) + 1
    target = state(5, count=8, observation_slot=slot)
    other = state(4, count=8, observation_slot=slot)
    other = PayoutStateEnvelope.seal(
        **{**other.model_dump(), "prepared_identity": target.prepared_identity}
    )
    keys = [target.mint]
    for recipient in target.recipient_accounts:
        keys.extend((recipient.recipient, recipient.address))
    transaction = TransactionInput(
        version="legacy",
        signature_count=0,
        accounts=tuple(Account(pubkey=key, signer=False, writable=False) for key in keys),
        instructions=(
            Instruction(
                program_id=next(iter(DEPLOYMENTS)), accounts=tuple(range(len(keys))), data_hex="03"
            ),
        ),
    )
    model = hybrid_bundle.models[target.state_key]
    valid = FormulaEstimator(model, inputs_for(target, features(8)))
    valid.validate_binding(target.prepared_identity, transaction=transaction)
    forged = FormulaEstimator(model, inputs_for(other, features(8)))
    with pytest.raises(ValueError, match="formula_accounts_mismatch"):
        forged.validate_binding(target.prepared_identity, transaction=transaction)
    # A key merely listed in a message but absent from the actual instruction is
    # not evidence that it belongs to the resource-estimated operation.
    unrelated = transaction.model_copy(
        update={
            "instructions": (transaction.instructions[0].model_copy(update={"accounts": (0,)}),)
        }
    )
    with pytest.raises(ValueError, match="formula_instruction_accounts_mismatch"):
        valid.validate_binding(target.prepared_identity, transaction=unrelated)
