"""Hybrid integration contracts use signed local fixtures, not runtime performance data."""

from __future__ import annotations

import pytest

from tests.test_payout_adaptive import planner as planner


def test_missing_hybrid_artifact_simulates_without_borrowing_quantile_release(planner):
    before = planner.app.registry.check(planner.profile(8), **planner.environment(8))
    assert before.eligible
    body = planner.signed_step("adaptive_derivation")
    assert body["chosen_count"] == 8
    assert body["decision"]["status"] == "simulation_success"
    assert body["decision"]["plan"]["profile_id"] is None
    assert body["decision"]["plan"]["formula_inputs"] is None
    assert body["model_digest"] == "0" * 64
    assert body["limit_source"] == "fresh_simulation"
    assert [row["count"] for row in planner.simulations] == [8]
    assert planner.app.registry.check(planner.profile(8), **planner.environment(8)).eligible


def test_hybrid_fallback_keeps_pre_sign_state_guard(planner):
    planner.mutate_snapshot = True
    with pytest.raises(RuntimeError, match="pre-execution state changed"):
        planner.signed_step("adaptive_derivation")
    assert not any(action == "sign" for action, _ in planner.calls)
    assert [row["count"] for row in planner.simulations] == [8]


def test_hybrid_fallback_stops_on_semantic_simulation_failure(planner):
    planner.simulation_errors[8] = "simulation_failed"
    with pytest.raises(RuntimeError, match="adaptive planning stopped"):
        planner.signed_step("adaptive_derivation")
    assert [row["count"] for row in planner.simulations] == [8]
    assert not any(action == "sign" for action, _ in planner.calls)


def test_collection_namespace_survives_interruption_and_rejects_retargeting(planner, monkeypatch):
    seeds = []

    def interrupted_create(**kwargs):
        seeds.append(kwargs["recipient_seed"])
        raise RuntimeError("fixture interrupts before funding")

    monkeypatch.setattr(planner.app, "create", interrupted_create)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="fixture interrupts"):
            planner.app.collect(groups=1)
    namespace = planner.app.setting("collection_namespace")
    assert namespace.startswith(planner.app.info["instance_id"] + ":")
    assert seeds[0] == seeds[1] == f"payout-collection-v2:{namespace}:0:1"
    with pytest.raises(ValueError, match="namespace changed"):
        planner.app.collect(groups=1, namespace="different-experiment")
    assert len(seeds) == 2


def test_legacy_collection_resume_preserves_old_public_recipients(planner, monkeypatch):
    planner.app.set_setting("collection_schedule", {"79:8": "old-queue"})
    seeds = []

    def interrupted_create(**kwargs):
        seeds.append(kwargs["recipient_seed"])
        raise RuntimeError("fixture interrupts before funding")

    monkeypatch.setattr(planner.app, "create", interrupted_create)
    with pytest.raises(RuntimeError, match="fixture interrupts"):
        planner.app.collect(groups=1)
    assert seeds == ["payout-collection-v1:1729:0:1"]
    assert planner.app.setting("collection_namespace") == "legacy:payout-collection-v1:1729"


