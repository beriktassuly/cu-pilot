"""Offline fixtures for independent fitted limits and durable failure scoping."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cu_pilot.lifecycle import DeploymentEvidence, ProfileManifest, ProfileRegistry, artifact_digest
from examples.payouts import app as application
from examples.payouts.baselines import (
    baseline_estimate,
    baseline_invalidation_key,
    fitted_baseline_risk,
)
from examples.payouts.model import fit_bundle
from examples.payouts.planning import fit_baselines
from tests.test_payout_app import ScriptedBridge
from tests.test_payout_model import DEPLOYMENTS, POLICY, features, key, state, varied_rows


@pytest.fixture
def fitted():
    observations = varied_rows()
    # Reject learned calibration deliberately. These synthetic fixtures can never
    # establish local runtime eligibility, but independent baseline scope can pass.
    bundle = fit_bundle(
        observations, policy=POLICY.model_copy(update={"max_joint_underestimation_rate": 0.01})
    )
    return bundle, fit_baselines(bundle, observations)


def registry_with_evidence(tmp_path: Path) -> ProfileRegistry:
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    registry.record_deployments(
        [
            DeploymentEvidence(
                program_id=program,
                fingerprint=fingerprint,
                owner=key(1),
                deployment_slot=None,
                observed_slot=200,
                checked_at=time.time(),
                cluster_identity="synthetic-cluster",
                runtime_identity="synthetic-runtime",
            )
            for program, fingerprint in DEPLOYMENTS.items()
        ]
    )
    return registry


def propose(fitted, candidate=None, *, registry=None, invalidated_keys=()):
    bundle, baseline = fitted
    return baseline_estimate(
        "fixed_batch",
        bundle,
        candidate or state(20, count=4),
        features(4),
        current_slot=200,
        cache={},
        fitted_baselines=baseline,
        registry=registry,
        invalidated_keys=invalidated_keys,
    )


def test_independent_constant_does_not_require_or_activate_learned_release(fitted, tmp_path):
    bundle, baseline = fitted
    candidate = state(20, count=4)
    prediction = bundle.predict(
        candidate, features(4), current_slot=200, deployment_bindings=DEPLOYMENTS
    )
    assert prediction.simulation_recommended and prediction.reason == "joint_calibration_risk"
    registry = registry_with_evidence(tmp_path)
    before = registry.audit_events()
    assert propose(fitted, registry=registry) == baseline.fixed_count_limits["4"]
    assert registry.audit_events() == before
    with pytest.raises(ValueError, match="No active profile"):
        registry.active_snapshot(bundle.profile_id(candidate.state_key))


@pytest.mark.parametrize("method", ["fixed_batch", "formula"])
def test_fitting_age_cannot_be_refreshed_by_later_calibration(fitted, method):
    bundle, baseline = fitted
    cell = bundle.models[state(20, count=4).state_key]
    stats = cell.patterns[features(4).pattern_id]
    slot = stats.training_max_slot + bundle.policy.max_age_slots + 1
    assert slot - cell.max_slot <= bundle.policy.max_age_slots
    candidate = state(20, count=4, observation_slot=slot)
    assert (
        fitted_baseline_risk(
            method,
            bundle,
            candidate,
            features(4),
            current_slot=slot,
            fitted_baselines=baseline,
        )
        == "baseline_stale_fitting_evidence"
    )
    assert (
        baseline_estimate(
            method,
            bundle,
            candidate,
            features(4),
            current_slot=slot,
            cache={},
            fitted_baselines=baseline,
        )
        is None
    )


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"runtime_identity": "different-runtime"}, "baseline_runtime_mismatch"),
        ({"cluster_identity": "different-bank"}, "baseline_runtime_mismatch"),
        ({"deployment_bindings": {key(7): "f" * 64}}, "deployment_changed"),
        ({"queue_data_bytes": 901}, "baseline_unsupported_state"),
        ({"paused": True}, "queue_not_executable"),
    ],
)
def test_constant_scope_rejects_changed_environment_or_unsupported_state(fitted, updates, reason):
    bundle, baseline = fitted
    candidate = state(20, count=4, **updates)
    assert (
        fitted_baseline_risk(
            "fixed_batch",
            bundle,
            candidate,
            features(4),
            current_slot=200,
            fitted_baselines=baseline,
        )
        == reason
    )
    assert propose(fitted, candidate) is None


def test_constant_does_not_inherit_count_support_for_unseen_message_pattern(fitted):
    bundle, baseline = fitted
    assert (
        baseline_estimate(
            "fixed_batch",
            bundle,
            state(20, count=4),
            features(4).model_copy(update={"pattern_id": "unseen-pattern"}),
            current_slot=200,
            cache={},
            fitted_baselines=baseline,
        )
        is None
    )
    assert (
        baseline_estimate(
            "fixed_batch",
            bundle,
            state(20, count=4),
            features(4).model_copy(update={"serialized_size": 1233}),
            current_slot=200,
            cache={},
            fitted_baselines=baseline,
        )
        is None
    )


def test_registered_but_never_activated_quarantine_blocks_constant(fitted, tmp_path):
    bundle, _ = fitted
    candidate = state(20, count=4)
    cell = bundle.models[candidate.state_key]
    profile = bundle.profile_id(candidate.state_key)
    revision = bundle.revision_for(candidate.state_key)
    registry = registry_with_evidence(tmp_path)
    registry.register(
        ProfileManifest(
            profile_id=profile,
            revision=revision,
            artifact_sha256=artifact_digest(cell.model_dump_json()),
            context=bundle.context,
            cluster_identity=bundle.cluster_identity,
            runtime_identity=bundle.runtime_identity,
            workload_allowlist=("payout-queue-v1",),
            deployment_bindings=DEPLOYMENTS,
            dependencies={program: () for program in DEPLOYMENTS},
            evidence_min_slot=0,
            evidence_max_slot=cell.max_slot,
            provenance=cell.source,
        ),
        cell.model_dump_json(),
        actor="offline-test",
    )
    registry.suspend(
        profile, revision, actor="offline-test", reason="explicit_test_suspension", current_slot=200
    )
    history = registry.audit_events()
    assert propose(fitted, registry=registry) is None
    assert registry.audit_events() == history


def manifest_for(bundle, cell_key, *, model=None, revision=None):
    cell = bundle.models[cell_key] if model is None else model
    return ProfileManifest(
        profile_id=bundle.profile_id(cell_key),
        revision=bundle.revision_for(cell_key) if revision is None else revision,
        artifact_sha256=artifact_digest(cell.model_dump_json()),
        context=bundle.context,
        cluster_identity=bundle.cluster_identity,
        runtime_identity=bundle.runtime_identity,
        workload_allowlist=("payout-queue-v1",),
        deployment_bindings=bundle.deployment_bindings,
        dependencies={program: () for program in bundle.deployment_bindings},
        dependency_closure_verified=True,
        budget_independent=True,
        evidence_min_slot=0,
        evidence_max_slot=cell.max_slot,
        provenance=cell.source,
    )


@pytest.mark.parametrize("method", ["fixed_batch", "formula"])
def test_retired_fitted_revision_stays_blocked_after_replacement_activation(tmp_path, method):
    # Handcrafted simulation-contract fixtures exercise public lifecycle APIs.
    # They are not actual runtime observations or releasable experiment evidence.
    observations = [
        row.model_copy(
            update={
                "observation": row.observation.model_copy(
                    update={
                        "context": "offline-revision-regression",
                        "source": "simulation",
                        "label_source": "simulation",
                        "evidence_origin": "local-runtime",
                    }
                )
            }
        )
        for row in varied_rows()
    ]
    bundle = fit_bundle(observations, policy=POLICY)
    baseline = fit_baselines(bundle, observations)
    candidate = state(20, count=4)
    cell_key = candidate.state_key
    profile = bundle.profile_id(cell_key)
    revision = bundle.revision_for(cell_key)
    registry = registry_with_evidence(tmp_path)
    registry.register(
        manifest_for(bundle, cell_key),
        bundle.models[cell_key].model_dump_json(),
        actor="offline-test",
    )
    registry.transition(profile, revision, "retired", actor="offline-test", reason="withdrawn")
    replacement = fit_bundle(
        observations, policy=POLICY.model_copy(update={"compute_margin_bps": 1001})
    ).models[cell_key]
    registry.register(
        manifest_for(bundle, cell_key, model=replacement, revision=revision + 1),
        replacement.model_dump_json(),
        actor="offline-test",
    )
    registry.transition(
        profile, revision + 1, "shadow", actor="offline-test", reason="replacement fixture"
    )
    registry.activate(
        profile,
        revision + 1,
        actor="offline-test",
        reason="replacement fixture",
        current_slot=200,
        context=bundle.context,
        cluster_identity=bundle.cluster_identity,
        runtime_identity=bundle.runtime_identity,
        workload="payout-queue-v1",
        program_ids=tuple(DEPLOYMENTS),
    )
    assert registry.check(
        profile,
        current_slot=200,
        context=bundle.context,
        cluster_identity=bundle.cluster_identity,
        runtime_identity=bundle.runtime_identity,
        workload="payout-queue-v1",
        program_ids=tuple(DEPLOYMENTS),
    ).eligible
    assert (
        fitted_baseline_risk(
            method,
            bundle,
            candidate,
            features(4),
            current_slot=200,
            fitted_baselines=baseline,
            registry=registry,
        )
        == "profile_retired"
    )
    assert (
        baseline_estimate(
            method,
            bundle,
            candidate,
            features(4),
            current_slot=200,
            cache={},
            fitted_baselines=baseline,
            registry=registry,
        )
        is None
    )


@pytest.mark.parametrize("condition", ["missing", "watcher_failed", "force_simulation"])
def test_live_constant_requires_current_deployment_and_emergency_evidence(
    fitted, tmp_path, condition
):
    registry = (
        ProfileRegistry(tmp_path / "profiles.sqlite")
        if condition == "missing"
        else registry_with_evidence(tmp_path)
    )
    if condition == "watcher_failed":
        registry.watcher_failed()
    if condition == "force_simulation":
        registry.force_simulation(True, actor="offline-test", reason="test emergency")
    assert propose(fitted, registry=registry) is None


def test_invalidation_survives_new_queue_and_state_cell_but_preserves_method_scope(fitted):
    bundle, baseline = fitted
    blocked = baseline_invalidation_key("fixed_batch", bundle, baseline.model_dump(mode="json"), 4)
    recorded_failure = {blocked: {"reason": "confirmed_resource_budget_exhaustion", "slot": 200}}
    assert propose(fitted, invalidated_keys=recorded_failure) is None
    other_queue_and_state = state(200, missing=4, count=4, observation_slot=200)
    assert propose(fitted, other_queue_and_state, invalidated_keys=recorded_failure) is None
    assert blocked != baseline_invalidation_key("fixed_batch", bundle, baseline, 2)
    assert blocked != baseline_invalidation_key("formula", bundle, baseline, 4)
    assert (
        baseline_estimate(
            "formula",
            bundle,
            state(20, count=4),
            features(4),
            current_slot=200,
            cache={},
            fitted_baselines=baseline,
            invalidated_keys=recorded_failure,
        )
        is not None
    )


def test_constant_rejects_baseline_from_different_bundle(fitted):
    bundle, baseline = fitted
    mismatched = baseline.model_copy(update={"bundle_digest": "f" * 64})
    with pytest.raises(ValueError, match="different frozen fit"):
        baseline_invalidation_key("fixed_batch", bundle, mismatched, 4)


def test_baseline_cannot_claim_calibration_records_as_fitting_provenance(fitted):
    bundle, baseline = fitted
    altered = baseline.model_copy(update={"fit_record_ids": bundle.split.calibration_ids})
    assert propose((bundle, altered)) is None


def test_confirmed_baseline_failure_is_durable_across_queues_and_restart(
    fitted, tmp_path, monkeypatch
):
    bundle, baseline = fitted
    blocked = baseline_invalidation_key("fixed_batch", bundle, baseline, 4)
    bridge = ScriptedBridge()
    monkeypatch.setattr(application, "Bridge", lambda _path: bridge)
    app = application.Application(tmp_path)
    body = {
        "id": "failed-constant-decision",
        "queue": "first-queue",
        "method": "fixed_batch",
        "chosen_count": 4,
        "baseline_invalidation_key": blocked,
        "decision": {"compute_unit_limit": baseline.fixed_count_limits["4"][0], "plan": {}},
    }
    failed = {
        "slot": 200,
        "meta": {"err": {"InstructionError": [0, "ComputationalBudgetExceeded"]}},
    }
    assert app.audit_budget_failure(body, failed)
    original_record = app.setting("fitted_baseline_invalidations")
    assert original_record[blocked]["decision_id"] == body["id"]
    assert original_record[blocked]["resource"] == "compute"
    # Reconciliation replay retains the first failure instead of replacing it.
    assert app.audit_budget_failure({**body, "id": "later-replay"}, {**failed, "slot": 201})
    assert app.setting("fitted_baseline_invalidations") == original_record
    app.close()
    reopened = application.Application(tmp_path)
    try:
        invalidated = reopened.setting("fitted_baseline_invalidations")
        assert invalidated == original_record
        other_queue = state(200, missing=4, count=4, observation_slot=200)
        assert propose(fitted, other_queue, invalidated_keys=invalidated) is None
        assert reopened.setting("force_simulation:another-queue") is None
    finally:
        reopened.close()


def test_semantic_failure_does_not_claim_confirmed_constant_exhaustion(
    fitted, tmp_path, monkeypatch
):
    bundle, baseline = fitted
    bridge = ScriptedBridge()
    monkeypatch.setattr(application, "Bridge", lambda _path: bridge)
    app = application.Application(tmp_path)
    try:
        body = {
            "id": "semantic-error",
            "method": "fixed_batch",
            "chosen_count": 4,
            "baseline_invalidation_key": baseline_invalidation_key(
                "fixed_batch", bundle, baseline, 4
            ),
            "decision": {"compute_unit_limit": 100_000, "plan": {}},
        }
        assert not app.audit_budget_failure(
            body,
            {"slot": 200, "meta": {"err": {"InstructionError": [0, "InvalidAccountData"]}}},
        )
        assert app.setting("fitted_baseline_invalidations") is None
    finally:
        app.close()


@pytest.mark.parametrize("restriction", ["confirmed_failure", "retired", "suspended"])
def test_final_presign_check_blocks_concurrently_invalidated_constant(
    tmp_path, monkeypatch, restriction
):
    from tests import test_payout_adaptive as harness

    monkeypatch.setattr(harness, "SLOT", 500)
    fixture = harness.PlannerFixture(tmp_path, monkeypatch)
    app = fixture.app
    try:

        def envelope(group, count, missing=0, observation_slot=None):
            return state(
                group,
                count=count,
                missing=missing,
                observation_slot=group * 10 if observation_slot is None else observation_slot,
                cluster_identity=app.cluster,
                runtime_identity=app.runtime,
                deployment_bindings=app.deployment_bindings,
                prepared_identity=fixture.prepared[count].prepared_identity,
            )

        observations = [
            row.model_copy(
                update={
                    "state": envelope(
                        int(row.queue_group.split("-")[1]),
                        row.state.candidate_count,
                        row.state.missing_atas,
                    ),
                    "observation": row.observation.model_copy(
                        update={
                            "context": app.context,
                            "features": fixture.prepared[row.state.candidate_count].features,
                        }
                    ),
                }
            )
            for row in varied_rows()
        ]
        bundle = fit_bundle(observations, policy=POLICY)
        app.bundle = bundle
        app.baselines = fit_baselines(bundle, observations).model_dump(mode="json")
        monkeypatch.setattr(
            app, "envelope", lambda c: envelope(50, c["count"], observation_slot=500)
        )
        original_call = fixture.call

        def invalidate_during_snapshot(action, **kwargs):
            if action == "snapshot":
                body = json.loads(
                    app.store.db.execute(
                        "SELECT body FROM payout_steps ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()[0]
                )
                assert body["limit_source"] == "scoped_fitted_estimate"
                if restriction == "confirmed_failure":
                    assert app.audit_budget_failure(
                        body,
                        {
                            "slot": 500,
                            "meta": {
                                "err": {"InstructionError": [0, "ComputationalBudgetExceeded"]}
                            },
                        },
                    )
                else:
                    cell_key = envelope(50, body["chosen_count"]).state_key
                    manifest = manifest_for(bundle, cell_key)
                    app.registry.register(
                        manifest, bundle.models[cell_key].model_dump_json(), actor="offline-test"
                    )
                    if restriction == "retired":
                        app.registry.transition(
                            manifest.profile_id,
                            manifest.revision,
                            "retired",
                            actor="offline-test",
                            reason="withdrawn before signing",
                        )
                    else:
                        app.registry.suspend(
                            manifest.profile_id,
                            manifest.revision,
                            actor="offline-test",
                            reason="suspended before signing",
                            current_slot=500,
                        )
            return original_call(action, **kwargs)

        monkeypatch.setattr(fixture, "call", invalidate_during_snapshot)
        with pytest.raises(RuntimeError, match="invalidated before signing"):
            app.step("second-queue", "fixed_batch", interrupt_after_sign=True)
        assert not any(action in {"sign", "send"} for action, _ in fixture.calls)
        if restriction == "confirmed_failure":
            assert app.setting("fitted_baseline_invalidations")
        assert fixture.simulations == []
    finally:
        app.close()


@pytest.mark.parametrize(
    ("method", "expected_count", "expected_status"),
    [("scoped_fixed_batch", 4, "simulation_success"), ("fixed_batch", 2, "accepted_prediction")],
)
def test_scoped_fixed_batch_simulates_exact_blocked_count_and_legacy_stays_unchanged(
    tmp_path, monkeypatch, method, expected_count, expected_status
):
    from tests import test_payout_adaptive as harness

    monkeypatch.setattr(harness, "SLOT", 500)
    fixture = harness.PlannerFixture(tmp_path, monkeypatch)
    app = fixture.app
    try:

        def envelope(group, count, missing=0, observation_slot=None):
            return state(
                group,
                count=count,
                missing=missing,
                observation_slot=group * 10 if observation_slot is None else observation_slot,
                cluster_identity=app.cluster,
                runtime_identity=app.runtime,
                deployment_bindings=app.deployment_bindings,
                prepared_identity=fixture.prepared[count].prepared_identity,
            )

        observations = [
            row.model_copy(
                update={
                    "state": envelope(
                        int(row.queue_group.split("-")[1]),
                        row.state.candidate_count,
                        row.state.missing_atas,
                    ),
                    "observation": row.observation.model_copy(
                        update={
                            "context": app.context,
                            "features": fixture.prepared[row.state.candidate_count].features,
                        }
                    ),
                }
            )
            for row in varied_rows()
        ]
        app.bundle = fit_bundle(observations, policy=POLICY)
        app.baselines = fit_baselines(app.bundle, observations).model_dump(mode="json")
        assert app.baselines["fixed_batch_count"] == 4
        monkeypatch.setattr(
            app, "envelope", lambda c: envelope(50, c["count"], observation_slot=500)
        )
        key = baseline_invalidation_key("fixed_batch", app.bundle, app.baselines, 4)
        app.set_setting(
            "fitted_baseline_invalidations", {key: {"reason": "earlier confirmed failure"}}
        )
        body = fixture.signed_step(method)
        assert body["chosen_count"] == expected_count
        assert body["decision"]["status"] == expected_status
        blocked_option = next(option for option in body["options"] if option["count"] == 4)
        assert blocked_option["baseline_invalidation_key"] == key
        assert blocked_option["limits"] is None
        assert [s["count"] for s in fixture.simulations] == (
            [4] if method == "scoped_fixed_batch" else []
        )
        if method == "scoped_fixed_batch":
            assert [option["count"] for option in body["options"]] == [4]
            assert body["limit_source"] == "fresh_simulation"
        else:
            assert body["limit_source"] == "scoped_fitted_estimate"
    finally:
        app.close()
