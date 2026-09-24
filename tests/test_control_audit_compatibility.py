"""Old partial control audits cannot silently acquire different outcomes on upgrade."""

import sqlite3

import httpx
import pytest
from test_binding import builder, rpc_response
from test_integration_review import release_for_wire

from cu_pilot.binding import unsigned_wire
from cu_pilot.rpc import RpcClient
from cu_pilot.shadow import ObservationStore, ShadowRequest, collect_shadow


def test_old_partial_control_audit_conflicts_without_overwriting_history(tmp_path, monkeypatch):
    wire = unsigned_wire(builder())
    context, estimator, registry = release_for_wire(
        tmp_path / "profiles.sqlite", wire, control_probability=1
    )
    request = ShadowRequest(
        observation_id="pre-fix-control",
        wire_base64=wire,
        context=context,
        evidence_origin="synthetic",
    )
    record_control = registry.record_control
    # Construct the previously committed state: durable partial simulation
    # evidence plus the old adapter's failed, empty control interpretation.
    monkeypatch.setattr(registry, "record_control", lambda *args, **kwargs: None)
    with (
        ObservationStore(tmp_path / "shadow.sqlite") as store,
        RpcClient(
            "http://unused.invalid",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json=rpc_response({"err": None, "unitsConsumed": 900}, slot=401),
                )
            ),
        ) as rpc,
    ):
        collect_shadow(
            [request],
            store=store,
            rpc=rpc,
            estimator=estimator,
            registry=registry,
            profile_id="review",
        )
        original_result = store.get(request.observation_id)["result"]
        record_control(
            request.observation_id,
            success=False,
            compute_units=None,
            loaded_accounts_bytes=None,
            current_slot=context.current_slot,
            elapsed_ms=original_result["full_preparation_ms"],
        )
        monkeypatch.setattr(registry, "record_control", record_control)
        with sqlite3.connect(registry.path) as db:
            original_control = db.execute("SELECT selection,outcome FROM controls").fetchall()
            original_profile = db.execute(
                "SELECT state,quarantine,failure_streak FROM profiles"
            ).fetchall()
        for _ in range(2):
            with pytest.raises(ValueError, match="Conflicting control outcome"):
                collect_shadow(
                    [request],
                    store=store,
                    rpc=rpc,
                    estimator=estimator,
                    registry=registry,
                    profile_id="review",
                )
            assert rpc.call_count == 1
            assert store.get(request.observation_id)["result"] == original_result
            with sqlite3.connect(registry.path) as db:
                assert (
                    db.execute("SELECT selection,outcome FROM controls").fetchall()
                    == original_control
                )
                assert (
                    db.execute("SELECT state,quarantine,failure_streak FROM profiles").fetchall()
                    == original_profile
                )