def install_formula(planner, *, control_probability=0.0):
    """Synthetic calibrated formula with real PDA inputs and signed-message bindings."""
    import base64
    from types import SimpleNamespace

    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    from cu_pilot.binding import bind_message
    from cu_pilot.estimator import wilson_upper_bound
    from cu_pilot.formula import (
        ATA_PROGRAM,
        TOKEN_PROGRAM,
        FormulaArtifact,
        FormulaEstimator,
        FormulaInputs,
        FormulaPattern,
        FormulaRecipient,
        features_digest,
    )
    from cu_pilot.lifecycle import ProfileManifest, artifact_digest
    from cu_pilot.resources import ResourceEstimator, ResourcePolicy, ResourceRange
    from cu_pilot.schemas import Observation, ResourceLabel

    count = 8
    mint = Pubkey.from_bytes(bytes([9]) * 32)
    recipients = []
    for index in range(count):
        recipient = Pubkey.from_bytes(bytes([50 + index]) * 32)
        address, _ = Pubkey.find_program_address(
            [bytes(recipient), bytes(Pubkey.from_string(TOKEN_PROGRAM)), bytes(mint)],
            Pubkey.from_string(ATA_PROGRAM),
        )
        recipients.append(
            FormulaRecipient(recipient=str(recipient), address=str(address), exists=True)
        )
    accounts = [AccountMeta(planner.payer.pubkey(), True, True), AccountMeta(mint, False, False)]
    for recipient in recipients:
        accounts.extend(
            [
                AccountMeta(Pubkey.from_string(recipient.recipient), False, False),
                AccountMeta(Pubkey.from_string(recipient.address), False, True),
            ]
        )
    wire = base64.b64encode(
        bytes(
            Transaction.new_unsigned(
                Message.new_with_blockhash(
                    [Instruction(Pubkey.default(), b"formula-binding-unit-fixture", accounts)],
                    planner.payer.pubkey(),
                    Hash.from_bytes(bytes([8]) * 32),
                )
            )
        )
    ).decode()
    bound = bind_message(wire, current_slot=100)
    planner.wires[count] = wire
    planner.prepared[count] = bound
    planner.estimators[count] = ResourceEstimator.fit(
        [
            Observation(
                record_id=f"formula-shape-{slot}",
                slot=slot,
                context=planner.app.context,
                source="simulation",
                features=bound.features,
                label=ResourceLabel(
                    success=True, compute_units=80_000, loaded_accounts_bytes=20_000
                ),
            )
            for slot in range(10, 70)
        ],
        planner.estimators[count].model.policy,
    )
    inputs = FormulaInputs(
        prepared_identity=bound.prepared_identity,
        bound_features_digest=features_digest(bound.features),
        state_digest="a" * 64,
        cell_key="synthetic-n8-m0",
        observed_slot=100,
        max_age_slots=8,
        mint=str(mint),
        recipients=tuple(recipients),
    )
    resource = planner.estimators[count].model.patterns[bound.features.pattern_id]
    policy = ResourcePolicy(
        compute_margin_bps=0,
        data_margin_bps=0,
        min_samples=12,
        min_calibration_samples=16,
        max_joint_underestimation_rate=0.15,
    )
    pattern = FormulaPattern(
        train_count=20,
        qualification_count=16,
        qualification_joint_exceedances=0,
        qualification_upper_bound=wilson_upper_bound(0, 16),
        numerical_ranges=resource.numerical_ranges,
        instruction_data_length_ranges=resource.instruction_data_length_ranges,
        program_ids=resource.program_ids,
        version=resource.version,
        vector_ranges=tuple(
            ResourceRange(minimum=value, maximum=value)
            for value in inputs.vector("count_missing_derivation")
        ),
    )
    model = FormulaArtifact(
        context=planner.app.context,
        source="simulation",
        label_source="simulation",
        evidence_origin="local-runtime",
        cluster_identity=planner.app.cluster,
        runtime_identity=planner.app.runtime,
        deployment_bindings=planner.app.deployment_bindings,
        cell_key=inputs.cell_key,
        evidence_min_slot=10,
        training_max_slot=29,
        calibration_min_slot=30,
        calibration_max_slot=53,
        qualification_min_slot=54,
        max_slot=69,
        policy=policy,
        compute_coefficients=(5000, 8000, 11000, 100, 2500),
        data_coefficients=(20000, 0, 0, 0, 0),
        calibration_scores=(0.0,) * 24,
        calibration_cohorts=tuple(f"calibration-{n}" for n in range(24)),
        fit_cohorts=tuple(f"fit-{n}" for n in range(20)),
        qualification_cohorts=tuple(f"qualification-{n}" for n in range(16)),
        correction=0.0,
        development_digest="b" * 64,
        patterns={bound.features.pattern_id: pattern},
    )
    manifest = ProfileManifest(
        profile_id="synthetic-derivation-count-8",
        revision=1,
        artifact_sha256=artifact_digest(model.model_dump_json()),
        context=planner.app.context,
        cluster_identity=planner.app.cluster,
        runtime_identity=planner.app.runtime,
        workload_allowlist=("payout-queue-v1",),
        deployment_bindings=planner.app.deployment_bindings,
        dependencies={p: () for p in planner.app.deployment_bindings},
        dependency_closure_verified=True,
        budget_independent=True,
        evidence_min_slot=10,
        evidence_max_slot=69,
        provenance="simulation",
        control_probability=control_probability,
    )
    registry = planner.app.registry
    registry.register(manifest, model.model_dump_json(), actor="synthetic-unit-fixture")
    registry.transition(manifest.profile_id, 1, "shadow", actor="test", reason="unit fixture")
    registry.activate(
        manifest.profile_id,
        1,
        actor="test",
        reason="unit fixture",
        **planner.environment(count),
    )
    estimator = FormulaEstimator(model, inputs)
    planner.app.hybrid_bundle = SimpleNamespace(
        digest="e" * 64,
        estimator_for=lambda state, **_kwargs: (
            (manifest.profile_id, estimator, "supported")
            if state.candidate_count == count
            else (None, None, "unsupported_count")
        ),
    )
    return manifest, estimator


