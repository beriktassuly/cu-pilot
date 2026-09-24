"""Adversarial offline boundary tests; RPC fixtures are not runtime evidence."""

import base64
import time
from pathlib import Path

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from test_binding import builder, context, rpc_response

from cu_pilot.binding import bind_message, decode_wire, replace_resources, unsigned_wire
from cu_pilot.integration import estimate_resources, execute_decision, prepare_decision
from cu_pilot.lifecycle import (
    NATIVE_LOADER,
    ProfileManifest,
    ProfileRegistry,
    artifact_digest,
    deployment_identity,
    refresh_deployments,
)
from cu_pilot.resource_evaluation import preparation_report
from cu_pilot.resources import ResourceEstimator
from cu_pilot.rpc import RpcClient
from cu_pilot.schemas import Observation, ResourceLabel
from cu_pilot.shadow import ObservationStore, RecordConflict, ShadowRequest, collect_shadow


def client(slot=10):
    return RpcClient(
        "http://example.invalid",
        requests_per_second=10000,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rpc_response(slot=slot))),
    )


def release_for_wire(path: Path, wire: str, *, control_probability: float = 0):
    ctx = context().model_copy(update={"current_slot": 400})
    features = bind_message(wire, current_slot=400).features
    # Simulated-source contract fixture, never exported or represented as evidence
    # of model accuracy. This test exercises explicit operator release mechanics.
    estimator = ResourceEstimator.fit(
        [
            Observation(
                record_id=f"fixture-{i}",
                slot=i,
                context=ctx.context,
                source="simulation",
                features=features,
                label=ResourceLabel(success=True, compute_units=600, loaded_accounts_bytes=128),
            )
            for i in range(400)
        ]
    )
    programs = list(dict.fromkeys(features.program_ids))
    evidence = [
        deployment_identity(
            p,
            {
                "owner": NATIVE_LOADER,
                "executable": True,
                "data": [base64.b64encode(b"offline-fixture").decode(), "base64"],
            },
            observed_slot=400,
            checked_at=time.time(),
            cluster_identity=ctx.cluster_identity,
            runtime_identity=ctx.runtime_identity,
        )
        for p in programs
    ]
    payload = estimator.model.model_dump_json()
    manifest = ProfileManifest(
        profile_id="review",
        revision=1,
        artifact_sha256=artifact_digest(payload),
        context=ctx.context,
        cluster_identity=ctx.cluster_identity,
        runtime_identity=ctx.runtime_identity,
        workload_allowlist=(ctx.workload,),
        deployment_bindings={e.program_id: e.fingerprint for e in evidence},
        dependencies={p: () for p in programs},
        dependency_closure_verified=True,
        budget_independent=True,
        evidence_min_slot=0,
        evidence_max_slot=399,
        provenance="simulation",
        control_probability=control_probability,
    )
    registry = ProfileRegistry(path)
    registry.register(manifest, payload, actor="test-operator")
    registry.transition("review", 1, "shadow", actor="test-operator", reason="fixture")
    registry.record_deployments(evidence)
    registry.activate(
        "review",
        1,
        actor="test-operator",
        reason="fixture",
        current_slot=400,
        context=ctx.context,
        cluster_identity=ctx.cluster_identity,
        runtime_identity=ctx.runtime_identity,
        workload=ctx.workload,
        program_ids=programs,
    )
    return ctx, estimator, registry


@pytest.mark.parametrize("change", ["bytes", "features", "budget", "identity", "nonce"])
def test_persisted_plan_cannot_substitute_message_or_feature_input(change):
    plan = prepare_decision(unsigned_wire(builder()), context=context())
    if change == "bytes":
        plan = plan.model_copy(
            update={
                "prepared_wire_base64": bind_message(
                    unsigned_wire(builder(amount=999)), current_slot=10
                ).wire_base64
            }
        )
    elif change == "features":
        plan = plan.model_copy(
            update={"features": plan.features.model_copy(update={"pattern_id": "unrelated"})}
        )
    elif change == "budget":
        changed = replace_resources(decode_wire(plan.prepared_wire_base64).message, 1, 1)
        plan = plan.model_copy(update={"prepared_wire_base64": unsigned_wire(changed)})
    elif change == "identity":
        plan = plan.model_copy(update={"prepared_identity": "made-up"})
    else:
        plan = plan.model_copy(update={"durable_nonce": True})
    with client() as rpc:
        result = execute_decision(plan, rpc=rpc, shadow=True)
        assert rpc.call_count == 0
    assert result.status == "unresolved"
    assert result.reason == "invalid_bound_plan"


