"""Exact revision withdrawal rules using handcrafted offline lifecycle fixtures."""

import json

import pytest

from cu_pilot.lifecycle import ProfileRegistry, artifact_digest
from tests.test_lifecycle import ENV, manifest, release
from tests.test_lifecycle import artifact as artifact


def different_artifact(artifact: str, *, offset: int = 0) -> str:
    """Alter fixture evidence, without representing it as collected runtime data."""
    model = json.loads(artifact)
    for item in [model, *model["patterns"].values()]:
        for field in ("training_max_slot", "calibration_min_slot", "max_slot"):
            if item[field] is not None:
                item[field] = str(int(item[field]) + offset)
    for stats in model["patterns"].values():
        stats["quantile_loaded_accounts_bytes"] += 1
    return json.dumps(model)


def risk(registry: ProfileRegistry, artifact: str, revision: int = 1):
    calibration = json.loads(artifact)["calibration_min_slot"]
    return registry.revision_risk(
        "batch",
        revision,
        artifact_sha256=artifact_digest(artifact),
        calibration_min_slot=int(calibration) if calibration is not None else None,
    )


def test_unregistered_and_unreleased_proposals_do_not_need_activation(tmp_path, artifact):
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    assert risk(registry, artifact) is None
    assert registry.audit_events() == []
    registry.register(manifest(artifact), artifact, actor="test-operator")
    before = registry.audit_events()
    assert risk(registry, artifact) is None
    assert registry.audit_events() == before
    with pytest.raises(ValueError, match="No active profile"):
        registry.active_snapshot("batch")


def test_exact_revision_cannot_substitute_another_artifact(tmp_path, artifact):
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    registry.register(manifest(artifact), artifact, actor="test-operator")
    assert risk(registry, different_artifact(artifact)) == "artifact_mismatch"


def test_retired_revision_remains_blocked_after_replacement_is_active(tmp_path, artifact):
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    release(registry, artifact)
    registry.transition("batch", 1, "retired", actor="test-operator", reason="withdrawn")
    replacement = different_artifact(artifact)
    release(registry, replacement, 2)
    assert registry.check("batch", **ENV).eligible
    assert registry.check("batch", **ENV).revision == 2
    assert risk(registry, artifact) == "profile_retired"
    assert risk(registry, replacement, 2) is None


def test_suspended_revision_remains_blocked_after_replacement_is_active(tmp_path, artifact):
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    release(registry, artifact)
    # Both fixtures are calibrated after this suspension, permitting replacement
    # registration while preserving the old revision's terminal withdrawal.
    registry.suspend("batch", 1, actor="test-operator", reason="withdrawn", current_slot=200)
    replacement = different_artifact(artifact)
    release(registry, replacement, 2)
    assert registry.check("batch", **ENV).eligible
    assert risk(registry, artifact) == "profile_suspended"
    assert risk(registry, replacement, 2) is None


def test_quarantined_digest_cannot_escape_through_preexisting_revision(tmp_path, artifact):
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    registry.register(manifest(artifact), artifact, actor="test-operator")
    registry.register(manifest(artifact, 2), artifact, actor="test-operator")
    registry.suspend("batch", 1, actor="test-operator", reason="failure", current_slot=400)
    assert risk(registry, artifact, 2) == "artifact_quarantined"
    assert risk(registry, artifact, 3) == "artifact_quarantined"


def test_new_calibration_permits_unreleased_recovery_with_suspended_active_pointer(
    tmp_path, artifact
):
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    release(registry, artifact)
    registry.suspend("batch", 1, actor="test-operator", reason="failure", current_slot=400)
    replacement = different_artifact(artifact, offset=1000)
    registry.register(
        manifest(replacement, 2, evidence_max_slot=1399), replacement, actor="test-operator"
    )
    before = registry.audit_events()
    assert risk(registry, replacement, 2) is None
    assert registry.audit_events() == before
    active, _, status = registry.active_snapshot("batch")
    assert active.revision == 1 and status == "suspended"


def test_latest_suspension_including_retired_revision_uses_full_unsigned_slots(tmp_path, artifact):
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    registry.register(manifest(artifact), artifact, actor="test-operator")
    registry.transition("batch", 1, "retired", actor="test-operator", reason="withdrawn")
    newest = 2**64 - 2
    for slot in (9, 10, newest, 2**63 + 6):
        registry.suspend(
            "batch", 1, actor="test-operator", reason="late failure", current_slot=slot
        )
    digest = artifact_digest(different_artifact(artifact))
    before = registry.audit_events()
    for calibration in (None, 10, 2**63 + 7, newest):
        assert (
            registry.revision_risk(
                "batch", 2, artifact_sha256=digest, calibration_min_slot=calibration
            )
            == "profile_suspended"
        )
    assert (
        registry.revision_risk("batch", 2, artifact_sha256=digest, calibration_min_slot=newest + 1)
        is None
    )
    assert (
        registry.revision_risk(
            "unrelated-profile", 1, artifact_sha256=digest, calibration_min_slot=None
        )
        is None
    )
    assert risk(registry, artifact) == "profile_retired"
    assert registry.audit_events() == before


@pytest.mark.parametrize(
    "updates",
    [
        {"revision": True},
        {"revision": 0},
        {"revision": 2**53},
        {"artifact_sha256": "not-a-digest"},
        {"calibration_min_slot": -1},
        {"calibration_min_slot": 2**64},
        {"calibration_min_slot": True},
    ],
)
def test_invalid_revision_query_rejected(tmp_path, artifact, updates):
    registry = ProfileRegistry(tmp_path / "profiles.sqlite")
    values = dict(
        profile_id="batch",
        revision=1,
        artifact_sha256=artifact_digest(artifact),
        calibration_min_slot=0,
    )
    values.update(updates)
    with pytest.raises(ValueError):
        registry.revision_risk(**values)
