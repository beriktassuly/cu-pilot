"""Synthetic, offline tests of the application learner; never runtime evidence."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from solders.pubkey import Pubkey

from cu_pilot.lifecycle import DeploymentEvidence, ProfileRegistry
from cu_pilot.resources import ResourceEstimator, ResourcePolicy
from cu_pilot.schemas import Features, Observation, ResourceLabel
from examples.payouts.baselines import baseline_estimate, cache_key, update_cache
from examples.payouts.model import (
    TOKEN_PROGRAM,
    PayoutModelBundle,
    PayoutObservation,
    PayoutStateEnvelope,
    RecipientAccountState,
    evaluate_bundle,
    fit_bundle,
    grouped_split,
    qualify_bundle,
    recipient_ata,
)
from examples.payouts.planning import (
    BaselineArtifact,
    CandidateEstimate,
    choose_candidate,
    eligible_counts,
    fit_baselines,
    fixed_ablation_estimate,
)


def key(index: int) -> str:
    return str(Pubkey.from_bytes(index.to_bytes(32, "little")))


MINT = key(5)
DEPLOYMENTS = {key(7): "1" * 64, key(1): "2" * 64}
POLICY = ResourcePolicy(
    min_samples=2,
    min_calibration_samples=2,
    max_joint_underestimation_rate=0.6,
    compute_margin_bps=1000,
    data_margin_bps=1000,
)


def state(group: int, missing: int = 0, count: int = 2, **updates: object) -> PayoutStateEnvelope:
    recipients = tuple(
        RecipientAccountState(
            address=recipient_ata(key(100 + group * 20 + i), MINT),
            recipient=key(100 + group * 20 + i),
            exists=i >= missing,
            data_bytes=0 if i < missing else 165,
            initialized=i >= missing,
            frozen=False,
            token_mint=None if i < missing else MINT,
            token_authority=None if i < missing else key(100 + group * 20 + i),
            program_owner=None if i < missing else TOKEN_PROGRAM,
        )
        for i in range(count)
    )
    return PayoutStateEnvelope.seal(
        **{
            "queue_address": key(10000 + group),
            "queue_identity": f"queue-{group}",
            "mint": MINT,
            "executor": key(6),
            "cursor": 0,
            "remaining": 8,
            "candidate_count": count,
            "queue_data_bytes": 900,
            "recipient_accounts": recipients,
            "deployment_bindings": DEPLOYMENTS,
            "cluster_identity": "synthetic-cluster",
            "runtime_identity": "synthetic-runtime",
            "observation_slot": group * 10,
            "prepared_identity": f"message-{group}-{count}",
            "approved_payments_digest": "3" * 64,
            **updates,
        }
    )


def features(count: int = 2) -> Features:
    return Features(
        pattern_id=f"shape-v1:payout-count-{count}",
        version="legacy",
        signature_count=1,
        account_count=8 + count * 2,
        signer_count=1,
        writable_count=4 + count,
        instruction_count=3,
        program_ids=(key(1), key(1), key(7)),
        instruction_data_lengths=(5, 5, 71),
        total_instruction_data_bytes=81,
        lookup_table_count=0,
        lookup_writable_count=0,
        lookup_readonly_count=0,
        serialized_size=400 + count * 64,
        requested_compute_units=1400000,
        requested_loaded_accounts_bytes=67108864,
    )


def rows() -> list[PayoutObservation]:
    result = []
    for group in range(20):
        for missing in (0, 1, 2):
            envelope = state(group, missing)
            record_id = f"group-{group}-missing-{missing}"
            result.append(
                PayoutObservation(
                    record_id=record_id,
                    queue_group=f"group-{group}",
                    state=envelope,
                    observation=Observation(
                        record_id=record_id,
                        slot=group * 10 + missing,
                        context="synthetic:payout",
                        source="synthetic",
                        label_source="simulation",
                        evidence_origin="synthetic",
                        collection_method="prospective",
                        collection_mode="shadow",
                        features=features(),
                        label=ResourceLabel(
                            compute_units=2000 + missing * 10000,
                            loaded_accounts_bytes=4000 - missing * 165,
                            success=True,
                        ),
                    ),
                )
            )
    return result


def test_learned_state_quantiles_use_real_label_parameters_and_reuse_core(tmp_path: Path) -> None:
    observations = rows()
    bundle = fit_bundle(observations, policy=POLICY)
    assert len(bundle.models) == 3
    assert bundle.release_status == "candidate"
    assert bundle.diagnostics["fit_count"] == 30
    assert bundle.diagnostics["calibration_count"] == 18
    assert bundle.diagnostics["holdout_count"] == 12
    for cell in bundle.models:
        actual_min = min(
            row.observation.slot
            for row in observations
            if row.state.state_key == cell
            and row.record_id in bundle.split.fit_ids + bundle.split.calibration_ids
        )
        assert bundle.diagnostics["cell_evidence_min_slots"][cell] == str(actual_min)
    low = bundle.predict(state(20), features(), current_slot=200, deployment_bindings=DEPLOYMENTS)
    high = bundle.predict(
        state(20, 2), features(), current_slot=200, deployment_bindings=DEPLOYMENTS
    )
    assert not low.simulation_recommended and not high.simulation_recommended
    assert low.compute_unit_limit == 2200
    assert high.compute_unit_limit == 24200
    assert low.compute_unit_limit < high.compute_unit_limit
    _, selected, _ = bundle.estimator_for(
        state(20), current_slot=200, deployment_bindings=DEPLOYMENTS
    )
    assert type(selected) is ResourceEstimator
    path = tmp_path / "candidate.json"
    bundle.save(path)
    assert PayoutModelBundle.load(path) == bundle
    report = evaluate_bundle(bundle, observations)
    assert report["methods"]["state_conditioned_quantiles"]["accepted"] == 12
    assert report["methods"]["state_conditioned_quantiles"]["joint_underestimations"] == 0
    assert (
        report["methods"]["state_conditioned_quantiles"]["compute_over_allocation"]["mean"]
        < (report["methods"]["pattern_p99"]["compute_over_allocation"]["mean"])
    )


def test_grouped_holdout_never_changes_fitted_parameters() -> None:
    observations = rows()
    fitted = fit_bundle(observations, policy=POLICY)
    held_out = set(fitted.split.holdout_ids)
    changed = [
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "label": ResourceLabel(
                            compute_units=999999, loaded_accounts_bytes=6000000, success=True
                        ),
                    }
                )
            }
        )
        if row.record_id in held_out
        else row
        for row in observations
    ]
    assert fit_bundle(changed, policy=POLICY) == fitted
    assert (
        evaluate_bundle(fitted, changed)["methods"]["state_conditioned_quantiles"][
            "joint_underestimations"
        ]
        == 12
    )


def test_pre_execution_features_reject_post_execution_leakage_and_digest_tampering() -> None:
    envelope = state(0)
    with pytest.raises(ValidationError, match="Extra inputs"):
        PayoutStateEnvelope.model_validate({**envelope.model_dump(), "compute_units": 123})
    with pytest.raises(ValidationError, match="digest mismatch"):
        PayoutStateEnvelope.model_validate({**envelope.model_dump(), "cursor": 1})
    assert state(20).state_key == envelope.state_key
    assert state(20).snapshot_digest != envelope.snapshot_digest


@pytest.mark.parametrize(
    ("changes", "slot", "bindings", "identity", "reason"),
    [
        ({}, 209, DEPLOYMENTS, None, "stale_state"),
        ({}, 199, DEPLOYMENTS, None, "future_state"),
        ({}, 200, {key(7): "2" * 64}, None, "deployment_changed"),
        ({}, 200, DEPLOYMENTS, "wrong-message", "state_message_mismatch"),
        ({"vault_frozen": True}, 200, DEPLOYMENTS, None, "unsupported_token_state"),
        ({"paused": True}, 200, DEPLOYMENTS, None, "queue_not_executable"),
        ({"runtime_identity": "different"}, 200, DEPLOYMENTS, None, "runtime_mismatch"),
    ],
)
def test_state_gates(
    changes: dict[str, object],
    slot: int,
    bindings: dict[str, str],
    identity: str | None,
    reason: str,
) -> None:
    bundle = fit_bundle(rows(), policy=POLICY)
    _, estimator, actual = bundle.estimator_for(
        state(20, **changes),
        current_slot=slot,
        deployment_bindings=bindings,
        prepared_identity=identity,
    )
    assert estimator is None and actual == reason


def test_unseen_count_never_inherits_a_neighbor_profile() -> None:
    bundle = fit_bundle(rows(), policy=POLICY)
    result = bundle.predict(
        state(20, count=1), features(1), current_slot=200, deployment_bindings=DEPLOYMENTS
    )
    assert result.simulation_recommended and result.reason == "unseen_count_or_state"


def test_nonmenu_count_is_only_supported_for_an_exact_tail() -> None:
    partial = state(20, count=3)
    assert partial.risk(current_slot=200, deployment_bindings=DEPLOYMENTS) == (
        "unsupported_nonterminal_count"
    )
    tail = state(20, count=3, remaining=3)
    assert tail.risk(current_slot=200, deployment_bindings=DEPLOYMENTS) is None


def test_frozen_or_substituted_recipient_is_unsupported() -> None:
    envelope = state(20)
    accounts = list(envelope.recipient_accounts)
    accounts[0] = accounts[0].model_copy(update={"frozen": True})
    frozen = PayoutStateEnvelope.seal(
        **{
            **envelope.model_dump(),
            "recipient_accounts": accounts,
        }
    )
    assert frozen.risk(current_slot=200, deployment_bindings=DEPLOYMENTS) == (
        "unsupported_recipient_state"
    )
    accounts[0] = envelope.recipient_accounts[0].model_copy(update={"address": key(90)})
    substitute = PayoutStateEnvelope.seal(
        **{
            **envelope.model_dump(),
            "recipient_accounts": accounts,
        }
    )
    assert substitute.risk(current_slot=200, deployment_bindings=DEPLOYMENTS) == (
        "unsupported_recipient_state"
    )


def test_duplicate_observations_are_idempotent_and_conflicts_rejected() -> None:
    observations = rows()
    assert fit_bundle(observations + [observations[0]], policy=POLICY).models == (
        fit_bundle(observations, policy=POLICY).models
    )
    with pytest.raises(ValueError, match="conflicting"):
        fit_bundle(
            observations + [observations[0].model_copy(update={"queue_group": "changed"})],
            policy=POLICY,
        )


def test_repeated_queue_and_interleaved_slot_windows_do_not_split() -> None:
    observations = rows()
    # A late retry of group 0 joins all overlapping queue intervals through group 3.
    retry = observations[0].model_copy(
        update={
            "record_id": "retry",
            "observation": observations[0].observation.model_copy(
                update={
                    "record_id": "retry",
                    "slot": 35,
                }
            ),
        }
    )
    split = grouped_split(observations + [retry])
    partitions = [set(split.fit_ids), set(split.calibration_ids), set(split.holdout_ids)]
    for group in range(20):
        ids = {r.record_id for r in observations + [retry] if r.queue_group == f"group-{group}"}
        assert sum(bool(ids & partition) for partition in partitions) == 1
    assert all(f"group-{group}-missing-0" in split.fit_ids for group in range(4))


def test_missing_data_is_not_filled_from_compute_or_account_sizes() -> None:
    observations = rows()
    missing = [
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "label": ResourceLabel(compute_units=1000, success=True),
                    }
                )
            }
        )
        if row.state.missing_atas == 2
        else row
        for row in observations
    ]
    bundle = fit_bundle(missing, policy=POLICY)
    assert state(20, 2).state_key not in bundle.models
    assert bundle.diagnostics["missing_data_development_count"] == 16


def test_core_explicit_calibration_boundary_preserves_group_partition() -> None:
    observations = [r.observation for r in rows()]
    fitted = ResourceEstimator.fit(observations, POLICY, calibration_boundary_slot=120)
    assert fitted.model.training_max_slot == 112
    assert fitted.model.calibration_min_slot == 120
    assert next(iter(fitted.model.patterns.values())).train_count == 36
    with pytest.raises(ValueError, match="divides an evidence window"):
        ResourceEstimator.fit(
            observations,
            POLICY.model_copy(
                update={
                    "independence_window_slots": 10,
                }
            ),
            calibration_boundary_slot=111,
        )


def test_mixed_simulation_and_execution_labels_are_not_independent_samples() -> None:
    observations = rows()
    historical = observations[0].model_copy(
        update={
            "observation": observations[0].observation.model_copy(
                update={"label_source": "historical"}
            ),
        }
    )
    with pytest.raises(ValueError, match="provenance"):
        fit_bundle([historical, *observations[1:]], policy=POLICY)


def varied_rows() -> list[PayoutObservation]:
    result = []
    for group in range(20):
        for count in (1, 2, 4):
            for missing in range(count + 1):
                envelope = state(group, missing, count)
                identifier = f"group-{group}-n{count}-m{missing}"
                result.append(
                    PayoutObservation(
                        record_id=identifier,
                        queue_group=f"group-{group}",
                        state=envelope,
                        observation=Observation(
                            record_id=identifier,
                            slot=group * 10 + missing,
                            context="synthetic:payout",
                            source="synthetic",
                            label_source="simulation",
                            evidence_origin="synthetic",
                            features=features(count),
                            label=ResourceLabel(
                                success=True,
                                compute_units=5000 + 4000 * count + 11000 * missing,
                                loaded_accounts_bytes=4000 + count * 165,
                            ),
                        ),
                    )
                )
    return result


def test_baselines_fit_only_and_equal_frozen_constraints(tmp_path: Path) -> None:
    observations = varied_rows()
    bundle = fit_bundle(observations, policy=POLICY)
    fitted = fit_baselines(bundle, observations)
    assert fitted.fixed_batch_count == 4
    assert fitted.formula_coefficients == pytest.approx((5000, 4000, 11000))
    path = tmp_path / "baselines.json"
    fitted.save(path)
    assert BaselineArtifact.load(path) == fitted
    changed = [
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "label": ResourceLabel(
                            success=True, compute_units=999999, loaded_accounts_bytes=8000
                        ),
                    }
                )
            }
        )
        if row.record_id in bundle.split.holdout_ids
        else row
        for row in observations
    ]
    assert fit_baselines(bundle, changed) == fitted
    candidates = []
    ablation = []
    for count in (1, 2, 4):
        candidate = state(20, count=count)
        limits = baseline_estimate(
            "formula",
            bundle,
            candidate,
            features(count),
            current_slot=200,
            cache={},
            fitted_baselines=fitted,
        )
        assert limits is not None
        candidates.append(
            CandidateEstimate(
                count=count,
                compute_units=limits[0],
                loaded_bytes=limits[1],
                serialized_size=700,
                account_count=20,
                source="formula",
                reason="fit",
            )
        )
        fixed = fixed_ablation_estimate(count)
        ablation.append(
            candidates[-1].model_copy(update={"compute_units": fixed[0], "loaded_bytes": fixed[1]})
        )
    assert choose_candidate(candidates, remaining=8).count == 4
    assert choose_candidate(ablation, remaining=8).count == 2
    assert eligible_counts(7) == (1, 2, 4, 7)
    assert eligible_counts(16) == (1, 2, 4, 8)
    assert (
        choose_candidate(
            [c.model_copy(update={"serialized_size": 1233}) for c in candidates], remaining=8
        )
        is None
    )


def test_cache_needs_successful_paired_measurements_and_bounded_freshness() -> None:
    envelope = state(20)
    fixture = features()
    missing = update_cache(
        {}, envelope, fixture, ResourceLabel(success=True, compute_units=5000), observed_slot=200
    )
    assert missing == {}
    failed = update_cache(
        {},
        envelope,
        fixture,
        ResourceLabel(success=False, compute_units=5000, loaded_accounts_bytes=5000),
        observed_slot=200,
    )
    assert failed == {}
    cache = update_cache(
        {},
        envelope,
        fixture,
        ResourceLabel(success=True, compute_units=5000, loaded_accounts_bytes=5000),
        observed_slot=199,
    )
    assert baseline_estimate("cache", None, envelope, fixture, current_slot=200, cache=cache) == (
        5500,
        6144,
    )
    assert (
        baseline_estimate("cache", None, state(24), fixture, current_slot=240, cache=cache) is None
    )
    cache[cache_key(envelope, fixture)]["uses"] = 8
    assert (
        baseline_estimate("cache", None, envelope, fixture, current_slot=200, cache=cache) is None
    )


def test_bundle_rejects_misreported_cell_policy() -> None:
    bundle = fit_bundle(rows(), policy=POLICY)
    payload = bundle.model_dump(mode="json")
    chosen = next(iter(payload["models"]))
    payload["models"][chosen]["policy"]["max_joint_underestimation_rate"] = 0.7
    with pytest.raises(ValidationError, match="declared bundle policy"):
        PayoutModelBundle.model_validate(payload)


def test_bundle_requires_valid_earliest_evidence_metadata() -> None:
    bundle = fit_bundle(rows(), policy=POLICY)
    payload = bundle.model_dump(mode="json")
    chosen = next(iter(payload["models"]))
    payload["diagnostics"]["cell_evidence_min_slots"][chosen] = str(
        bundle.models[chosen].training_max_slot + 1
    )
    with pytest.raises(ValidationError, match="must not follow"):
        PayoutModelBundle.model_validate(payload)
    del payload["diagnostics"]["cell_evidence_min_slots"][chosen]
    with pytest.raises(ValidationError, match="every fitted cell"):
        PayoutModelBundle.model_validate(payload)


def test_capacity_sensitivity_only_describes_paired_holdout_without_retuning() -> None:
    observations = rows()
    bundle = fit_bundle(observations, policy=POLICY)
    original_digest = bundle.digest
    holdout_ids = bundle.split.holdout_ids
    changed_labels = dict(
        zip(
            holdout_ids,
            [
                ResourceLabel(success=True, compute_units=200000, loaded_accounts_bytes=4000),
                ResourceLabel(success=True, compute_units=10000, loaded_accounts_bytes=1048577),
                ResourceLabel(success=True, compute_units=2000),
                ResourceLabel(success=False, compute_units=2000, loaded_accounts_bytes=4000),
            ],
            strict=False,
        )
    )
    changed = [
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={"label": changed_labels[row.record_id]}
                )
            }
        )
        if row.record_id in changed_labels
        else row
        for row in observations
    ]
    sensitivity = evaluate_bundle(bundle, changed)["capacity_sensitivity"]
    assert sensitivity["holdout_rows"] == 12
    assert sensitivity["successful_paired_labels"] == 10
    assert sensitivity["failed_or_unpaired_rows"] == 2
    assert sensitivity["declared_policy"]["compute_cap"] == 100000
    assert sensitivity["declared_policy"]["within_both"] == 8
    assert sensitivity["protocol_compute_ceiling"]["compute_cap"] == 1400000
    assert sensitivity["protocol_compute_ceiling"]["within_both"] == 9
    assert sensitivity["by_candidate_count"]["2"]["maximum_observed_compute_units"] == 200000
    assert sensitivity["all_observed_count8_pairs_fit_protocol_ceiling"] is None
    assert bundle.digest == original_digest
    assert fit_bundle(changed, policy=POLICY).digest == original_digest


def test_prefunded_system_ata_state_cannot_receive_a_missing_ata_profile() -> None:
    absent = state(20, missing=1)
    accounts = list(absent.recipient_accounts)
    accounts[0] = accounts[0].model_copy(
        update={"program_owner": "11111111111111111111111111111111"}
    )
    prefunded = PayoutStateEnvelope.seal(**{**absent.model_dump(), "recipient_accounts": accounts})
    assert prefunded.risk(current_slot=200, deployment_bindings=DEPLOYMENTS) == (
        "unsupported_recipient_state"
    )
    bundle = fit_bundle(rows(), policy=POLICY)
    prediction = bundle.predict(
        prefunded, features(), current_slot=200, deployment_bindings=DEPLOYMENTS
    )
    assert prediction.simulation_recommended


def test_baseline_fitting_requires_unchanged_frozen_development_records() -> None:
    observations = rows()
    bundle = fit_bundle(observations, policy=POLICY)
    fitting_id = bundle.split.fit_ids[0]
    changed = [
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "label": ResourceLabel(
                            success=True, compute_units=999999, loaded_accounts_bytes=999999
                        )
                    }
                )
            }
        )
        if row.record_id == fitting_id
        else row
        for row in observations
    ]
    with pytest.raises(ValueError, match="frozen bundle"):
        fit_baselines(bundle, changed)
    with pytest.raises(ValueError, match="frozen bundle"):
        fit_baselines(bundle, [row for row in observations if row.record_id != fitting_id])


def test_explicit_qualification_reuses_registry_and_quarantine(tmp_path: Path) -> None:
    synthetic = rows()
    registry = ProfileRegistry(tmp_path / "lifecycle.sqlite")
    dependencies = {program: () for program in DEPLOYMENTS}
    deployments = [
        DeploymentEvidence(
            program_id=program,
            fingerprint=fingerprint,
            owner=key(1),
            deployment_slot=0,
            observed_slot=200,
            checked_at=time.time(),
            cluster_identity="synthetic-cluster",
            runtime_identity="synthetic-runtime",
        )
        for program, fingerprint in DEPLOYMENTS.items()
    ]
    with pytest.raises(ValueError, match="actual local-runtime"):
        qualify_bundle(
            fit_bundle(synthetic, policy=POLICY),
            registry,
            deployments=deployments,
            dependencies=dependencies,
            current_slot=200,
        )
    # Deliberately relabelled fixture only exercises lifecycle integration in an
    # offline unit test; this is not published as measured runtime evidence.
    fixtures = [
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "source": "simulation",
                        "evidence_origin": "local-runtime",
                    }
                )
            }
        )
        for row in synthetic
    ]
    bundle = fit_bundle(fixtures, policy=POLICY)
    report = qualify_bundle(
        bundle, registry, deployments=deployments, dependencies=dependencies, current_slot=200
    )
    assert report["active_count"] == 3
    for cell in bundle.models:
        manifest, _, _ = registry.active_snapshot(bundle.profile_id(cell))
        assert manifest.evidence_min_slot == int(
            bundle.diagnostics["cell_evidence_min_slots"][cell]
        )
        assert manifest.evidence_min_slot < bundle.models[cell].training_max_slot
    assert (
        qualify_bundle(
            bundle, registry, deployments=deployments, dependencies=dependencies, current_slot=200
        )["active_count"]
        == 3
    )
    chosen = state(20).state_key
    registry.record_execution(
        bundle.profile_id(chosen),
        bundle.revision_for(chosen),
        "synthetic-confirmed-underestimate",
        success=True,
        compute_units=10001,
        loaded_accounts_bytes=None,
        compute_unit_limit=10000,
        loaded_accounts_data_size_limit=100000,
        current_slot=200,
    )
    repeated = qualify_bundle(
        bundle, registry, deployments=deployments, dependencies=dependencies, current_slot=200
    )
    assert repeated["active_count"] == 2
    assert chosen in repeated["qualification_failures"]
    assert any(event["reason"] == "execution_resource_excess" for event in registry.audit_events())