def test_skip_requires_fresh_check_and_prediction_matches_active_artifact(tmp_path):
    wire = unsigned_wire(builder())
    ctx, estimator, registry = release_for_wire(tmp_path / "profiles.sqlite", wire)
    with client(400) as rpc:
        result = estimate_resources(
            wire, rpc=rpc, context=ctx, estimator=estimator, registry=registry, profile_id="review"
        )
        assert result.status == "accepted_prediction"
        assert result.compute_unit_limit == 700
        assert rpc.call_count == 0
        plan = result.plan
        assert plan is not None
        stale = execute_decision(plan, rpc=rpc, registry=registry)
        assert stale.status == "simulation_success"
        assert stale.reason == "fresh_profile_check_required"
        forged = plan.model_copy(
            update={"prediction": plan.prediction.model_copy(update={"compute_unit_limit": 1})}
        )
        rejected = execute_decision(forged, rpc=rpc, registry=registry, current_slot=400)
        assert rejected.status == "simulation_success"
        assert rejected.reason == "prediction_artifact_mismatch"


def test_reconciliation_finality_retries_and_training_origin(tmp_path):
    payer = Keypair()
    wire = unsigned_wire(builder(payer=payer.pubkey()))
    request = ShadowRequest(
        observation_id="retry",
        wire_base64=wire,
        context=context(),
        evidence_origin="synthetic",
        collection_method="offline-replay",
    )
    with ObservationStore(tmp_path / "shadow.sqlite") as store, client() as rpc:
        collect_shadow([request], store=store, rpc=rpc)
        message = decode_wire(store.get("retry")["result"]["unsigned_transaction_base64"]).message
        first = base64.b64encode(bytes(VersionedTransaction(message, [payer]))).decode()
        sig1 = store.attach_signature("retry", first, commitment="confirmed")
        failed = {
            "slot": 11,
            "transaction": [first, "base64"],
            "meta": {"err": {"failed": True}, "computeUnitsConsumed": 10},
        }
        assert store.reconcile(sig1, failed, commitment="finalized") == "finalized"
        store.attach_signature("retry", first, commitment="confirmed")
        assert store.get("retry")["outcome"] == "finalized"
        with pytest.raises(RecordConflict):
            store.attach_signature("retry", first, commitment="finalized")
        limits = store.get("retry")["result"]
        refreshed = replace_resources(
            message,
            limits["compute_unit_limit"],
            limits["loaded_accounts_data_size_limit"],
            blockhash=Hash.new_unique(),
        )
        second = base64.b64encode(bytes(VersionedTransaction(refreshed, [payer]))).decode()
        sig2 = store.attach_signature("retry", second, allow_blockhash_refresh=True)
        successful = {
            "slot": 12,
            "transaction": [second, "base64"],
            "meta": {
                "err": None,
                "computeUnitsConsumed": 650,
                "loadedAccountsDataSize": 128,
                "costUnits": 999999,
            },
        }
        assert store.reconcile(sig2, successful, commitment="finalized") == "finalized"
        execution = list(
            store.training_observations(source="historical", evidence_origin="synthetic")
        )
        simulation = list(
            store.training_observations(source="simulation", evidence_origin="synthetic")
        )
        assert len(execution) == len(simulation) == 1
        assert execution[0].label.compute_units == 650
        assert execution[0].source == "synthetic"
        assert execution[0].label_source == "historical"
        assert simulation[0].label_source == "simulation"
        assert execution[0].evidence_origin == "synthetic"
        assert next(store.export_records())["execution_signature_count"] == 2
        with pytest.raises(ValueError, match="provenance"):
            ResourceEstimator.fit(execution + simulation)


def test_verified_execution_excess_suspends_original_profile(tmp_path):
    payer = Keypair()
    wire = unsigned_wire(builder(payer=payer.pubkey()))
    ctx, estimator, registry = release_for_wire(tmp_path / "profiles.sqlite", wire)
    with ObservationStore(tmp_path / "outcomes.sqlite") as store, client(400) as rpc:
        result = estimate_resources(
            wire,
            rpc=rpc,
            context=ctx,
            estimator=estimator,
            registry=registry,
            profile_id="review",
            observation_id="actual",
        )
        assert result.status == "accepted_prediction" and result.plan is not None
        store.ingest("actual", {"evidence_origin": "local-runtime"})
        store.freeze_plan("actual", result.plan.model_dump(mode="json"))
        store.finish("actual", result.model_dump(mode="json"), stream="review", cursor=1)
        signed = VersionedTransaction(
            decode_wire(result.unsigned_transaction_base64).message, [payer]
        )
        signed_wire = base64.b64encode(bytes(signed)).decode()
        signature = store.attach_signature("actual", signed_wire)
        outcome = {
            "slot": 401,
            "transaction": [signed_wire, "base64"],
            "meta": {"err": None, "computeUnitsConsumed": 701, "loadedAccountsDataSize": 128},
        }
        store.reconcile(signature, outcome, commitment="finalized", registry=registry)
        store.reconcile(signature, outcome, commitment="finalized", registry=registry)
        assert registry.active_snapshot("review")[2] == "suspended"
        assert sum(e["action"] == "execution" for e in registry.audit_events()) == 1


