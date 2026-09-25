"""Offline failure-state tests with real signatures and the actual durable journal.

The scripted RPC outcomes are explicitly synthetic unit fixtures, not evidence of
Solana execution. Runtime integration separately validates the payout program.
"""

from __future__ import annotations

import base64
import copy
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction

from cu_pilot.binding import decode_wire
from cu_pilot.integration import EstimationContext, prepare_decision
from examples.payouts import app as application
from examples.payouts import benchmark
from examples.payouts.benchmark import distribution, summarize, summarize_controls


class ScriptedBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.evidence: list[dict[str, Any]] = []
        self.send_failure: Exception | None = None
        self.rpc_calls = 0
        self.rpc_method_counts: dict[str, int] = {}
        self.method_counts: Counter[str] = Counter()
        self.transport_ms = 0.0
        self.client = SimpleNamespace(close=lambda: None)

    def call(self, action: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((action, arguments))
        if action == "info":
            return {
                "instance_id": "synthetic-offline-fixture",
                "runtime": {"version": "fixture"},
                "rpc_url": "http://127.0.0.1:9",
                "program": str(Pubkey.default()),
            }
        if action == "reconcile":
            return copy.deepcopy(self.evidence.pop(0))
        if action == "send":
            if self.send_failure is not None:
                raise self.send_failure
            return {}
        raise AssertionError(f"Recovery unexpectedly chose a new action: {action}")


def verification(cursor: int, *, correct: bool = True) -> dict[str, Any]:
    return {
        "correct": correct,
        "duplicate_count": 0,
        "queue": {
            "cursor": cursor,
            "paid_count": cursor,
            "length": 4,
            "status": int(cursor == 4),
            "last_decision": "unused-unit-fixture",
            "last_model": "0" * 64,
        },
    }


def evidence(
    transaction: dict[str, Any] | None, cursor: int = 0, *, correct: bool = True
) -> dict[str, Any]:
    return {"transaction": transaction, "verification": verification(cursor, correct=correct)}


@pytest.fixture
def pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bridge = ScriptedBridge()
    monkeypatch.setattr(application, "Bridge", lambda _path: bridge)
    app = application.Application(tmp_path)
    payer = Keypair.from_seed(bytes(range(32)))
    instructions = [
        transfer(
            TransferParams(
                from_pubkey=payer.pubkey(),
                to_pubkey=Pubkey.from_bytes(bytes([value]) * 32),
                lamports=1,
            )
        )
        for value in (50, 60)
    ]
    wire = base64.b64encode(
        bytes(
            Transaction.new_unsigned(
                Message.new_with_blockhash(
                    instructions,
                    payer.pubkey(),
                    Hash.default(),
                )
            )
        )
    ).decode()
    plan = prepare_decision(
        wire,
        context=EstimationContext(
            context="synthetic:payout-recovery",
            current_slot=100,
            cluster_identity="synthetic-local",
            runtime_identity="synthetic-local",
            workload="offline-recovery-test",
            budget_independent=True,
        ),
        observation_id="execute:synthetic-step:2",
    )
    decision = app._baseline_result(plan, 10000, 100000, "synthetic-unit-test")
    app.store.ingest(plan.observation_id, {"fixture": "synthetic"})
    app.store.freeze_plan(plan.observation_id, plan.model_dump(mode="json"))
    app.store.finish(
        plan.observation_id, decision.model_dump(mode="json"), stream="synthetic-tests", cursor=1
    )
    assert decision.unsigned_transaction_base64 is not None
    signed = VersionedTransaction(
        decode_wire(decision.unsigned_transaction_base64).message, [payer]
    )
    signed_wire = base64.b64encode(bytes(signed)).decode()
    signature = app.store.attach_signature(plan.observation_id, signed_wire, commitment="confirmed")
    body = {
        "id": "synthetic-step",
        "queue": "fixture-queue",
        "cursor": 0,
        "chosen_count": 2,
        "model_digest": "0" * 64,
        "method": "learned",
        "mode": "prediction",
        "decision": decision.model_dump(mode="json"),
        "options": [],
        "estimation_simulations": 0,
        "control_simulations": 0,
        "inference_and_candidate_ms": 1.0,
    }
    app._store_step("synthetic-step", "fixture-queue", body)
    with app.store.db:
        app.store.db.execute(
            "UPDATE payout_steps SET phase='signed',signature=?,wire=? WHERE id=?",
            (signature, signed_wire, "synthetic-step"),
        )
    transaction = {
        "transaction": [signed_wire, "base64"],
        "slot": 101,
        "meta": {
            "err": None,
            "computeUnitsConsumed": 600,
            "fee": 5000,
            "preBalances": [10000000] * len(signed.message.account_keys),
            "postBalances": [9995000, *([10000000] * (len(signed.message.account_keys) - 1))],
        },
    }
    yield app, bridge, transaction, signed_wire, signature, body
    app.close()


def phase(app) -> str:
    return app.store.db.execute("SELECT phase FROM payout_steps").fetchone()[0]


def sends(bridge: ScriptedBridge) -> list[dict[str, Any]]:
    return [arguments for action, arguments in bridge.calls if action == "send"]


def test_confirmed_lost_response_reconciles_without_resending(pending) -> None:
    app, bridge, transaction, _, signature, _ = pending
    bridge.evidence = [evidence(transaction, 2)]
    result = app.reconcile_pending("fixture-queue")
    assert result is not None and result["verification"]["correct"]
    assert phase(app) == "confirmed"
    assert sends(bridge) == []
    assert app.reconcile_pending("fixture-queue") is None
    assert (
        app.store.db.execute(
            "SELECT COUNT(*) FROM outcomes WHERE signature=?", (signature,)
        ).fetchone()[0]
        == 1
    )


def test_recovery_retains_decision_and_labels_without_inventing_preparation_timing(pending) -> None:
    app, bridge, transaction, _, _, body = pending
    bridge.evidence = [evidence(transaction, 2)]
    result = app.reconcile_pending("fixture-queue")
    assert result is not None
    assert result["decision"] == body["decision"]
    assert result["chosen_count"] == 2
    assert result["success"] is True
    assert result["compute_units"] == 600
    assert result["loaded_accounts_bytes"] is None
    assert result["fee_lamports"] == 5000
    assert result.get("preparation_ms") is None
    assert result.get("rpc_calls") is None


def test_crash_after_sign_resends_only_the_identical_bound_transaction(pending) -> None:
    app, bridge, transaction, signed_wire, _, _ = pending
    bridge.evidence = [evidence(None), evidence(transaction, 2)]
    result = app.reconcile_pending("fixture-queue")
    assert result is not None and result["verification"]["queue"]["cursor"] == 2
    assert sends(bridge) == [{"wire": signed_wire}]
    assert phase(app) == "confirmed"
    assert app.reconcile_pending("fixture-queue") is None


def test_uncertain_send_survives_restart_and_never_exceeds_retry_bound(pending, tmp_path) -> None:
    app, bridge, transaction, signed_wire, _, _ = pending
    bridge.send_failure = httpx.ReadTimeout("synthetic lost response")
    bridge.evidence = [evidence(None), evidence(None)]
    with pytest.raises(RuntimeError, match="uncertain signature"):
        app.reconcile_pending("fixture-queue")
    assert phase(app) == "uncertain"
    assert app.setting("rebroadcast:synthetic-step") == 1
    # A second application instance reopens the same actual SQLite journal.
    reopened = application.Application(tmp_path)
    try:
        bridge.evidence = [evidence(None), evidence(None)]
        with pytest.raises(RuntimeError, match="uncertain signature"):
            reopened.reconcile_pending("fixture-queue")
        bridge.evidence = [evidence(None)]
        with pytest.raises(RuntimeError, match="uncertain signature"):
            reopened.reconcile_pending("fixture-queue")
        assert sends(bridge) == [{"wire": signed_wire}, {"wire": signed_wire}]
        assert reopened.setting("rebroadcast:synthetic-step") == application.MAX_ATTEMPTS
        # Confirmation after the retry limit is still reconciled, not rebroadcast.
        bridge.evidence = [evidence(transaction, 2)]
        assert reopened.reconcile_pending("fixture-queue")["verification"]["correct"]
        assert len(sends(bridge)) == application.MAX_ATTEMPTS
    finally:
        reopened.close()


def test_advanced_cursor_without_signature_is_not_assumed_confirmed(pending) -> None:
    app, bridge, _, _, _, _ = pending
    bridge.evidence = [evidence(None, 2)]
    with pytest.raises(RuntimeError, match="uncertain signature"):
        app.reconcile_pending("fixture-queue")
    assert phase(app) == "uncertain" and sends(bridge) == []


@pytest.mark.parametrize(("cursor", "correct"), [(1, True), (2, False)])
def test_confirmation_still_requires_verified_payment_progress(pending, cursor, correct) -> None:
    app, bridge, transaction, _, _, _ = pending
    bridge.evidence = [evidence(transaction, cursor, correct=correct)]
    with pytest.raises(RuntimeError, match="approved balances/cursor"):
        app.reconcile_pending("fixture-queue")
    assert phase(app) == "signed" and sends(bridge) == []


def test_reconciliation_rejects_a_different_final_message(pending) -> None:
    app, bridge, transaction, _, _, _ = pending
    tampered = copy.deepcopy(transaction)
    tampered["transaction"][0] = base64.b64encode(
        bytes(
            Transaction.new_unsigned(
                Message.new_with_blockhash([], None, Hash.default()),
            )
        )
    ).decode()
    bridge.evidence = [evidence(tampered, 2)]
    with pytest.raises(ValueError):
        app.reconcile_pending("fixture-queue")
    assert phase(app) == "signed" and sends(bridge) == []


def test_confirmed_failure_is_recorded_without_treating_partial_usage_as_success(pending) -> None:
    app, bridge, transaction, _, _, _ = pending
    failed = copy.deepcopy(transaction)
    failed["meta"]["err"] = {"InstructionError": [1, "Custom"]}
    bridge.evidence = [evidence(failed)]
    app.reconcile_pending("fixture-queue")
    assert phase(app) == "failed" and sends(bridge) == []
    assert app.reconcile_pending("fixture-queue") is None


@pytest.mark.parametrize(
    ("error", "quarantine"),
    [
        ({"InstructionError": [2, "ComputationalBudgetExceeded"]}, True),
        ("MaxLoadedAccountsDataSizeExceeded", True),
        ({"InstructionError": [2, {"Custom": 1}]}, False),
        ({"InstructionError": [2, "InvalidAccountData"]}, False),
        (None, False),
    ],
)
def test_only_confirmed_resource_exhaustion_quarantines_the_exact_revision(
    pending, monkeypatch, error, quarantine
) -> None:
    app, _, transaction, _, _, body = pending
    body = copy.deepcopy(body)
    body["decision"]["plan"].update(profile_id="synthetic-profile", profile_revision=7)
    transaction = copy.deepcopy(transaction)
    transaction["meta"]["err"] = error
    suspended = []
    monkeypatch.setattr(
        app.registry, "suspend", lambda *args, **kwargs: suspended.append((args, kwargs))
    )
    app.audit_budget_failure(body, transaction)
    assert bool(suspended) is quarantine
    if quarantine:
        assert suspended[0][0] == ("synthetic-profile", 7)
        assert suspended[0][1]["reason"] == "confirmed_resource_budget_exhaustion"
        assert suspended[0][1]["current_slot"] == transaction["slot"]


def test_sampled_controls_keep_original_prediction_denominators() -> None:
    selected = {
        "selected": True,
        "compute_unit_limit": 100,
        "loaded_accounts_data_size_limit": 1000,
    }
    records = [
        {"selection": selected, "outcome": None},
        {
            "selection": selected,
            "outcome": {"success": True, "compute_units": 110, "loaded_accounts_bytes": 900},
        },
        {
            "selection": selected,
            "outcome": {"success": True, "compute_units": None, "loaded_accounts_bytes": 1100},
        },
        {
            "selection": selected,
            "outcome": {"success": False, "compute_units": 100, "loaded_accounts_bytes": 900},
        },
        {"selection": {**selected, "selected": False}, "outcome": None},
    ]
    result = summarize_controls(records)
    assert result["selected_candidates"] == 4
    assert result["selected_without_outcome"] == 1
    assert result["observed_controls"] == 3 and result["failed_controls"] == 1
    assert result["compute_underestimation_rate"] == 1
    assert result["loaded_data_underestimation_rate"] == 0.5
    assert result["joint_exceedance_rate"] == 1 and result["paired_labels"] == 1


def test_budget_failures_retain_fees_attempts_and_retries_without_demand_labels() -> None:
    successful = {
        "decision": {"compute_unit_limit": 100, "loaded_accounts_data_size_limit": 1000},
        "mode": "prediction",
        "success": True,
        "compute_units": 90,
        "loaded_accounts_bytes": 900,
        "fee_lamports": 5000,
        "rent_deposit_lamports": 0,
        "rpc_retries": 1,
        "transaction": {"meta": {"err": None}},
    }
    failed = {
        **successful,
        "success": False,
        "compute_units": 100,
        "loaded_accounts_bytes": 1001,
        "rpc_retries": 0,
    }
    steps = [
        {
            **failed,
            "cursor": 0,
            "transaction": {
                "meta": {"err": {"InstructionError": [2, "ComputationalBudgetExceeded"]}}
            },
        },
        {**successful, "cursor": 0},
        {
            **failed,
            "cursor": 2,
            "transaction": {"meta": {"err": "MaxLoadedAccountsDataSizeExceeded"}},
        },
        {**successful, "cursor": 2},
        {
            **failed,
            "cursor": 4,
            "transaction": {"meta": {"err": {"InstructionError": [2, "InvalidAccountData"]}}},
        },
    ]
    result = summarize(
        [{"steps": steps, "verification": verification(4), "queue_completion_ms": 10}]
    )
    assert result["transactions"] == 5 and result["successful_transactions"] == 2
    assert result["failed_attempts"] == 3 and result["confirmed_attempts"] == 5
    assert result["confirmed_compute_budget_exhaustions"] == 1
    assert result["confirmed_loaded_data_budget_exhaustions"] == 1
    assert result["confirmed_budget_exhaustion_rate"] == 2 / 5
    assert result["application_retries_after_confirmed_budget_failure"] == 2
    assert result["rpc_retries"] == 2 and result["total_retries"] == 4
    assert result["execution_paired_labels"] == 2
    assert result["compute_underestimations"] == result["data_underestimations"] == 0
    assert result["observed_fee_lamports"] == 25000
    measured = {"steps": [{**step, "complete_step_ms": 4.0} for step in steps]}
    assert benchmark.active_step_time(measured) == 20.0
    # Losing a recovery interval makes the complete active sum unmeasured;
    # it must not silently drop failed attempts or fill the gap with zero.
    measured["steps"][0]["complete_step_ms"] = None
    assert benchmark.active_step_time(measured) is None


def test_summary_preserves_missing_labels_timings_and_measured_denominators() -> None:
    common = {
        "decision": {"compute_unit_limit": 100, "loaded_accounts_data_size_limit": 1000},
        "success": True,
        "mode": "prediction",
        "compute_units": 90,
        "loaded_accounts_bytes": None,
        "estimation_simulations": 0,
        "control_simulations": 0,
        "rpc_calls": 10,
        "rpc_retries": 0,
        "fee_lamports": 5000,
        "rent_deposit_lamports": 2039280,
        "preparation_ms": 5.0,
        "local_transport_ms": 1.0,
        "complete_step_ms": 8.0,
        "state_deployment_reads": 3,
        "rpc_method_counts": {"getMultipleAccounts": 3},
    }
    recovered = {
        **common,
        "recovered": True,
        "compute_units": None,
        "loaded_accounts_bytes": 1500,
        "rpc_calls": None,
        "preparation_ms": None,
        "complete_step_ms": None,
        "state_deployment_reads": None,
        "rpc_method_counts": None,
    }
    result = summarize(
        [
            {
                "steps": [common, recovered],
                "verification": verification(4),
                "queue_completion_ms": 20.0,
            }
        ]
    )
    assert result["transactions"] == 2 and result["recovered_decisions"] == 1
    assert result["execution_compute_labels"] == 1
    assert result["execution_loaded_data_labels"] == 1
    assert result["execution_paired_labels"] == 0
    assert result["loaded_data_underestimation_rate"] == 1.0
    assert result["joint_exceedance_rate"] is None
    assert result["preparation_ms"]["measured_count"] == 1
    assert result["preparation_ms"]["mean"] == 5.0
    assert result["state_deployment_reads"] is None
    assert result["rpc_calls"] is None
    assert result["measurement_coverage"]["rpc_calls"]["observed_sum"] == 10
    assert result["observed_fee_lamports"] == 10000
    assert result["rent_deposit_lamports"] == 4078560
    assert distribution([None, None]) == {
        "measured_count": 0,
        "mean": None,
        "median": None,
        "p95": None,
    }
    controlled = {
        **common,
        "mode": "fallback",
        "method": "learned",
        "control_simulations": 1,
        "options": [{"eligible": True}, {"eligible": False}],
    }
    control_summary = summarize(
        [{"steps": [controlled], "verification": verification(4), "queue_completion_ms": 20}]
    )
    assert control_summary["simulation_decisions"] == 1
    assert control_summary["control_only_decisions"] == 1
    assert control_summary["fallback_decisions"] == 0
    assert control_summary["model_candidate_eligibility_rate"] == 0.5


@pytest.mark.parametrize(
    "error",
    ["MaxLoadedAccountsDataSizeExceeded", {"InstructionError": [0, "ComputationalBudgetExceeded"]}],
)
def test_confirmed_budget_failure_persists_forced_simulation_after_restart(
    pending, tmp_path, error
) -> None:
    app, bridge, transaction, _, signature, _ = pending
    failed = copy.deepcopy(transaction)
    failed["meta"]["err"] = error
    bridge.evidence = [evidence(failed)]
    result = app.reconcile_pending("fixture-queue")
    assert result["success"] is False and result["confirmed_resource_exhaustion"] is True
    assert result["fee_lamports"] == 5000 and result["verification"]["queue"]["cursor"] == 0
    assert app.setting("force_simulation:fixture-queue") is True
    reopened = application.Application(tmp_path)
    try:
        assert reopened.setting("force_simulation:fixture-queue") is True
        assert reopened.reconcile_pending("fixture-queue") is None
        row = reopened.store.db.execute("SELECT * FROM payout_steps").fetchone()
        assert row["phase"] == "failed" and row["signature"] == signature
        assert json.loads(row["outcome"])["success"] is False
        assert sends(bridge) == []
    finally:
        reopened.close()


def test_confirmed_failure_limit_is_durable_and_blocks_new_signing(pending, monkeypatch) -> None:
    app, bridge, transaction, _, _, body = pending
    failed = copy.deepcopy(transaction)
    failed["meta"]["err"] = {"InstructionError": [0, "ComputationalBudgetExceeded"]}
    bridge.evidence = [evidence(failed)]
    app.reconcile_pending("fixture-queue")
    with app.store.db:
        app.store.db.execute(
            "INSERT INTO payout_steps SELECT "
            "'second-failure',queue,body,phase,signature,wire,outcome "
            "FROM payout_steps WHERE id='synthetic-step'"
        )
    original = bridge.call

    def call(action, **arguments):
        if action == "verify":
            return {**verification(0), "queue": {**verification(0)["queue"], "paused": False}}
        return original(action, **arguments)

    monkeypatch.setattr(bridge, "call", call)
    monkeypatch.setattr(app, "executor_balance", lambda: 100_000_000)
    with pytest.raises(RuntimeError, match="bounded failed-attempt limit"):
        app.step("fixture-queue")
    assert sends(bridge) == []
    assert app.store.db.execute("SELECT COUNT(*) FROM payout_steps").fetchone()[0] == 2


def test_suspended_profile_can_sign_a_fresh_simulation_fallback(pending, monkeypatch) -> None:
    """The release gate must not reject measured fallback merely for retaining provenance."""
    app, bridge, transaction, _, _, body = pending
    failed = copy.deepcopy(transaction)
    failed["meta"]["err"] = {"InstructionError": [0, "ComputationalBudgetExceeded"]}
    bridge.evidence = [evidence(failed)]
    app.reconcile_pending("fixture-queue")
    payer = Keypair.from_seed(bytes(range(32)))
    previous = decode_wire(body["decision"]["unsigned_transaction_base64"]).message
    next_message = Message.new_with_compiled_instructions(
        previous.header.num_required_signatures,
        previous.header.num_readonly_signed_accounts,
        previous.header.num_readonly_unsigned_accounts,
        previous.account_keys,
        Hash.from_bytes(bytes([9]) * 32),
        previous.instructions,
    )
    q = {**verification(0)["queue"], "paused": False, "slot": 100}
    candidate = {
        "queue": q,
        "count": 1,
        "slot": 100,
        "supported": True,
        "ata_states": [],
        "evidence": [],
        "snapshot_digest": "frozen-test-state",
        "wire": base64.b64encode(bytes(Transaction.new_unsigned(next_message))).decode(),
        "serialized_size": 400,
    }
    original_call = bridge.call

    def call(action, **arguments):
        if action == "verify":
            return {**verification(0), "queue": q}
        if action == "candidates":
            return {"candidates": [copy.deepcopy(candidate)]}
        if action == "snapshot":
            return copy.deepcopy(candidate)
        if action == "sign":
            signed = VersionedTransaction(decode_wire(arguments["wire"]).message, [payer])
            return {
                "wire": base64.b64encode(bytes(signed)).decode(),
                "signature": str(signed.signatures[0]),
            }
        return original_call(action, **arguments)

    monkeypatch.setattr(bridge, "call", call)
    monkeypatch.setattr(app, "executor_balance", lambda: 100_000_000)
    monkeypatch.setattr(app.rpc, "get_slot", lambda: 100)
    monkeypatch.setattr(app, "refresh", lambda _slot: None)
    state = SimpleNamespace(
        model_dump=lambda **_: {"deployment_bindings": {}}, prepared_identity="fixture"
    )
    monkeypatch.setattr(app, "envelope", lambda _: state)
    app.bundle = SimpleNamespace(
        digest="f" * 64, estimator_for=lambda *_, **__: ("suspended-profile", None, "supported")
    )
    original_prepare = application.prepare_decision

    def prepare(wire, **kwargs):
        kwargs.update(registry=None, profile_id=None)
        plan = original_prepare(wire, **kwargs)
        return plan.model_copy(
            update={
                "profile_id": "suspended-profile",
                "profile_revision": 7,
                "artifact_digest": "f" * 64,
                "eligibility_reason": "profile_suspended",
            }
        )

    simulated = []

    def simulate(plan, **kwargs):
        simulated.append(kwargs)
        return app._baseline_result(plan, 10000, 100000, "profile_suspended").model_copy(
            update={"status": "simulation_success", "resource_simulation_calls": 1}
        )

    monkeypatch.setattr(application, "prepare_decision", prepare)
    monkeypatch.setattr(application, "execute_decision", simulate)
    monkeypatch.setattr(app.registry, "check", lambda *_, **__: SimpleNamespace(eligible=False))
    result = app.step("fixture-queue", count_cap=1, interrupt_after_sign=True)
    assert result["interrupted_after_sign"] is True
    assert len(simulated) == 1 and simulated[0]["force_simulation"] is True
    row = app.store.db.execute(
        "SELECT body,phase FROM payout_steps WHERE id=?", (result["id"],)
    ).fetchone()
    frozen = json.loads(row["body"])
    assert row["phase"] == "signed" and frozen["mode"] == "fallback"
    assert frozen["recovery_simulation"] and frozen["retry_after_confirmed_failure"]
    assert frozen["decision"]["plan"]["profile_id"] == "suspended-profile"
    assert sends(bridge) == []


@pytest.mark.parametrize(
    ("consumed", "limit", "logs", "expected"),
    [
        (67800, 67800, ["runtime"], "compute"),
        (67799, 67800, ["runtime"], None),
        (67800, 67800, ["spoof"], None),
        (67800, 67800, ["other_error"], None),
        (67800, 67800, [], None),
        (67800, 67800, None, None),
        (None, None, ["runtime"], None),
        (True, 1, ["runtime"], None),
    ],
)
def test_bpf_meter_failure_requires_exact_runtime_evidence(consumed, limit, logs, expected) -> None:
    meter = (
        "Program 4KkbhqTuK19kWqwrYF5sUdhYteq8ABsCvWm6AArTk8vK "
        "failed: exceeded CUs meter at BPF instruction"
    )
    lines = {
        "runtime": meter,
        "spoof": "Program log: " + meter,
        "other_error": meter.replace("exceeded CUs meter", "access violation"),
    }
    transaction = {
        "meta": {
            "err": {"InstructionError": [0, "ProgramFailedToComplete"]},
            "computeUnitsConsumed": consumed,
            "logMessages": [lines[line] for line in logs] if logs is not None else None,
        }
    }
    assert application.confirmed_budget_failure(transaction, limit) == expected


def test_checkpoint_interrupted_replacement_preserves_prior_run(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution-report.json"
    prior = {"pending_run": {"queue": "same-funded-queue"}, "runs": [{"signature": "paid"}]}
    benchmark.save_checkpoint(path, prior)
    replace = benchmark.os.replace

    def interrupted(source, destination):
        assert Path(source).exists() and Path(destination) == path
        raise OSError("synthetic interruption before atomic replacement")

    monkeypatch.setattr(benchmark.os, "replace", interrupted)
    with pytest.raises(OSError, match="synthetic interruption"):
        benchmark.save_checkpoint(path, {**prior, "status": "later"})
    assert json.loads(path.read_text()) == prior
    monkeypatch.setattr(benchmark.os, "replace", replace)
    completed = {**prior, "status": "resumed"}
    benchmark.save_checkpoint(path, completed)
    assert json.loads(path.read_text()) == completed
    assert not path.with_suffix(".json.tmp").exists()
