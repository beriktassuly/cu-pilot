"""Deterministic lifecycle/loader contract tests; these are not runtime evidence."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cu_pilot.lifecycle import (
    IMMUTABLE_LOADERS,
    NATIVE_LOADER,
    UPGRADEABLE_LOADER,
    DeploymentEvidence,
    ProfileManifest,
    ProfileRegistry,
    artifact_digest,
    deployment_identity,
    refresh_deployments,
    runtime_identity_from_version,
)
from cu_pilot.lifecycle_cli import app
from cu_pilot.resources import ResourceEstimator
from cu_pilot.schemas import Features, Observation, ResourceLabel

PROGRAM = "11111111111111111111111111111111"
DEPENDENCY = "ComputeBudget111111111111111111111111111111"
ENV: dict[str, Any] = dict(
    current_slot=400,
    context="fixture",
    cluster_identity="local-genesis",
    runtime_identity="test-runtime-1",
    workload="controlled-batch",
    program_ids=[PROGRAM],
    now=1000.0,
)


def account(
    owner: str = NATIVE_LOADER, data: bytes = b"native", executable: bool = True
) -> dict[str, Any]:
    return dict(
        owner=owner, executable=executable, data=[base64.b64encode(data).decode(), "base64"]
    )


def evidence(program: str = PROGRAM, data: bytes = b"native") -> DeploymentEvidence:
    return deployment_identity(
        program,
        account(data=data),
        observed_slot=400,
        checked_at=1000.0,
        cluster_identity="local-genesis",
        runtime_identity="test-runtime-1",
    )


@pytest.fixture(scope="module")
def artifact() -> str:
    features = Features(
        pattern_id="shape-v1:lifecycle-fixture",
        version="legacy",
        signature_count=1,
        account_count=3,
        signer_count=1,
        writable_count=2,
        instruction_count=1,
        program_ids=(PROGRAM,),
        instruction_data_lengths=(12,),
        total_instruction_data_bytes=12,
        lookup_table_count=0,
        lookup_writable_count=0,
        lookup_readonly_count=0,
        requested_compute_units=1400000,
        requested_loaded_accounts_bytes=67108864,
    )
    # Handcrafted test rows use the simulation contract to exercise the operator
    # state machine; no resulting artifact is released or claimed as runtime data.
    observations = [
        Observation(
            record_id=str(slot),
            slot=slot,
            context="fixture",
            source="simulation",
            features=features,
            label=ResourceLabel(success=True, compute_units=1000, loaded_accounts_bytes=2000),
        )
        for slot in range(400)
    ]
    return ResourceEstimator.fit(observations).model.model_dump_json()


def manifest(artifact: str, revision: int = 1, **changes: Any) -> ProfileManifest:
    values = dict(
        profile_id="batch",
        revision=revision,
        artifact_sha256=artifact_digest(artifact),
        context="fixture",
        cluster_identity="local-genesis",
        runtime_identity="test-runtime-1",
        workload_allowlist=("controlled-batch",),
        deployment_bindings={PROGRAM: evidence().fingerprint},
        dependencies={PROGRAM: ()},
        dependency_closure_verified=True,
        budget_independent=True,
        evidence_min_slot=0,
        evidence_max_slot=399,
        provenance="simulation",
        control_probability=1.0,
    )
    values.update(changes)
    return ProfileManifest.model_validate(values)


def release(registry: ProfileRegistry, artifact: str, revision: int = 1, **changes: Any) -> None:
    registry.register(manifest(artifact, revision, **changes), artifact, actor="test-operator")
    registry.transition("batch", revision, "shadow", actor="test-operator", reason="review")
    registry.record_deployments([evidence()])
    registry.activate("batch", revision, actor="test-operator", reason="release", **ENV)


@pytest.fixture
def registry(tmp_path: Path, artifact: str) -> ProfileRegistry:
    result = ProfileRegistry(tmp_path / "profiles.sqlite")
    release(result, artifact)
    return result


def test_explicit_release_immutable_artifact_and_restart(
    registry: ProfileRegistry, artifact: str
) -> None:
    assert registry.check("batch", **ENV).eligible
    loaded_manifest, loaded = registry.load_active("batch")
    assert loaded_manifest.revision == 1
    assert artifact_digest(loaded.model.model_dump_json()) == loaded_manifest.artifact_sha256
    assert ProfileRegistry(registry.path).check("batch", **ENV).eligible
    registry.register(
        manifest(artifact), json.dumps(json.loads(artifact), indent=4), actor="operator"
    )
    with pytest.raises(ValueError, match="Conflicting"):
        registry.register(manifest(artifact, control_probability=0.2), artifact, actor="operator")
    assert [event["action"] for event in registry.audit_events()] == [
        "register",
        "shadow",
        "activate",
    ]


def test_legacy_artifact_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        artifact_digest('{"artifact_version":"cu-only"}')


def test_candidate_never_automatically_active(tmp_path: Path, artifact: str) -> None:
    registry = ProfileRegistry(tmp_path / "registry.sqlite")
    registry.register(manifest(artifact), artifact, actor="operator")
    assert registry.check("batch", **ENV).reason == "no_active_profile"
    with pytest.raises(ValueError, match="shadow"):
        registry.activate("batch", 1, actor="operator", reason="release", **ENV)


@pytest.mark.parametrize("field", ["dependency_closure_verified", "budget_independent"])
def test_explicit_assumptions_required(tmp_path: Path, artifact: str, field: str) -> None:
    registry = ProfileRegistry(tmp_path / "registry.sqlite")
    with pytest.raises(ValueError, match="release rejected"):
        release(registry, artifact, **{field: False})


def test_synthetic_cannot_release(tmp_path: Path, artifact: str) -> None:
    model = json.loads(artifact)
    model["source"] = "synthetic"
    synthetic = json.dumps(model)
    with pytest.raises(ValueError, match="Synthetic"):
        release(ProfileRegistry(tmp_path / "registry.sqlite"), synthetic, provenance="synthetic")


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"context": "different"}, "context_mismatch"),
        ({"workload": "unknown"}, "workload_not_allowed"),
        ({"runtime_identity": "changed"}, "runtime_or_cluster_mismatch"),
        ({"cluster_identity": "changed"}, "runtime_or_cluster_mismatch"),
        ({"program_ids": [DEPENDENCY]}, "untracked_program"),
    ],
)
def test_environment_mismatch_is_not_eligible(
    registry: ProfileRegistry, changes: dict[str, Any], reason: str
) -> None:
    assert registry.check("batch", **{**ENV, **changes}).reason == reason
    assert registry.check("batch", **ENV).eligible


@pytest.mark.parametrize("slot", [True, 1.5, "400", -1, 2**64])
def test_strict_current_slot(registry: ProfileRegistry, slot: Any) -> None:
    with pytest.raises(ValueError, match="Slot"):
        registry.check("batch", **{**ENV, "current_slot": slot})


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1, True])
def test_invalid_clock_never_accepts(registry: ProfileRegistry, now: float) -> None:
    with pytest.raises(ValueError, match="Time"):
        registry.check("batch", **{**ENV, "now": now})


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"now": 1061.0}, "deployment_evidence_stale"),
        ({"current_slot": 501}, "deployment_evidence_stale"),
        ({"current_slot": 399}, "deployment_evidence_stale"),
        ({"current_slot": 10400}, "stale_observations"),
    ],
)
def test_stale_evidence_suspends_and_cannot_be_refreshed_away(
    registry: ProfileRegistry, changes: dict[str, Any], reason: str
) -> None:
    assert registry.check("batch", **{**ENV, **changes}).reason == reason
    registry.record_deployments([evidence()])
    assert registry.check("batch", **ENV).reason == "profile_suspended"
    with pytest.raises(ValueError, match="unquarantined"):
        registry.activate("batch", 1, actor="operator", reason="retry", **ENV)


def test_program_and_dependency_changes_suspend(tmp_path: Path, artifact: str) -> None:
    registry = ProfileRegistry(tmp_path / "registry.sqlite")
    profile = manifest(
        artifact,
        deployment_bindings={
            PROGRAM: evidence().fingerprint,
            DEPENDENCY: evidence(DEPENDENCY).fingerprint,
        },
        dependencies={PROGRAM: (DEPENDENCY,), DEPENDENCY: ()},
    )
    registry.register(profile, artifact, actor="operator")
    registry.transition("batch", 1, "shadow", actor="operator", reason="review")
    registry.record_deployments([evidence(), evidence(DEPENDENCY)])
    registry.activate("batch", 1, actor="operator", reason="release", **ENV)
    assert registry.check("batch", **ENV).eligible
    registry.record_deployments([evidence(DEPENDENCY, b"upgrade")])
    assert registry.check("batch", **ENV).reason == "deployment_changed"


def test_watcher_failure_prevents_use_of_fresh_cache(registry: ProfileRegistry) -> None:
    registry.watcher_failed()
    assert registry.check("batch", **ENV).reason == "deployment_watcher_failed"
    assert registry.check("batch", **ENV).reason == "profile_suspended"


def test_emergency_switch_is_audited_and_reversible(registry: ProfileRegistry) -> None:
    registry.force_simulation(True, actor="operator", reason="incident")
    assert registry.check("batch", **ENV).reason == "force_simulation"
    registry.force_simulation(False, actor="operator", reason="resolved")
    assert registry.check("batch", **ENV).eligible
    assert registry.audit_events()[-1]["reason"] == "resolved"


def test_rollback_rechecks_and_preserves_suspended_revision(
    registry: ProfileRegistry, artifact: str
) -> None:
    different = json.loads(artifact)
    for stats in different["patterns"].values():
        stats["quantile_loaded_accounts_bytes"] = 2100
    release(registry, json.dumps(different), 2)
    registry.rollback("batch", 1, actor="operator", reason="rollback", **ENV)
    assert registry.check("batch", **ENV).revision == 1
    registry.activate("batch", 2, actor="operator", reason="release", **ENV)
    with pytest.raises(ValueError, match="stale"):
        registry.rollback("batch", 1, actor="operator", reason="rollback", **{**ENV, "now": 1100.0})
    assert registry.check("batch", **ENV).revision == 2
    registry.record_deployments([evidence(data=b"upgrade")])
    assert registry.check("batch", **ENV).reason == "deployment_changed"
    registry.record_deployments([evidence()])
    registry.rollback("batch", 1, actor="operator", reason="rollback", **ENV)
    with pytest.raises(ValueError, match="unquarantined"):
        registry.activate("batch", 2, actor="operator", reason="retry", **ENV)
    with pytest.raises(ValueError, match="new evidence"):
        registry.register(manifest(artifact, 3), artifact, actor="operator")


def select(registry: ProfileRegistry, request_id: str = "request-1", **changes: Any) -> Any:
    return registry.select_control(
        **dict(
            request_id=request_id,
            profile_id="batch",
            revision=1,
            decision_version="resources-v1:test",
            eligible=True,
            compute_unit_limit=1100,
            loaded_accounts_data_size_limit=3072,
            current_slot=400,
            **changes,
        )
    )


def test_control_selected_before_result_idempotence_and_joint_excess(
    registry: ProfileRegistry,
) -> None:
    with pytest.raises(ValueError, match="before"):
        registry.record_control(
            "missing",
            success=True,
            compute_units=1,
            loaded_accounts_bytes=1,
            current_slot=400,
            elapsed_ms=1,
        )
    assert select(registry).selected
    assert select(registry) == select(registry)
    assert registry.control_records()[0]["outcome"] is None
    registry.record_control(
        "request-1",
        success=True,
        compute_units=1000,
        loaded_accounts_bytes=4000,
        current_slot=400,
        elapsed_ms=1,
    )
    registry.record_control(
        "request-1",
        success=True,
        compute_units=1000,
        loaded_accounts_bytes=4000,
        current_slot=400,
        elapsed_ms=1,
    )
    assert registry.check("batch", **ENV).reason == "profile_suspended"
    assert len(registry.control_records()) == 1
    with pytest.raises(ValueError, match="Conflicting"):
        registry.record_control(
            "request-1",
            success=True,
            compute_units=1000,
            loaded_accounts_bytes=2000,
            current_slot=400,
            elapsed_ms=1,
        )


def test_failed_and_missing_control_labels_remain_failed(registry: ProfileRegistry) -> None:
    for index in range(3):
        select(registry, f"request-{index}")
        registry.record_control(
            f"request-{index}",
            success=False,
            compute_units=1,
            loaded_accounts_bytes=None,
            current_slot=400,
            elapsed_ms=1,
        )
    assert registry.check("batch", **ENV).reason == "profile_suspended"
    assert registry.audit_events()[-1]["reason"] == "control_deterioration"
    assert all(
        record["outcome"]["loaded_accounts_bytes"] is None for record in registry.control_records()
    )


_POLICY_CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "lifecycle_policy_contract.json").read_text()
)


@pytest.mark.parametrize("case", _POLICY_CONTRACT["freshness_cases"])
def test_shared_fractional_freshness_contract(
    tmp_path: Path, artifact: str, case: dict[str, Any]
) -> None:
    if not case["valid"]:
        with pytest.raises(ValidationError):
            manifest(artifact, max_deployment_age_seconds=case["seconds"])
        return
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    release(registry, artifact, max_deployment_age_seconds=case["seconds"])
    snapshot = registry.export_snapshot("batch", now=1000.0)
    assert snapshot["manifest"]["max_deployment_age_seconds"] == case["seconds"]
    assert registry.check("batch", **{**ENV, "now": 1000.0 + case["age"]}).eligible == case["fresh"]


@pytest.mark.parametrize("case", _POLICY_CONTRACT["control_cases"])
def test_shared_control_failure_threshold_contract(
    tmp_path: Path, artifact: str, case: dict[str, Any]
) -> None:
    path = tmp_path / "profiles.sqlite"
    release(ProfileRegistry(path), artifact, max_control_failure_streak=case["threshold"])
    snapshot = ProfileRegistry(path).export_snapshot("batch", now=1000.0)
    assert snapshot["manifest"]["max_control_failure_streak"] == case["threshold"]
    for index, success in enumerate(case["success"]):
        registry = ProfileRegistry(path)
        request_id = f"shared-{index}"
        select(registry, request_id)
        registry.record_control(
            request_id,
            success=success,
            compute_units=1000 if success else None,
            loaded_accounts_bytes=2000 if success else None,
            current_slot=400,
            elapsed_ms=1,
        )
        assert ProfileRegistry(path).check("batch", **ENV).eligible != case["suspended"][index]


def test_concurrent_reads_observe_whole_artifact_revisions(
    registry: ProfileRegistry, artifact: str
) -> None:
    registry.register(manifest(artifact, 2), artifact, actor="operator")
    registry.transition("batch", 2, "shadow", actor="operator", reason="review")

    def read() -> int:
        for _ in range(10):
            selected, payload, state = registry.active_snapshot("batch")
            assert artifact_digest(payload) == selected.artifact_sha256
            assert state == "active"
        return selected.revision

    with ThreadPoolExecutor(max_workers=4) as workers:
        reads = [workers.submit(read) for _ in range(3)]
        registry.activate("batch", 2, actor="operator", reason="release", **ENV)
        assert all(result.result() in (1, 2) for result in reads)


def test_verified_loader_v3_code_and_authority_changes() -> None:
    pointer = b"\0" * 32
    program = account(UPGRADEABLE_LOADER, (2).to_bytes(4, "little") + pointer)

    def identity(code: bytes, slot: int = 10, authority: int = 1) -> DeploymentEvidence:
        data = (
            (3).to_bytes(4, "little")
            + slot.to_bytes(8, "little")
            + bytes([authority])
            + b"x" * 32
            + code
        )
        return deployment_identity(
            PROGRAM,
            program,
            programdata=account(UPGRADEABLE_LOADER, data, False),
            observed_slot=11,
            checked_at=1000.0,
            cluster_identity="local",
            runtime_identity="runtime",
        )

    original = identity(b"elf")
    assert original.programdata_address == PROGRAM
    assert original.deployment_slot == 10
    assert original.fingerprint != identity(b"upgraded-elf").fingerprint
    assert original.fingerprint != identity(b"elf", authority=0).fingerprint
    with pytest.raises(ValueError, match="visibility"):
        identity(b"elf", slot=11)
    with pytest.raises(ValueError, match="state"):
        identity(b"elf", authority=2)


@pytest.mark.parametrize("loader", sorted(IMMUTABLE_LOADERS))
def test_immutable_loader_code_hash(loader: str) -> None:
    identity = deployment_identity(
        PROGRAM,
        account(loader, b"elf"),
        observed_slot=11,
        checked_at=1000.0,
        cluster_identity="local",
        runtime_identity="runtime",
    )
    assert identity.owner == loader
    assert identity.deployment_slot is None


def test_unsupported_loader_closed_program_and_invalid_encoding() -> None:
    for bad in [
        account(PROGRAM),
        account(executable=False),
        {**account(), "data": ["!", "base64"]},
    ]:
        with pytest.raises(ValueError):
            deployment_identity(
                PROGRAM,
                bad,
                observed_slot=11,
                checked_at=1000.0,
                cluster_identity="local",
                runtime_identity="runtime",
            )


class Reader:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls = 0

    def get_multiple_accounts(
        self, addresses: Any, *, min_context_slot: int, commitment: str
    ) -> Any:
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def test_bounded_watcher_real_account_contract(registry: ProfileRegistry) -> None:
    reader = Reader([dict(context=dict(slot=400), value=[account()])])
    observations = refresh_deployments(
        registry,
        reader,
        [PROGRAM],
        current_slot=400,
        cluster_identity="local-genesis",
        runtime_identity="test-runtime-1",
        checked_at=1000.0,
    )
    assert len(observations) == 1 and reader.calls == 1
    assert registry.check("batch", **ENV).eligible


def test_watcher_redacts_errors_and_fails_closed(registry: ProfileRegistry) -> None:
    reader = Reader([RuntimeError("https://user:password@provider.example/secret")])
    with pytest.raises(ValueError, match="cached evidence invalidated") as error:
        refresh_deployments(
            registry,
            reader,
            [PROGRAM],
            current_slot=400,
            cluster_identity="local-genesis",
            runtime_identity="test-runtime-1",
        )
    assert "password" not in str(error.value)
    assert registry.check("batch", **ENV).reason == "deployment_watcher_failed"


def test_watcher_rejects_incoherent_loader_v3_batches(registry: ProfileRegistry) -> None:
    program = account(UPGRADEABLE_LOADER, (2).to_bytes(4, "little") + b"\0" * 32)
    reader = Reader(
        [
            dict(context=dict(slot=400), value=[program]),
            dict(context=dict(slot=401), value=[account()]),
        ]
    )
    with pytest.raises(ValueError, match="refresh failed"):
        refresh_deployments(
            registry,
            reader,
            [PROGRAM],
            current_slot=400,
            cluster_identity="local-genesis",
            runtime_identity="test-runtime-1",
        )
    assert reader.calls == 2


def test_execution_audit_is_distinct_and_missing_data_not_imputed(
    registry: ProfileRegistry,
) -> None:
    values = dict(
        success=True,
        compute_units=1200,
        loaded_accounts_bytes=None,
        compute_unit_limit=1100,
        loaded_accounts_data_size_limit=3072,
        current_slot=400,
    )
    registry.record_execution("batch", 1, "execution-1", **values)
    registry.record_execution("batch", 1, "execution-1", **values)
    assert registry.control_records() == []
    assert registry.check("batch", **ENV).reason == "profile_suspended"
    assert registry.audit_events()[-1]["reason"] == "execution_resource_excess"
    assert len([event for event in registry.audit_events() if event["action"] == "execution"]) == 1
    with pytest.raises(ValueError, match="Conflicting"):
        registry.record_execution("batch", 1, "execution-1", **{**values, "success": False})


def test_failed_execution_partial_usage_not_successful_demand(registry: ProfileRegistry) -> None:
    registry.record_execution(
        "batch",
        1,
        "execution-1",
        success=False,
        compute_units=1400000,
        loaded_accounts_bytes=67108864,
        compute_unit_limit=1100,
        loaded_accounts_data_size_limit=3072,
        current_slot=400,
    )
    assert registry.check("batch", **ENV).eligible


def test_export_snapshot_integrity_portable_slots_and_suspension(registry: ProfileRegistry) -> None:
    snapshot = registry.export_snapshot("batch", now=1000.0)
    assert (
        hashlib.sha256(snapshot["artifact_canonical_json"].encode()).hexdigest()
        == snapshot["manifest"]["artifact_sha256"]
    )
    assert snapshot["manifest"]["evidence_max_slot"] == "399"
    assert snapshot["deployments"][0]["observed_slot"] == "400"
    assert snapshot["state"] == "active"
    registry.suspend("batch", 1, actor="operator", reason="investigate", current_slot=400)
    newer = registry.export_snapshot("batch", now=1001.0)
    assert newer["state"] == "suspended" and newer["quarantine"] == "investigate"
    assert newer["manifest"]["evidence_max_slot"] == "399"


def test_recovery_requires_fresh_calibration_and_new_release(
    registry: ProfileRegistry, artifact: str
) -> None:
    registry.suspend("batch", 1, actor="operator", reason="investigate", current_slot=400)
    updated = json.loads(artifact)
    # Controlled fixture simulates independent new fitting and calibration slots.
    for key in ("max_slot", "training_max_slot", "calibration_min_slot"):
        updated[key] = str(int(updated[key]) + 1000)
    for stats in updated["patterns"].values():
        for key in ("max_slot", "training_max_slot", "calibration_min_slot"):
            stats[key] = str(int(stats[key]) + 1000)
    fresh = json.dumps(updated)
    registry.register(
        manifest(fresh, 2, evidence_min_slot=1000, evidence_max_slot=1399), fresh, actor="operator"
    )
    registry.transition("batch", 2, "shadow", actor="operator", reason="requalified")
    new_evidence = evidence().model_copy(update=dict(observed_slot=1400, checked_at=1010.0))
    registry.record_deployments([new_evidence])
    registry.activate(
        "batch",
        2,
        actor="operator",
        reason="new evidence review",
        **{**ENV, "current_slot": 1400, "now": 1010.0},
    )
    assert registry.check("batch", **{**ENV, "current_slot": 1400, "now": 1010.0}).eligible
    with pytest.raises(ValueError, match="unquarantined"):
        registry.rollback(
            "batch",
            1,
            actor="operator",
            reason="unsafe rollback",
            **{**ENV, "current_slot": 1400, "now": 1010.0},
        )


def test_retired_revision_cannot_recover(registry: ProfileRegistry) -> None:
    registry.transition("batch", 1, "retired", actor="operator", reason="obsolete")
    assert registry.check("batch", **ENV).reason == "profile_retired"
    with pytest.raises(ValueError):
        registry.transition("batch", 1, "shadow", actor="operator", reason="invalid")


def test_control_selection_rejects_wrong_revision_and_conflicts(registry: ProfileRegistry) -> None:
    select(registry)
    with pytest.raises(ValueError, match="Conflicting"):
        registry.select_control(
            request_id="request-1",
            profile_id="batch",
            revision=1,
            decision_version="different",
            eligible=True,
            compute_unit_limit=1100,
            loaded_accounts_data_size_limit=3072,
            current_slot=400,
        )
    registry.suspend("batch", 1, actor="operator", reason="incident", current_slot=400)
    assert select(registry).selected  # retry returns the recorded original decision
    with pytest.raises(ValueError, match="active revision"):
        select(registry, "new-request")


def test_unselected_control_cannot_acquire_posthoc_outcome(
    registry: ProfileRegistry, artifact: str
) -> None:
    release(registry, artifact, 2, control_probability=0.0)
    selection = registry.select_control(
        request_id="request-1",
        profile_id="batch",
        revision=2,
        decision_version="v1",
        eligible=True,
        compute_unit_limit=1100,
        loaded_accounts_data_size_limit=3072,
        current_slot=400,
    )
    assert not selection.selected and selection.probability == 0.0
    with pytest.raises(ValueError, match="unselected"):
        registry.record_control(
            "request-1",
            success=True,
            compute_units=1000,
            loaded_accounts_bytes=2000,
            current_slot=400,
            elapsed_ms=1,
        )


def test_partial_refresh_does_not_clear_global_watcher_failure(tmp_path: Path) -> None:
    registry = ProfileRegistry(tmp_path / "registry.sqlite")
    registry.record_deployments([evidence(), evidence(DEPENDENCY)])
    registry.watcher_failed()
    registry.record_deployments([evidence()])
    with registry._connection() as db:
        assert (
            db.execute("SELECT value FROM settings WHERE key='watcher_failure'").fetchone()[0]
            == "true"
        )
    registry.record_deployments([evidence(), evidence(DEPENDENCY)])
    with registry._connection() as db:
        assert (
            db.execute("SELECT value FROM settings WHERE key='watcher_failure'").fetchone()[0]
            == "false"
        )


def test_cli_status_export_and_emergency_switch(registry: ProfileRegistry, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["status", str(registry.path), "batch"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["snapshot"]["state"] == "active"
    target = tmp_path / "snapshot.json"
    result = runner.invoke(app, ["export", str(registry.path), "batch", str(target)])
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text())["schema_version"] == "cu-pilot-release-snapshot-v1"
    assert not list(tmp_path.glob(".profile-*.tmp"))
    result = runner.invoke(
        app, ["force-simulation", str(registry.path), "--actor", "operator", "--reason", "incident"]
    )
    assert result.exit_code == 0, result.output
    assert registry.check("batch", **ENV).reason == "force_simulation"


def test_runtime_identity_requires_feature_build_identifier() -> None:
    assert (
        runtime_identity_from_version({"solana-core": "4.2.2", "feature-set": 123})
        == "rpc:4.2.2:feature-set:123"
    )
    for value in ({"solana-core": "4.2.2"}, {"solana-core": "4.2.2", "feature-set": True}):
        with pytest.raises(ValueError):
            runtime_identity_from_version(value)


def test_preregistered_revision_cannot_bypass_artifact_quarantine(
    registry: ProfileRegistry, artifact: str
) -> None:
    registry.register(manifest(artifact, 2), artifact, actor="operator")
    registry.transition("batch", 2, "shadow", actor="operator", reason="review")
    registry.suspend("batch", 1, actor="operator", reason="excess", current_slot=400)
    with pytest.raises(ValueError, match="quarantined artifact"):
        registry.activate("batch", 2, actor="operator", reason="renamed release", **ENV)


def test_late_control_suspends_active_artifact_alias(
    registry: ProfileRegistry, artifact: str
) -> None:
    select(registry)
    release(registry, artifact, 2)
    registry.record_control(
        "request-1",
        success=True,
        compute_units=1200,
        loaded_accounts_bytes=2000,
        current_slot=400,
        elapsed_ms=1,
    )
    assert registry.check("batch", **ENV).reason == "artifact_quarantined"
    assert registry.check("batch", **ENV).reason == "profile_suspended"


@pytest.mark.parametrize("deployment_slot", [280, 450])
def test_old_calibration_cannot_be_rebound_to_new_deployment(
    tmp_path: Path, artifact: str, deployment_slot: int
) -> None:
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    current = evidence().model_copy(
        update=dict(
            owner=UPGRADEABLE_LOADER,
            deployment_slot=deployment_slot,
            observed_slot=500,
        )
    )
    registry.record_deployments([current])
    registry.register(manifest(artifact), artifact, actor="operator")
    registry.transition("batch", 1, "shadow", actor="operator", reason="review")
    with pytest.raises(ValueError, match="deployment_not_covered_by_calibration"):
        registry.activate(
            "batch", 1, actor="operator", reason="invalid rebind", **{**ENV, "current_slot": 500}
        )


def test_deployment_before_calibration_and_atomic_decision_snapshot(
    tmp_path: Path, artifact: str
) -> None:
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    deployed = evidence().model_copy(update=dict(owner=UPGRADEABLE_LOADER, deployment_slot=279))
    registry.record_deployments([deployed])
    registry.register(manifest(artifact), artifact, actor="operator")
    registry.transition("batch", 1, "shadow", actor="operator", reason="review")
    registry.activate("batch", 1, actor="operator", reason="calibrated deployment", **ENV)
    checked = registry.check("batch", **ENV)
    assert checked.eligible and checked.evidence_snapshot is not None
    snapshot = checked.evidence_snapshot
    assert snapshot["eligible"] and snapshot["state"] == "active"
    assert snapshot["artifact_calibration_min_slot"] == "280"
    assert snapshot["deployments"][0]["deployment_slot"] == "279"
    assert snapshot["manifest"]["artifact_sha256"] == checked.artifact_sha256
    registry.record_deployments([deployed.model_copy(update={"fingerprint": "1" * 64})])
    rejected = registry.check("batch", **ENV)
    assert rejected.evidence_snapshot is not None
    assert rejected.evidence_snapshot["state"] == "suspended"
    assert rejected.evidence_snapshot["quarantine"] == "deployment_changed"
    assert snapshot["state"] == "active"  # Previously returned evidence is not overwritten.