def test_collection_latency_includes_durable_result_and_separates_rpc_retries(
    tmp_path, monkeypatch
):
    calls = []

    def handler(_):
        calls.append(1)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json=rpc_response())

    request = ShadowRequest(
        observation_id="timed",
        wire_base64=unsigned_wire(builder()),
        context=context(),
        evidence_origin="local-runtime",
        collection_method="offline-replay",
    )
    with ObservationStore(tmp_path / "timing.sqlite") as store:
        finish = store.finish

        def delayed_finish(*args, **kwargs):
            time.sleep(0.02)
            finish(*args, **kwargs)

        monkeypatch.setattr(store, "finish", delayed_finish)
        with RpcClient(
            "http://example.invalid",
            requests_per_second=10000,
            transport=httpx.MockTransport(handler),
        ) as rpc:
            collect_shadow([request], store=store, rpc=rpc)
        result = store.get("timed")["result"]
        assert result["resource_simulation_calls"] == 1
        assert result["rpc_attempts"] == 2
        assert result["retries"] == 1
        trace = store.preparation_traces()[0]
        assert trace.total_ms >= 20
        assert trace.rpc_attempts == 2 and trace.resource_estimation_calls == 1
        report = preparation_report([trace])
        assert report["collection_method"] == "offline-replay"
        assert report["mean_inference_ms"] is None
        assert next(store.export_records())["preparation_trace"] is not None


