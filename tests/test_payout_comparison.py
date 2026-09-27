"""Offline comparison accounting and reproducibility contracts; no timing claims."""

from __future__ import annotations

import json
from collections import Counter
from types import SimpleNamespace

import pytest

from examples.payouts import compare


def verification(paid=16):
    return {
        "correct": True,
        "duplicate_count": 0,
        "queue": {"paid_count": paid, "cursor": paid, "status": 1 if paid == 16 else 0},
    }


def transaction_step(*, success=True, phase="confirmed", compute_units=80_000):
    return {
        "decision": {"compute_unit_limit": 100_000},
        "success": success,
        "durable_phase": phase,
        "mode": "prediction",
        "compute_units": compute_units,
        "loaded_accounts_bytes": None,
        "estimation_simulations": 0,
        "control_simulations": 0,
        "fee_lamports": 5000,
        "rent_deposit_lamports": 0,
        "transaction": {
            "slot": 100,
            "meta": {
                "err": None if success else {"InstructionError": [0, "ComputationalBudgetExceeded"]}
            },
        },
    }


def case(
    *,
    identifier="case",
    method="adaptive_derivation",
    phase="measurement",
    block="measurement:0:all_accounts_exist",
    status="complete",
    paid=16,
    elapsed=100,
    steps=None,
):
    return {
        "case_id": identifier,
        "block_id": block,
        "phase": phase,
        "scenario": "all_accounts_exist",
        "method": method,
        "payments": 16,
        "status": status,
        "verification": verification(paid),
        "queue_completion_ms": elapsed,
        "steps": steps or [],
    }


def test_schedule_is_frozen_balanced_and_separates_selected_stress():
    config = compare.CampaignConfig(repeats=3, stress_blocks=2)
    first = compare.schedule(config)
    assert first == compare.schedule(config)
    assert first != compare.schedule(config.model_copy(update={"seed": config.seed + 1}))
    assert len({row["case_id"] for row in first}) == len(first) == 12 + 36 + 8
    for block in {row["block_id"] for row in first}:
        rows = [row for row in first if row["block_id"] == block]
        assert {row["method"] for row in rows} == set(compare.METHODS)
        assert len(rows) == 4
        assert len({(row["scenario"], row["existing"], row["payments"]) for row in rows}) == 1
    assert all(row["scenario"] == "derivation_tail" for row in first if row["phase"] == "stress")
    assert all(
        row["scenario"] != "derivation_tail" for row in first if row["phase"] == "measurement"
    )


def test_failure_fees_censored_labels_and_incomplete_denominators_are_retained():
    success = transaction_step()
    failure = transaction_step(success=False, phase="failed", compute_units=100_000)
    rows = [
        case(steps=[success]),
        case(identifier="failed", status="error", paid=0, elapsed=None, steps=[failure]),
    ]
    result = compare.metrics(rows, planned=3)
    assert result["attempted_queues"] == 2
    assert result["not_attempted_queues"] == 1
    assert result["completed_queues"] == 1
    assert result["completion_rate"] == 0.5
    assert result["timed_completed_queues"] == 1
    assert result["failed_transactions"] == 1
    assert result["confirmed_budget_exhaustions"] == 1
    assert result["compute_labels"] == 1  # Failed usage is censored.
    assert result["data_labels"] == 0
    assert result["missing_data_labels"] == 1
    assert result["fees_lamports"] == 10_000
    assert result["pending_or_unverified_payments"] == 16
    assert result["compute_excess"]["mean"] == 20_000


def test_unsigned_plan_is_not_reported_as_unknown_transaction():
    unsigned = {
        "decision": {"compute_unit_limit": 100_000},
        "durable_phase": "planned",
        "mode": "prediction",
        "estimation_simulations": 0,
        "control_simulations": 0,
    }
    row = case(status="error", paid=0, elapsed=None, steps=[unsigned])
    result = compare.metrics([row], planned=1)
    assert result["planned_decisions"] == 1
    assert result["unknown_transactions"] == 0
    assert result["requested_cu_all_attempts"] == 0
    assert result["successful_transactions"] == result["failed_transactions"] == 0


def test_missing_rpc_measurements_do_not_turn_into_zero_calls():
    first = case()
    first["run_counters"] = {"rpc_calls": 30}
    first["all_counters"] = {"rpc_calls": 40, "methods": {"getSlot": 40}}
    second = case(identifier="interrupted", status="interrupted", paid=0, elapsed=None)
    result = compare.metrics([first, second], planned=2)
    assert result["run_rpc_calls"] is None
    assert result["run_rpc_calls_observed"] == 30
    assert result["run_rpc_missing_cases"] == 1