def test_live_formula_reloads_with_bound_inputs_and_signs_exact_candidate(planner):
    from cu_pilot.formula import FormulaEstimator

    manifest, _ = install_formula(planner)
    _, loaded = planner.app.registry.load_active(manifest.profile_id)
    assert isinstance(loaded, FormulaEstimator)
    assert loaded.inputs is None
    assert (
        loaded.predict(
            planner.prepared[8].features, context=planner.app.context, current_slot=100
        ).reason
        == "formula_state_unbound"
    )
    body = planner.signed_step("adaptive_derivation")
    assert body["decision"]["status"] == "accepted_prediction"
    assert body["model_digest"] == "e" * 64
    assert body["limit_source"] == "qualified_derivation_prediction"
    assert body["adaptive_planning"]["limit_source"] == "qualified_derivation_prediction"
    assert body["decision"]["plan"]["formula_inputs"]["prepared_identity"] == (
        planner.prepared[8].prepared_identity
    )
    assert planner.simulations == []


def test_formula_rejects_other_prepared_message_before_prediction(planner):
    from cu_pilot.integration import prepare_decision

    manifest, estimator = install_formula(planner)
    with pytest.raises(ValueError, match="formula_message_mismatch"):
        prepare_decision(
            planner.wires[4],
            context=planner.app.estimation_context(100),
            estimator=estimator,
            profile_id=manifest.profile_id,
            registry=planner.app.registry,
        )
    assert planner.simulations == []


def test_formula_control_exceedance_suspends_formula_only(planner):
    manifest, _ = install_formula(planner, control_probability=1.0)
    body = planner.signed_step("adaptive_derivation")
    assert body["decision"]["status"] == "simulation_success"
    assert body["control_simulations"] == 1
    assert body["estimation_simulations"] == 0
    assert not planner.app.registry.check(manifest.profile_id, **planner.environment(8)).eligible
    assert planner.app.registry.check(planner.profile(8), **planner.environment(8)).eligible


def test_formula_replay_tampering_requires_simulation(planner):
    from cu_pilot.integration import execute_decision, prepare_decision

    manifest, estimator = install_formula(planner)
    plan = prepare_decision(
        planner.wires[8],
        context=planner.app.estimation_context(100),
        estimator=estimator,
        profile_id=manifest.profile_id,
        registry=planner.app.registry,
    )
    assert plan.formula_inputs is not None
    changed = plan.model_copy(
        update={
            "formula_inputs": plan.formula_inputs.model_copy(
                update={"prepared_identity": planner.prepared[4].prepared_identity}
            )
        }
    )
    result = execute_decision(
        changed,
        rpc=planner.app.rpc,
        registry=planner.app.registry,
        current_slot=100,
    )
    assert result.status == "simulation_success"
    assert result.reason == "profile_revalidation_failed"
    assert len(planner.simulations) == 1