def test_completed_results_are_immutable_and_duplicates_idempotent(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    with ObservationStore(path) as first, ObservationStore(path) as second:
        first.ingest("same", {"message": "same"})
        result = {"status": "simulation_success", "label": 123}
        first.finish("same", result, stream="s", cursor=1)
        second.finish("same", result, stream="s", cursor=1)
        assert first.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        with pytest.raises(RecordConflict):
            second.finish("same", {"status": "unresolved"}, stream="s", cursor=2)
        assert first.get("same")["result"] == result
        assert first.checkpoint("s") == 1


def test_result_message_cannot_mutate_between_collection_and_reconciliation(tmp_path):
    payer = Keypair()
    request = ShadowRequest(
        observation_id="mutated",
        wire_base64=unsigned_wire(builder(payer=payer.pubkey())),
        context=context(),
        evidence_origin="synthetic",
    )
    with ObservationStore(tmp_path / "mutated.sqlite") as store, client() as rpc:
        collect_shadow([request], store=store, rpc=rpc)
        result = store.get("mutated")["result"]
        changed = bind_message(
            unsigned_wire(builder(amount=999, payer=payer.pubkey())), current_slot=10
        )
        message = replace_resources(
            decode_wire(changed.wire_base64).message,
            result["compute_unit_limit"],
            result["loaded_accounts_data_size_limit"],
        )
        result["unsigned_transaction_base64"] = unsigned_wire(message)
        import json

        with store.db:
            store.db.execute(
                "UPDATE observations SET result=? WHERE id='mutated'", (json.dumps(result),)
            )
        signed = base64.b64encode(bytes(VersionedTransaction(message, [payer]))).decode()
        with pytest.raises(ValueError, match="message changed"):
            store.attach_signature("mutated", signed)


def test_shadow_freezes_atomic_deployment_snapshot_and_records_ineligible_checks(tmp_path):
    wire = unsigned_wire(builder())
    ctx, estimator, registry = release_for_wire(tmp_path / "deployment.sqlite", wire)
    request = ShadowRequest(
        observation_id="deployment-first",
        wire_base64=wire,
        context=ctx,
        evidence_origin="synthetic",
    )
    with ObservationStore(tmp_path / "deployment-events.sqlite") as store:

        def handler(_):
            plan = store.get("deployment-first")["plan"]
            assert plan["deployment_evidence_status"] == "eligible"
            snapshot = plan["deployment_snapshot"]
            assert snapshot["eligible"] is True
            assert snapshot["current_slot"] == "400"
            assert snapshot["manifest"]["artifact_sha256"] == plan["artifact_digest"]
            assert len(snapshot["deployments"]) == len(set(plan["features"]["program_ids"]))
            assert "artifact_canonical_json" not in snapshot
            return httpx.Response(200, json=rpc_response(slot=400))

        with RpcClient("http://example.invalid", transport=httpx.MockTransport(handler)) as rpc:
            collect_shadow(
                [request],
                store=store,
                rpc=rpc,
                estimator=estimator,
                registry=registry,
                profile_id="review",
            )
        registry.force_simulation(True, actor="operator", reason="incident")
        with client(400) as rpc:
            collect_shadow(
                [request.model_copy(update={"observation_id": "deployment-second"})],
                store=store,
                rpc=rpc,
                estimator=estimator,
                registry=registry,
                profile_id="review",
            )
        first = store.get("deployment-first")["plan"]
        second = store.get("deployment-second")["plan"]
        assert first["deployment_snapshot"]["force_simulation"] is False
        assert second["deployment_evidence_status"] == "ineligible"
        assert second["deployment_snapshot"]["force_simulation"] is True
        assert second["deployment_snapshot"]["reason"] == "force_simulation"


def test_missing_deployment_snapshot_is_explicit_and_old_plans_default_unavailable():
    from cu_pilot.integration import DecisionPlan

    plan = prepare_decision(unsigned_wire(builder()), context=context())
    assert plan.deployment_evidence_status == "unavailable"
    assert plan.deployment_snapshot is None
    legacy = plan.model_dump(mode="json")
    del legacy["deployment_evidence_status"], legacy["deployment_snapshot"]
    assert DecisionPlan.model_validate(legacy).deployment_evidence_status == "unavailable"


def test_bootstrap_shadow_captures_watcher_cache_without_authorizing_skip(tmp_path):
    wire = unsigned_wire(builder())
    ctx = context()
    programs = bind_message(wire, current_slot=10).features.program_ids
    registry = ProfileRegistry(tmp_path / "bootstrap-registry.sqlite")
    reads = []

    class Reader:
        def get_multiple_accounts(self, addresses, **kwargs):
            reads.append(list(addresses))
            return {
                "context": {"slot": 10},
                "value": [
                    {
                        "owner": NATIVE_LOADER,
                        "executable": True,
                        "data": [base64.b64encode(b"fixture code").decode(), "base64"],
                    }
                    for _ in addresses
                ],
            }

    refresh_deployments(
        registry,
        Reader(),
        programs,
        current_slot=10,
        cluster_identity=ctx.cluster_identity,
        runtime_identity=ctx.runtime_identity,
    )
    request = ShadowRequest(
        observation_id="bootstrap",
        wire_base64=wire,
        context=ctx,
        evidence_origin="synthetic",
    )
    with ObservationStore(tmp_path / "bootstrap-events.sqlite") as store:

        def handler(_):
            plan = store.get("bootstrap")["plan"]
            assert plan["deployment_evidence_status"] == "observed_unreleased"
            snapshot = plan["deployment_snapshot"]
            assert snapshot["current_slot"] == "10"
            assert snapshot["context"] == ctx.context
            assert set(snapshot["program_ids"]) == set(programs)
            assert len(snapshot["deployments"]) == len(set(programs))
            assert snapshot["dependency_closure_verified"] is False
            assert snapshot["release_authorized"] is False
            assert snapshot["eligible"] is False
            assert snapshot["problems"] == {}
            assert plan["prediction"]["simulation_recommended"] is True
            assert plan["eligibility_reason"] == "profile_not_released"
            return httpx.Response(200, json=rpc_response())

        with RpcClient("http://example.invalid", transport=httpx.MockTransport(handler)) as rpc:
            collect_shadow([request], store=store, rpc=rpc, registry=registry)
            assert rpc.call_count == 1
        assert store.get("bootstrap")["result"]["status"] == "simulation_success"
        assert len(reads) == 1  # Collection consults the cache only.
        registry.watcher_failed()
        later = prepare_decision(wire, context=ctx, registry=registry)
        assert later.deployment_evidence_status == "ineligible"
        assert later.deployment_snapshot["watcher_failed"] is True
        assert store.get("bootstrap")["plan"]["deployment_snapshot"]["watcher_failed"] is False


def test_bootstrap_snapshot_preserves_missing_expired_and_wrong_context_evidence(tmp_path):
    wire = unsigned_wire(builder())
    programs = bind_message(wire, current_slot=10).features.program_ids
    registry = ProfileRegistry(tmp_path / "bootstrap-staleness.sqlite")
    options = dict(
        current_slot=10,
        context="local",
        cluster_identity="local-genesis",
        runtime_identity="local-runtime",
        now=1000,
    )
    missing = registry.cached_deployment_snapshot(programs, **options)
    assert missing["status"] == "unavailable"
    assert set(missing["problems"].values()) == {"deployment_evidence_missing"}
    registry.record_deployments(
        [
            deployment_identity(
                program,
                {
                    "owner": NATIVE_LOADER,
                    "executable": True,
                    "data": [base64.b64encode(b"fixture code").decode(), "base64"],
                },
                observed_slot=10,
                checked_at=1000,
                cluster_identity="local-genesis",
                runtime_identity="local-runtime",
            )
            for program in programs
        ]
    )
    assert (
        registry.cached_deployment_snapshot(programs, **options)["status"] == "observed_unreleased"
    )
    cases = (
        ({"now": 1061}, "deployment_time_stale_or_future"),
        ({"now": 999}, "deployment_time_stale_or_future"),
        ({"current_slot": 111}, "deployment_slot_stale_or_future"),
        ({"current_slot": 9}, "deployment_slot_stale_or_future"),
        ({"runtime_identity": "another-build"}, "deployment_context_mismatch"),
        ({"cluster_identity": "another-genesis"}, "deployment_context_mismatch"),
    )
    for change, reason in cases:
        snapshot = registry.cached_deployment_snapshot(programs, **(options | change))
        assert snapshot["status"] == "ineligible"
        assert set(snapshot["problems"].values()) == {reason}
        assert snapshot["deployments"]  # Preserve stale evidence for the audit.
        assert snapshot["eligible"] is False


def test_shadow_control_audit_resumes_from_durable_result_without_new_simulation(
    tmp_path, monkeypatch
):
    wire = unsigned_wire(builder())
    ctx, estimator, registry = release_for_wire(
        tmp_path / "controls.sqlite", wire, control_probability=1
    )
    request = ShadowRequest(
        observation_id="interrupted-control",
        wire_base64=wire,
        context=ctx,
        evidence_origin="synthetic",
    )
    original = registry.record_control
    audit_calls = []

    def interrupted(*args, **kwargs):
        audit_calls.append(1)
        if len(audit_calls) == 1:
            raise RuntimeError("test interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "record_control", interrupted)
    with ObservationStore(tmp_path / "control-events.sqlite") as store, client(400) as rpc:
        with pytest.raises(RuntimeError, match="interruption"):
            collect_shadow(
                [request],
                store=store,
                rpc=rpc,
                estimator=estimator,
                registry=registry,
                profile_id="review",
            )
        assert store.get(request.observation_id)["phase"] == "complete"
        assert rpc.call_count == 1
        result = collect_shadow(
            [request],
            store=store,
            rpc=rpc,
            estimator=estimator,
            registry=registry,
            profile_id="review",
        )
        assert result["deduplicated"] == 1
        assert rpc.call_count == 1
        controls = registry.control_records()
        assert len(controls) == 1 and controls[0]["outcome"] is not None


def test_resume_normalizes_valid_request_defaults_without_rewriting_old_evidence(tmp_path):
    request = ShadowRequest(
        observation_id="legacy-defaults",
        wire_base64=unsigned_wire(builder()),
        context=context(),
        evidence_origin="synthetic",
    )
    old_input = request.model_dump(mode="json")
    del old_input["context"]["max_deployment_age_slots"]
    del old_input["context"]["max_deployment_age_seconds"]
    old_plan = prepare_decision(request.wire_base64, context=request.context).model_dump(
        mode="json"
    )
    del old_plan["context"]["max_deployment_age_slots"]
    del old_plan["context"]["max_deployment_age_seconds"]
    del old_plan["deployment_snapshot"], old_plan["deployment_evidence_status"]
    with ObservationStore(tmp_path / "legacy-defaults.sqlite") as store, client() as rpc:
        store.ingest(request.observation_id, old_input)
        store.freeze_plan(request.observation_id, old_plan)
        assert collect_shadow([request], store=store, rpc=rpc)["completed"] == 1
        assert store.get(request.observation_id)["input"] == old_input
        assert store.get(request.observation_id)["plan"] == old_plan
        assert collect_shadow([request], store=store, rpc=rpc)["deduplicated"] == 1
        assert rpc.call_count == 1
        changed = request.model_dump(mode="json")
        changed["context"]["max_deployment_age_slots"] = 101
        with pytest.raises(RecordConflict, match="input"):
            store.ingest(request.observation_id, changed)
        store.ingest("untyped", {"context": {}})
        with pytest.raises(RecordConflict, match="input"):
            store.ingest("untyped", {"context": {"max_deployment_age_slots": 100}})
