"""Adaptive application tests with real wires, signatures, releases and journals.

RPC/bridge responses and calibration rows are synthetic offline test fixtures.
They test application decisions and bindings, not actual Solana execution.
"""

from __future__ import annotations

import base64
import copy
import json
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction

from cu_pilot.binding import bind_message, decode_wire
from cu_pilot.lifecycle import NATIVE_LOADER, ProfileManifest, artifact_digest, deployment_identity
from cu_pilot.resources import ResourceEstimator, ResourcePolicy
from cu_pilot.rpc import RpcError, SimulationEstimate
from cu_pilot.schemas import Observation, ResourceLabel
from examples.payouts import app as application

COUNTS = (1, 2, 4, 8)
SLOT = 100


class PlannerFixture:
    def __init__(
        self,
        directory: Path,
        monkeypatch: pytest.MonkeyPatch,
        controls=(),
        *,
        compute_unit_cap=None,
    ):
        self.directory = directory
        self.reconciliation: list[dict[str, Any]] = []
        self.payer = Keypair.from_seed(bytes(range(32)))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.simulations: list[dict[str, Any]] = []
        self.simulation_limits: dict[int, tuple[int, int]] = {}
        self.simulation_errors: dict[int, str] = {}
        self.unsupported: set[int] = set()
        self.mutate_snapshot = False
        self.rpc_calls = 0
        self.method_counts: Counter[str] = Counter()
        self.transport_ms = 0.0
        self.client = SimpleNamespace(close=lambda: None)
        self.wires = {}
        for count in COUNTS:
            instructions = [
                transfer(
                    TransferParams(
                        from_pubkey=self.payer.pubkey(),
                        to_pubkey=Pubkey.from_bytes(bytes([50 + index]) * 32),
                        lamports=(index + 1) * 1000,
                    )
                )
                for index in range(count)
            ]
            tx = Transaction.new_unsigned(
                Message.new_with_blockhash(
                    instructions, self.payer.pubkey(), Hash.from_bytes(bytes([count]) * 32)
                )
            )
            self.wires[count] = base64.b64encode(bytes(tx)).decode()
        self.prepared = {
            count: bind_message(wire, current_slot=SLOT) for count, wire in self.wires.items()
        }
        monkeypatch.setattr(application, "Bridge", lambda _path: self)
        self.app = application.Application(directory, compute_unit_cap=compute_unit_cap)
        monkeypatch.setattr(self.app, "executor_balance", lambda: 100_000_000)
        monkeypatch.setattr(self.app.rpc, "get_slot", lambda: SLOT)
        monkeypatch.setattr(self.app.rpc, "simulate", self.simulate)
        monkeypatch.setattr(self.app, "refresh", lambda _slot, **_kw: None)
        monkeypatch.setattr(self.app, "envelope", self.envelope)
        programs = set(p for bound in self.prepared.values() for p in bound.features.program_ids)
        evidence = [
            deployment_identity(
                program,
                {
                    "owner": NATIVE_LOADER,
                    "data": [base64.b64encode(b"unit-native").decode(), "base64"],
                    "executable": True,
                },
                observed_slot=SLOT,
                checked_at=time.time(),
                cluster_identity=self.app.cluster,
                runtime_identity=self.app.runtime,
            )
            for program in sorted(programs)
        ]
        self.app.registry.record_deployments(evidence)
        self.app.deployment_bindings = {e.program_id: e.fingerprint for e in evidence}
        policy = ResourcePolicy(
            min_samples=12,
            min_calibration_samples=20,
            max_joint_underestimation_rate=0.15,
            calibration_fraction=0.5,
        )
        self.estimators = {}
        for count in COUNTS:
            rows = [
                Observation(
                    record_id=f"synthetic-{count}-{slot}",
                    slot=slot,
                    context=self.app.context,
                    source="simulation",
                    features=self.prepared[count].features,
                    label=ResourceLabel(
                        success=True, compute_units=count * 10_000, loaded_accounts_bytes=20_000
                    ),
                )
                for slot in range(10, 70)
            ]
            estimator = ResourceEstimator.fit(rows, policy)
            self.estimators[count] = estimator
            artifact = estimator.model.model_dump_json()
            manifest = ProfileManifest(
                profile_id=self.profile(count),
                revision=1,
                artifact_sha256=artifact_digest(artifact),
                context=self.app.context,
                cluster_identity=self.app.cluster,
                runtime_identity=self.app.runtime,
                workload_allowlist=("payout-queue-v1",),
                deployment_bindings=self.app.deployment_bindings,
                dependencies={p: () for p in programs},
                dependency_closure_verified=True,
                budget_independent=True,
                evidence_min_slot=10,
                evidence_max_slot=69,
                provenance="simulation",
                control_probability=1.0 if count in controls else 0.0,
            )
            self.app.registry.register(manifest, artifact, actor="synthetic-unit-fixture")
            self.app.registry.transition(
                self.profile(count),
                1,
                "shadow",
                actor="synthetic-unit-fixture",
                reason="synthetic fixture calibration",
            )
            self.app.registry.activate(
                self.profile(count),
                1,
                actor="synthetic-unit-fixture",
                reason="synthetic fixture release; not runtime evidence",
                **self.environment(count),
            )
        self.app.bundle = SimpleNamespace(
            digest="f" * 64,
            estimator_for=lambda state, **_kw: (
                self.profile(state.candidate_count),
                self.estimators[state.candidate_count],
                "supported",
            ),
        )

    @staticmethod
    def profile(count):
        return f"synthetic-count-{count}"

    def environment(self, count):
        return dict(
            current_slot=SLOT,
            context=self.app.context,
            cluster_identity=self.app.cluster,
            runtime_identity=self.app.runtime,
            workload="payout-queue-v1",
            program_ids=self.prepared[count].features.program_ids,
        )

    def suspend(self, *counts):
        for count in counts:
            self.app.registry.suspend(
                self.profile(count),
                1,
                actor="synthetic-unit-fixture",
                reason="explicit test suspension; no real resource failure claimed",
                current_slot=SLOT,
            )

    def envelope(self, candidate):
        count = candidate["count"]
        values = dict(
            candidate_count=count,
            remaining=16,
            deployment_bindings=self.app.deployment_bindings,
            prepared_identity=self.prepared[count].prepared_identity,
            cluster_identity=self.app.cluster,
            runtime_identity=self.app.runtime,
        )
        return SimpleNamespace(**values, model_dump=lambda **_kw: values, risk=lambda **_kw: None)

    def candidate(self, count):
        return {
            "count": count,
            "wire": self.wires[count],
            "serialized_size": len(base64.b64decode(self.wires[count])),
            "queue": self.verification()["queue"],
            "slot": SLOT,
            "supported": count not in self.unsupported,
            "ata_states": [],
            "evidence": [],
            "snapshot_digest": f"snapshot-{count}",
        }

    @staticmethod
    def verification():
        return {
            "correct": True,
            "duplicate_count": 0,
            "queue": {
                "cursor": 0,
                "paid_count": 0,
                "length": 16,
                "status": 0,
                "paused": False,
                "slot": SLOT,
            },
        }

    def call(self, action, **arguments):
        self.calls.append((action, arguments))
        if action == "info":
            return {
                "instance_id": "synthetic-adaptive-fixture",
                "runtime": {"version": "fixture"},
                "rpc_url": "http://127.0.0.1:9",
                "program": str(Pubkey.default()),
            }
        if action == "verify":
            return copy.deepcopy(self.verification())
        if action == "candidates":
            return {"candidates": [self.candidate(n) for n in arguments["counts"]]}
        if action == "snapshot":
            value = self.candidate(arguments["count"])
            if self.mutate_snapshot:
                value["snapshot_digest"] += "-changed"
            return value
        if action == "reconcile":
            return copy.deepcopy(self.reconciliation.pop(0))
        if action == "send":
            return {}
        if action == "sign":
            tx = VersionedTransaction(decode_wire(arguments["wire"]).message, [self.payer])
            return {
                "wire": base64.b64encode(bytes(tx)).decode(),
                "signature": str(tx.signatures[0]),
            }
        raise AssertionError(f"Unexpected action in before-submission fixture: {action}")

    def simulate(self, wire, **kwargs):
        count = next(n for n, bound in self.prepared.items() if bound.wire_base64 == wire)
        self.simulations.append({"count": count, "wire": wire, **kwargs})
        self.app.rpc.call_count += 1
        self.app.rpc.method_counts["simulateTransaction"] += 1
        if count in self.simulation_errors:
            code = self.simulation_errors[count]
            raise RpcError(
                "synthetic simulation outcome",
                code=code,
                evidence={
                    "success": code == "resource_cap_exceeded",
                    "error": None if code == "resource_cap_exceeded" else "transaction_error",
                    "compute_units": 1_400_000,
                    "loaded_accounts_bytes": 20_000,
                    "slot": SLOT,
                    "elapsed_ms": 1.0,
                },
            )
        compute, data = self.simulation_limits.get(count, (count * 11_000, 32_768))
        return SimulationEstimate(
            slot=SLOT,
            units_consumed=compute * 10 // 11,
            loaded_accounts_bytes=20_000,
            compute_unit_limit=compute,
            loaded_accounts_data_size_limit=data,
            elapsed_ms=1.0,
        )

    def signed_step(self, method="adaptive"):
        result = self.app.step("synthetic-queue", method=method, interrupt_after_sign=True)
        assert result["interrupted_after_sign"]
        row = self.app.store.db.execute(
            "SELECT body,phase,signature,wire FROM payout_steps ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        body = json.loads(row["body"])
        assert row["phase"] == "signed"
        signed = decode_wire(row["wire"])
        assert str(signed.signatures[0]) == row["signature"]
        final = decode_wire(body["decision"]["unsigned_transaction_base64"])
        assert bytes(signed.message) == bytes(final.message)
        assert (
            body["decision"]["plan"]["prepared_identity"]
            == self.prepared[body["chosen_count"]].prepared_identity
        )
        return body


def planning_audit(fixture):
    rows = fixture.app.store.db.execute(
        "SELECT body,status FROM payout_planning_attempts ORDER BY rowid"
    ).fetchall()
    assert len(rows) == 1
    return json.loads(rows[0]["body"]), rows[0]["status"]


@pytest.fixture
def planner(tmp_path, monkeypatch, request):
    fixture = PlannerFixture(tmp_path, monkeypatch, controls=getattr(request, "param", ()))
    yield fixture
    fixture.app.close()


def test_larger_simulation_beats_smaller_prediction_without_reactivation(planner):
    planner.suspend(8, 4)
    before = planner.app.registry.check(planner.profile(8), **planner.environment(8))
    assert not before.eligible
    body = planner.signed_step("adaptive")
    assert body["chosen_count"] == 8
    assert body["decision"]["status"] == "simulation_success"
    assert [s["count"] for s in planner.simulations] == [8]
    assert planner.simulations[0]["wire"] == body["decision"]["plan"]["prepared_wire_base64"]
    assert body["estimation_simulations"] == 1 and body["control_simulations"] == 0
    audit, status = planning_audit(planner)
    assert status == "selected" and audit["selected_count"] == 8
    assert audit["limit_source"] == "fresh_simulation"
    assert audit["estimation_simulations"] == 1 and audit["control_simulations"] == 0
    assert audit["rpc_calls"] == 1
    assert audit["rpc_method_counts"] == {"simulateTransaction": 1}
    assert audit["probes"][0]["prepared_identity"] == planner.prepared[8].prepared_identity
    after = planner.app.registry.check(planner.profile(8), **planner.environment(8))
    assert after.model_dump(exclude={"evidence_snapshot"}) == before.model_dump(
        exclude={"evidence_snapshot"}
    )


def test_legacy_learned_keeps_prediction_first_behavior(planner):
    """Retain an honest old-method comparator instead of silently changing it."""
    planner.suspend(8, 4)
    body = planner.signed_step("learned")
    assert body["chosen_count"] == 2
    assert body["decision"]["status"] == "accepted_prediction"
    assert planner.simulations == []


def test_largest_eligible_prediction_needs_no_probe(planner):
    body = planner.signed_step("adaptive")
    assert body["chosen_count"] == 8
    assert body["decision"]["status"] == "accepted_prediction"
    assert planner.simulations == []
    assert body["estimation_simulations"] == body["control_simulations"] == 0
    audit, status = planning_audit(planner)
    assert status == "selected" and audit["limit_source"] == "qualified_learned_prediction"
    assert audit["rpc_calls"] == 0 and audit["rpc_method_counts"] == {}


@pytest.mark.parametrize("limits", [(100_001, 32_768), (88_000, 1_048_577)])
def test_large_simulated_resource_limit_can_reject_larger_candidates(planner, limits):
    planner.suspend(8, 4)
    planner.simulation_limits.update({8: limits, 4: limits})
    body = planner.signed_step("adaptive")
    assert body["chosen_count"] == 2
    assert body["decision"]["status"] == "accepted_prediction"
    assert [s["count"] for s in planner.simulations] == [8, 4]
    assert body["estimation_simulations"] == 2
    assert body["control_simulations"] == 0
    assert planner.app.rpc.method_counts["simulateTransaction"] == 2


def test_larger_eligible_but_over_cap_prediction_can_be_resized_by_its_own_simulation(
    planner, monkeypatch
):
    monkeypatch.setattr(application, "CU_CAP", 70_000)
    planner.simulation_limits[8] = (66_000, 32_768)
    body = planner.signed_step("adaptive")
    assert body["chosen_count"] == 8
    assert body["decision"]["status"] == "simulation_success"
    assert body["decision"]["compute_unit_limit"] == 66_000
    assert [s["count"] for s in planner.simulations] == [8]


@pytest.mark.parametrize(
    "error", ["transaction_error", "missing_measurement", "stale_context", "transport"]
)
def test_unresolved_large_simulation_does_not_hide_error_with_smaller_transfers(planner, error):
    planner.suspend(8, 4)
    planner.simulation_errors[8] = error
    with pytest.raises(RuntimeError):
        planner.signed_step("adaptive")
    assert [s["count"] for s in planner.simulations] == [8]
    assert not any(action == "sign" for action, _ in planner.calls)
    assert planner.app.store.db.execute("SELECT COUNT(*) FROM payout_steps").fetchone()[0] == 0
    records = [
        planner.app.store.get(row[0])
        for row in planner.app.store.db.execute("SELECT id FROM observations")
    ]
    outcomes = [r["result"] for r in records if r["result"]]
    assert len(outcomes) == 1 and outcomes[0]["reason"] == error
    assert outcomes[0]["resource_simulation_calls"] == 1
    assert planner.app.rpc.method_counts["simulateTransaction"] == 1
    audit, status = planning_audit(planner)
    assert status == "stopped" and audit["selected_count"] is None
    assert audit["rpc_calls"] == 1
    assert audit["rpc_method_counts"] == {"simulateTransaction": 1}
    assert audit["estimation_simulations"] == 1 and audit["control_simulations"] == 0
    assert len(audit["probes"]) == 1 and audit["probes"][0]["reason"] == error


def test_successful_measurement_over_protocol_cap_is_resource_infeasibility(planner):
    planner.suspend(8, 4)
    planner.simulation_errors[8] = "resource_cap_exceeded"
    body = planner.signed_step("adaptive")
    assert body["chosen_count"] == 4
    assert [s["count"] for s in planner.simulations] == [8, 4]
    assert body["estimation_simulations"] == 2


def test_unsupported_large_account_state_blocks_unrelated_smaller_transfer(planner):
    planner.unsupported.add(8)
    with pytest.raises(RuntimeError):
        planner.signed_step("adaptive")
    assert planner.simulations == []
    assert not any(action == "sign" for action, _ in planner.calls)


def test_all_profiles_unavailable_stops_after_first_successful_largest_probe(planner):
    planner.suspend(*COUNTS)
    body = planner.signed_step("adaptive")
    assert body["chosen_count"] == 8
    assert [s["count"] for s in planner.simulations] == [8]
    assert body["estimation_simulations"] == 1
    assert all(
        not planner.app.registry.check(planner.profile(n), **planner.environment(n)).eligible
        for n in COUNTS
    )


def test_all_resource_probes_infeasible_are_bounded_once_per_candidate(planner):
    planner.suspend(*COUNTS)
    planner.simulation_limits = {count: (100_001, 32_768) for count in COUNTS}
    with pytest.raises(RuntimeError):
        planner.signed_step("adaptive")
    assert [s["count"] for s in planner.simulations] == [8, 4, 2, 1]
    assert planner.app.rpc.method_counts["simulateTransaction"] == 4
    audit, status = planning_audit(planner)
    assert status == "stopped" and audit["selected_count"] is None
    assert audit["rpc_calls"] == audit["max_simulation_probes"] == 4
    assert audit["estimation_simulations"] == 4 and audit["control_simulations"] == 0
    assert [p["count"] for p in audit["probes"]] == [8, 4, 2, 1]
    assert not any(action == "sign" for action, _ in planner.calls)


@pytest.mark.parametrize("planner", [(8,)], indirect=True)
def test_selected_control_is_simulated_and_counted_once(planner):
    body = planner.signed_step("adaptive")
    assert body["chosen_count"] == 8
    assert body["decision"]["status"] == "simulation_success"
    assert [s["count"] for s in planner.simulations] == [8]
    assert body["control_simulations"] == 1 and body["estimation_simulations"] == 0
    audit, status = planning_audit(planner)
    assert status == "selected"
    assert audit["rpc_calls"] == 1 and audit["control_simulations"] == 1
    assert audit["estimation_simulations"] == 0
    observed = [r for r in planner.app.registry.control_records() if r["outcome"] is not None]
    assert len(observed) == 1 and observed[0]["outcome"]["success"]


def test_state_mutation_after_successful_probe_requires_replan_before_signing(planner):
    planner.suspend(8, 4)
    planner.mutate_snapshot = True
    with pytest.raises(RuntimeError, match="state changed"):
        planner.signed_step("adaptive")
    assert [s["count"] for s in planner.simulations] == [8]
    assert not any(action == "sign" for action, _ in planner.calls)
    phases = [row[0] for row in planner.app.store.db.execute("SELECT phase FROM payout_steps")]
    assert all(phase == "planned" for phase in phases)


def test_adaptive_signed_decision_recovers_after_restart_without_new_planning(planner):
    """A probe does not loosen identical-byte rebroadcast or signature reconciliation."""
    planner.suspend(8, 4)
    body = planner.signed_step("adaptive")
    row = planner.app.store.db.execute("SELECT signature,wire FROM payout_steps").fetchone()
    signature, wire = row["signature"], row["wire"]
    signed = decode_wire(wire)
    confirmed = {
        "transaction": [wire, "base64"],
        "slot": SLOT + 1,
        "meta": {
            "err": None,
            "computeUnitsConsumed": 80_000,
            "fee": 5_000,
            "preBalances": [100_000_000] * len(signed.message.account_keys),
            "postBalances": [99_995_000, *([100_000_000] * (len(signed.message.account_keys) - 1))],
        },
    }
    progressed = copy.deepcopy(planner.verification())
    progressed["queue"].update(cursor=8, paid_count=8)
    planner.reconciliation = [
        {"transaction": None, "verification": planner.verification()},
        {"transaction": confirmed, "verification": progressed},
    ]
    planner.app.close()
    planner.app = application.Application(planner.directory)
    recovered = planner.app.reconcile_pending("synthetic-queue")
    assert recovered["success"] and recovered["recovered"]
    assert recovered["signature"] == signature and recovered["fee_lamports"] == 5_000
    assert recovered["verification"]["queue"]["cursor"] == body["chosen_count"] == 8
    assert recovered["verification"]["duplicate_count"] == 0
    sends = [args["wire"] for action, args in planner.calls if action == "send"]
    assert sends == [wire]
    assert len([action for action, _ in planner.calls if action == "sign"]) == 1
    assert len([action for action, _ in planner.calls if action == "candidates"]) == 1
    assert planner.app.reconcile_pending("synthetic-queue") is None
    assert not planner.app.registry.check(planner.profile(8), **planner.environment(8)).eligible