def test_formula_manifest_cannot_rebind_another_runtime(planner):
    manifest, estimator = install_formula(planner)
    changed = manifest.model_copy(update={"profile_id": "forged", "runtime_identity": "other-bank"})
    with pytest.raises(ValueError, match="Formula evidence scope"):
        planner.app.registry.register(changed, estimator.model.model_dump_json(), actor="test")


@pytest.mark.parametrize("age,expected", [(8, "accepted_prediction"), (9, "simulation_success")])
def test_formula_revalidation_reports_expiry_without_relaxing_freshness(
    planner, monkeypatch, age, expected
):
    from cu_pilot.integration import execute_decision, prepare_decision

    manifest, estimator = install_formula(planner)
    plan = prepare_decision(
        planner.wires[8],
        context=planner.app.estimation_context(100),
        estimator=estimator,
        profile_id=manifest.profile_id,
        registry=planner.app.registry,
    )
    original_simulate = planner.simulate

    def fresh_simulation(wire, **kwargs):
        return original_simulate(wire, **kwargs).model_copy(
            update={"slot": kwargs["min_context_slot"]}
        )

    monkeypatch.setattr(planner.app.rpc, "simulate", fresh_simulation)
    result = execute_decision(
        plan, rpc=planner.app.rpc, registry=planner.app.registry, current_slot=100 + age
    )
    assert result.status == expected
    assert result.reason == ("stale_state" if age == 9 else "qualified_joint_conformal_formula")
    assert result.resource_simulation_calls == int(age == 9)
    assert len(planner.simulations) == int(age == 9)
    if age == 9:
        assert result.compute_unit_limit == planner.simulation_limits.get(8, (88_000, 32_768))[0]
        assert result.plan.prediction == plan.prediction


def test_formula_changed_numeric_prediction_keeps_artifact_mismatch_diagnostic(planner):
    from cu_pilot.integration import execute_decision, prepare_decision

    manifest, estimator = install_formula(planner)
    plan = prepare_decision(
        planner.wires[8],
        context=planner.app.estimation_context(100),
        estimator=estimator,
        profile_id=manifest.profile_id,
        registry=planner.app.registry,
    )
    altered = plan.model_copy(
        update={
            "prediction": plan.prediction.model_copy(
                update={
                    "compute_unit_limit": plan.prediction.compute_unit_limit + 1000,
                }
            )
        }
    )
    result = execute_decision(
        altered, rpc=planner.app.rpc, registry=planner.app.registry, current_slot=100
    )
    assert result.status == "simulation_success"
    assert result.reason == "prediction_artifact_mismatch"
    assert result.resource_simulation_calls == 1


def test_loaded_manifest_mismatch_is_not_hidden_by_expired_formula_inputs(planner, monkeypatch):
    from cu_pilot.integration import execute_decision, prepare_decision

    manifest, estimator = install_formula(planner)
    plan = prepare_decision(
        planner.wires[8],
        context=planner.app.estimation_context(100),
        estimator=estimator,
        profile_id=manifest.profile_id,
        registry=planner.app.registry,
    )
    original = planner.app.registry.load_active

    def changed_manifest(profile):
        loaded, model = original(profile)
        return loaded.model_copy(update={"artifact_sha256": "0" * 64}), model

    monkeypatch.setattr(planner.app.registry, "load_active", changed_manifest)
    result = execute_decision(
        plan, rpc=planner.app.rpc, registry=planner.app.registry, current_slot=109
    )
    assert result.status == "simulation_success"
    assert result.reason == "prediction_artifact_mismatch"
    assert result.resource_simulation_calls == 1