def test_paired_latency_excludes_warmup_stress_failed_and_untimed_completion():
    rows = [
        case(method="adaptive_derivation", elapsed=90),
        case(identifier="comparator", method="always_simulate", elapsed=100),
        case(identifier="unpaired", method="adaptive_derivation", block="unpaired", elapsed=1),
        case(
            identifier="failure",
            method="always_simulate",
            block="unpaired",
            status="error",
            paid=0,
            elapsed=None,
        ),
        case(identifier="warm-h", phase="warmup", block="warm", elapsed=1000),
        case(
            identifier="warm-c", phase="warmup", block="warm", method="always_simulate", elapsed=1
        ),
        case(identifier="stress-h", phase="stress", block="stress", elapsed=1000),
        case(
            identifier="stress-c",
            phase="stress",
            block="stress",
            method="always_simulate",
            elapsed=1,
        ),
        case(identifier="untimed-h", block="untimed", elapsed=None),
        case(identifier="untimed-c", block="untimed", method="always_simulate", elapsed=100),
    ]
    paired = compare.paired_differences(rows, seed=17)
    result = paired["always_simulate"]
    assert result["complete_matched_blocks"] == 1
    assert result["hybrid_minus_comparator_ms"]["mean"] == -10
    assert result["descriptive_paired_bootstrap_95pct_interval_ms"] is None


