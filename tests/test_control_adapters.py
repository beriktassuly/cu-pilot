"""Offline adapter checks with real RpcClient validation and synthetic RPC responses."""

import json
import sqlite3

import httpx
import pytest
from test_binding import builder, rpc_response
from test_integration_review import release_for_wire

from cu_pilot.binding import unsigned_wire
from cu_pilot.integration import estimate_resources
from cu_pilot.rpc import RpcClient
from cu_pilot.shadow import ObservationStore, ShadowRequest, collect_shadow


@pytest.mark.parametrize("adapter", ["estimate", "shadow"])
@pytest.mark.parametrize(
    "value,excess",
    [
        ({"err": None, "unitsConsumed": 900}, True),
        ({"err": None, "loadedAccountsDataSize": 2000}, True),
        ({"err": None, "unitsConsumed": 900, "loadedAccountsDataSize": "bad"}, True),
        ({"err": None, "unitsConsumed": "bad", "loadedAccountsDataSize": 2000}, True),
        ({"err": None, "unitsConsumed": 100}, False),
        ({"err": None, "loadedAccountsDataSize": 100}, False),
        ({"err": {"InstructionError": [0, "Custom"]}, "unitsConsumed": 900}, False),
    ],
)
def test_partial_control_evidence_matches_lifecycle_policy(tmp_path, adapter, value, excess):
    wire = unsigned_wire(builder())
    ctx, estimator, registry = release_for_wire(
        tmp_path / "profiles.sqlite", wire, control_probability=1
    )
    with (
        ObservationStore(tmp_path / "shadow.sqlite") as store,
        RpcClient(
            "http://unused.invalid",
            requests_per_second=10000,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=rpc_response(value, slot=401))
            ),
        ) as rpc,
    ):
        for attempt in range(1 if excess else 3):
            identifier = f"control-{attempt}"
            if adapter == "estimate":
                result = estimate_resources(
                    wire,
                    rpc=rpc,
                    context=ctx,
                    estimator=estimator,
                    registry=registry,
                    profile_id="review",
                    observation_id=identifier,
                ).model_dump(mode="json")
            else:
                request = ShadowRequest(
                    observation_id=identifier,
                    wire_base64=wire,
                    context=ctx,
                    evidence_origin="synthetic",
                )
                collect_shadow(
                    [request],
                    store=store,
                    rpc=rpc,
                    estimator=estimator,
                    registry=registry,
                    profile_id="review",
                )
                result = store.get(identifier)["result"]
                # Replaying a durable result must not resimulate or advance the streak.
                assert (
                    collect_shadow(
                        [request],
                        store=store,
                        rpc=rpc,
                        estimator=estimator,
                        registry=registry,
                        profile_id="review",
                    )["deduplicated"]
                    == 1
                )
            assert result["status"] == "unresolved"
            assert result["compute_unit_limit"] is None
            assert result["loaded_accounts_data_size_limit"] is None
            assert rpc.call_count == attempt + 1
            with sqlite3.connect(registry.path) as db:
                evidence = json.loads(
                    db.execute(
                        "SELECT outcome FROM controls WHERE request_id=?", (identifier,)
                    ).fetchone()[0]
                )
                streak = db.execute("SELECT failure_streak FROM profiles").fetchone()[0]
            assert evidence["success"] is (value["err"] is None)
            for measured, rpc_field in (
                ("compute_units", "unitsConsumed"),
                ("loaded_accounts_bytes", "loadedAccountsDataSize"),
            ):
                raw = value.get(rpc_field)
                assert evidence[measured] == (
                    raw if type(raw) is int and 0 <= raw < 2**64 else None
                )
            assert evidence["slot"] == "401"
            assert evidence["elapsed_ms"] == result["simulation_failure"]["elapsed_ms"]
            assert streak == attempt + 1
            snapshot = registry.export_snapshot("review")
            should_suspend = excess or attempt == 2
            assert snapshot["state"] == ("suspended" if should_suspend else "active")
        assert snapshot["quarantine"] == (
            "control_resource_excess" if excess else "control_deterioration"
        )


@pytest.mark.parametrize(
    "value",
    [
        {"err": None, "unitsConsumed": 900},
        {"err": None, "loadedAccountsDataSize": 2000},
    ],
)
def test_resumed_shadow_audits_partial_excess_after_interrupted_registry_write(
    tmp_path, monkeypatch, value
):
    wire = unsigned_wire(builder())
    ctx, estimator, registry = release_for_wire(
        tmp_path / "profiles.sqlite", wire, control_probability=1
    )
    request = ShadowRequest(
        observation_id="interrupted-control",
        wire_base64=wire,
        context=ctx,
        evidence_origin="synthetic",
    )
    original = registry.record_control

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    with (
        ObservationStore(tmp_path / "shadow.sqlite") as store,
        RpcClient(
            "http://unused.invalid",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=rpc_response(value, slot=401))
            ),
        ) as rpc,
    ):
        monkeypatch.setattr(registry, "record_control", interrupt)
        with pytest.raises(KeyboardInterrupt):
            collect_shadow(
                [request],
                store=store,
                rpc=rpc,
                estimator=estimator,
                registry=registry,
                profile_id="review",
            )
        assert store.get(request.observation_id)["phase"] == "complete"
        assert registry.export_snapshot("review")["state"] == "active"
        monkeypatch.setattr(registry, "record_control", original)
        for _ in range(2):
            assert (
                collect_shadow(
                    [request],
                    store=store,
                    rpc=rpc,
                    estimator=estimator,
                    registry=registry,
                    profile_id="review",
                )["deduplicated"]
                == 1
            )
            assert rpc.call_count == 1
            assert registry.export_snapshot("review")["quarantine"] == "control_resource_excess"