def frozen_campaign(directory, monkeypatch):
    source = {"sha256": "a" * 64, "files": {"module.py": "b" * 64}}
    monkeypatch.setattr(compare, "source_fingerprint", lambda: source)
    directory.mkdir()
    (directory / "private/collection").mkdir(parents=True)
    (directory / "plan.json").write_text('{"plan": "frozen"}')
    names = (
        "candidate.json",
        "hybrid-candidate.json",
        "baselines.json",
        "count-only-candidate.json",
        "observations.jsonl",
    )
    for name in names:
        (directory / "private/collection" / name).write_text(name)
    for method in compare.METHODS:
        arm = directory / "private/arms" / method
        arm.mkdir(parents=True)
        for name in names[:3]:
            (arm / name).write_text(name)
    manifest = {
        "plan_sha256": compare.sha_file(directory / "plan.json"),
        "source": source,
        "artifacts": {
            name: compare.sha_file(directory / "private/collection" / name) for name in names
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "private/frozen.json").write_text(
        json.dumps({"manifest_sha256": compare.sha_file(directory / "manifest.json")})
    )
    return manifest


@pytest.mark.parametrize(
    "relative,reason",
    [
        ("plan.json", "pre-collection plan changed"),
        ("manifest.json", "campaign manifest changed"),
        ("private/collection/observations.jsonl", "frozen collection or model changed"),
        ("private/arms/adaptive_derivation/hybrid-candidate.json", "arm artifact changed"),
    ],
)
def test_frozen_comparison_rejects_parameter_or_evidence_mutation(
    tmp_path, monkeypatch, relative, reason
):
    directory = tmp_path / "campaign"
    expected = frozen_campaign(directory, monkeypatch)
    assert compare.verify_frozen(directory) == expected
    target = directory / relative
    target.write_text(target.read_text() + " ")
    with pytest.raises(ValueError, match=reason):
        compare.verify_frozen(directory)


def test_frozen_comparison_rejects_changed_executable_source(tmp_path, monkeypatch):
    directory = tmp_path / "campaign"
    frozen_campaign(directory, monkeypatch)
    monkeypatch.setattr(compare, "source_fingerprint", lambda: {"sha256": "changed"})
    with pytest.raises(ValueError, match="source or executed ELF/JS changed"):
        compare.verify_frozen(directory)


def test_execute_case_preserves_failed_attempt_and_rpc_calls(tmp_path, monkeypatch):
    rpc = SimpleNamespace(call_count=0, retry_count=0, method_counts=Counter())
    bridge = SimpleNamespace(rpc_calls=0, transport_failures=0, method_counts=Counter())

    def bridge_call(action, **_kwargs):
        bridge.rpc_calls += 1
        bridge.method_counts[action] += 1
        return verification(0) if action == "verify" else {}

    bridge.call = bridge_call

    def create(**_kwargs):
        rpc.call_count += 4
        rpc.method_counts["setup"] += 4
        return {"queue": {"address": "queue"}}

    def run(*_args):
        rpc.call_count += 5
        rpc.method_counts["execution"] += 5
        raise RuntimeError("fixture stopped after confirmed resource failure")

    app = SimpleNamespace(
        rpc=rpc,
        bridge=bridge,
        info={},
        create=create,
        run=run,
        registry=SimpleNamespace(control_records=lambda: []),
    )
    failure = transaction_step(success=False, phase="failed", compute_units=100_000)
    monkeypatch.setattr(compare, "durable_steps", lambda _app, _queue: [failure])
    if hasattr(compare, "durable_planning_attempts"):
        monkeypatch.setattr(compare, "durable_planning_attempts", lambda *_args: [])
    row = next(
        row
        for row in compare.schedule(compare.CampaignConfig(repeats=1))
        if row["phase"] == "measurement"
    )
    manifest = {
        "config": compare.CampaignConfig(repeats=1).model_dump(),
        "bank": {"instance_id": "fixture-bank"},
    }
    path = tmp_path / "case.json"
    result = compare.execute_case(app, row, manifest, path)
    assert result == json.loads(path.read_text())
    assert result["status"] == "error"
    assert result["queue_completion_ms"] is None
    assert result["steps"] == [failure]
    assert result["run_counters"]["rpc_calls"] == 5
    assert result["all_counters"]["rpc_calls"] == 11
    assert compare.metrics([result], planned=1)["fees_lamports"] == 5000


def test_orphan_planning_simulations_count_without_double_counting_selected_step():
    step = transaction_step()
    step.update(id="selected", estimation_simulations=1, control_simulations=1)
    row = case(steps=[step])
    row["planning_attempts"] = [
        {"id": "selected", "audit": {"estimation_simulations": 1, "control_simulations": 1}},
        {"id": "aborted", "audit": {"estimation_simulations": 3, "control_simulations": 0}},
    ]
    result = compare.metrics([row], planned=1)
    assert result["orphan_planning_attempts"] == 1
    assert result["estimation_simulations"] == 4
    assert result["control_simulations"] == 1


def test_interrupted_signature_reconciliation_keeps_original_timing_unknown(tmp_path, monkeypatch):
    rpc = SimpleNamespace(call_count=0, retry_count=0, method_counts=Counter())
    bridge = SimpleNamespace(rpc_calls=0, transport_failures=0, method_counts=Counter())
    pending = {"decision": {"compute_unit_limit": 100_000}, "durable_phase": "signed"}
    states = [pending]
    events = []

    def reconcile(_queue):
        events.append("reconcile")
        rpc.call_count += 1
        rpc.method_counts["reconcile"] += 1
        states[:] = [transaction_step()]

    def call(action, **_kwargs):
        assert action == "verify"
        events.append("verify")
        bridge.rpc_calls += 1
        bridge.method_counts["verify"] += 1
        return verification()

    bridge.call = call
    app = SimpleNamespace(rpc=rpc, bridge=bridge, reconcile_pending=reconcile)
    monkeypatch.setattr(compare, "durable_steps", lambda *_args: list(states))
    monkeypatch.setattr(compare, "durable_planning_attempts", lambda *_args: [])
    prior = case(status="started", paid=0, elapsed=13, steps=[pending])
    prior["queue"] = "existing-queue"
    path = tmp_path / "case.json"
    compare.reconcile_case(app, prior, path)
    assert events == ["reconcile", "verify"]
    saved = json.loads(path.read_text())
    assert saved["status"] == "interrupted"
    assert saved["queue_completion_ms"] is None
    assert saved["steps"][0]["success"] is True
    assert saved["resume_reconciliation"][0]["counters"]["rpc_calls"] == 2
    result = compare.metrics([saved], planned=1)
    assert result["completed_queues"] == 1
    assert result["timed_completed_queues"] == 0
    assert result["fees_lamports"] == 5000
    assert result["resume_rpc_calls_observed"] == 2


def test_resume_stops_before_new_case_when_prior_signature_remains_unknown(tmp_path, monkeypatch):
    directory = tmp_path / "campaign"
    results = directory / "private/cases"
    results.mkdir(parents=True)
    config = compare.CampaignConfig(repeats=1)
    schedule = compare.schedule(config)[:2]
    manifest = {
        "config": config.model_dump(),
        "bank": {"instance_id": "bank"},
        "schedule": schedule,
    }
    prior = {
        **schedule[0],
        "queue": "inflight-queue",
        "status": "started",
        "queue_completion_ms": None,
        "steps": [],
        "verification": None,
    }
    (results / "000000.json").write_text(json.dumps(prior))
    monkeypatch.setattr(compare, "verify_frozen", lambda _directory: manifest)
    reports = []
    monkeypatch.setattr(compare, "write_report", lambda _directory: reports.append(True) or {})
    monkeypatch.setattr(compare, "durable_planning_attempts", lambda *_args: [])
    monkeypatch.setattr(
        compare,
        "durable_steps",
        lambda *_args: [
            {"decision": {"compute_unit_limit": 100_000}, "durable_phase": "uncertain"}
        ],
    )
    opened = []

    class Arm:
        def __init__(self, *_args, **_kwargs):
            self.info = {"instance_id": "bank"}
            self.rpc = SimpleNamespace(call_count=0, retry_count=0, method_counts=Counter())
            self.bridge = SimpleNamespace(
                rpc_calls=0, transport_failures=0, method_counts=Counter()
            )
            self.closed = False
            opened.append(self)

        def reconcile_pending(self, _queue):
            raise RuntimeError("unknown signature; requires reconciliation")

        def close(self):
            self.closed = True

    executed = []
    monkeypatch.setattr(compare, "Application", Arm)
    monkeypatch.setattr(compare, "execute_case", lambda *_args: executed.append(True))
    with pytest.raises(RuntimeError, match="campaign paused"):
        compare.run(directory)
    assert executed == []
    assert len(opened) == 4 and all(arm.closed for arm in opened)
    assert reports
    saved = json.loads((results / "000000.json").read_text())
    assert saved["status"] == "unresolved"
    assert saved["queue_completion_ms"] is None
    assert not (results / "000001.json").exists()
