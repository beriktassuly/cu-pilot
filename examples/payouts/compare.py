"""Frozen four-arm comparison on a disposable local Solana bank.

Use prepare/run/report/stop, or all. Share report.json/report.md/metrics.csv.
The private/ directory contains local authorization tokens and raw evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt

from cu_pilot.schemas import StrictModel
from examples.payouts.app import Application, Bridge, confirmed_budget_failure
from examples.payouts.benchmark import distribution, save_checkpoint, summarize_controls
from examples.payouts.model import PayoutObservation, canonical_ata, fit_bundle
from examples.payouts.planning import PlanningPolicy, fit_baselines

ROOT = Path(__file__).resolve().parents[2]
METHODS = ("adaptive", "adaptive_derivation", "always_simulate", "scoped_fixed_batch")
SCENARIOS = ("all_accounts_exist", "no_accounts_exist", "half_accounts_exist")
SCHEMA = "payout-hybrid-comparison-v1"


class CampaignConfig(StrictModel):
    schema_version: Literal["payout-hybrid-comparison-v1"] = SCHEMA
    groups: StrictInt = Field(default=80, ge=80, le=512)
    repeats: StrictInt = Field(default=10, ge=1, le=1000)
    payments: StrictInt = Field(default=16, ge=2, le=16)
    compute_unit_cap: StrictInt = Field(default=1_400_000, ge=1, le=1_400_000)
    seed: StrictInt = Field(default=20260927, ge=0, le=2**32 - 1)
    alpha: float = Field(default=0.05, gt=0, lt=1, allow_inf_nan=False)
    stress_blocks: StrictInt = Field(default=1, ge=0, le=20)
    stress_attempts: StrictInt = Field(default=8, ge=5, le=12)
    priority_price_micro_lamports: Literal[0] = 0


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_fingerprint() -> dict:
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    suffixes = {".py", ".ts", ".mjs", ".json", ".toml", ".lock", ".rs", ".sh"}
    files = {
        name: sha_file(ROOT / name)
        for name in sorted(set(names))
        if name and Path(name).suffix in suffixes and (ROOT / name).is_file()
    }
    elf = ROOT / "programs/payout_queue/target/deploy/payout_queue.so"
    files[str(elf.relative_to(ROOT))] = sha_file(elf)
    # Bind executed JS in addition to its TypeScript source.
    for path in sorted((ROOT / "typescript/dist/src").rglob("*.js")):
        files[str(path.relative_to(ROOT))] = sha_file(path)
    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"sha256": digest, "files": files}


def schedule(config: CampaignConfig) -> list[dict]:
    rng = random.Random(config.seed)
    rows = []
    for phase, repetitions, scenarios in (
        ("warmup", 1, SCENARIOS),
        ("measurement", config.repeats, SCENARIOS),
        ("stress", config.stress_blocks, ("derivation_tail",)),
    ):
        for repetition in range(repetitions):
            ordering = list(scenarios)
            rng.shuffle(ordering)
            for scenario in ordering:
                methods = list(METHODS)
                rng.shuffle(methods)
                block = f"{phase}:{repetition}:{scenario}"
                for method in methods:
                    rows.append(
                        {
                            "case_id": f"{block}:{method}",
                            "block_id": block,
                            "phase": phase,
                            "scenario": scenario,
                            "method": method,
                            "payments": config.payments,
                            "existing": config.payments
                            if scenario == SCENARIOS[0]
                            else config.payments // 2
                            if scenario == SCENARIOS[2]
                            else 0,
                        }
                    )
    return rows


def private_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o600)


def wait_runtime(config: Path, process: subprocess.Popen) -> None:
    end = time.monotonic() + 60
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError("runtime exited; inspect private/runtime.log")
        if config.exists() and json.loads(config.read_text()).get("pid") == process.pid:
            return
        time.sleep(0.1)
    raise RuntimeError("runtime readiness timed out")


def resume_plan(directory: Path, config: CampaignConfig) -> dict:
    plan = json.loads((directory / "plan.json").read_text())
    if plan["schema_version"] != SCHEMA or plan["config"] != config.model_dump(mode="json"):
        raise ValueError("preparation configuration changed")
    if source_fingerprint() != plan["source"]:
        raise ValueError("preparation source changed; start a new campaign")
    if plan["schedule"] != schedule(config):
        raise ValueError("preparation schedule changed")
    return plan


def prepare(directory: Path, config: CampaignConfig, *, resume: bool = False) -> dict:
    from examples.payouts.hybrid import fit_hybrid

    private = directory / "private"
    process = None
    if resume:
        plan = resume_plan(directory, config)
        if (directory / "manifest.json").exists():
            manifest = verify_frozen(directory)
            bridge = Bridge(private / "runtime.json")
            try:
                if bridge.call("info")["instance_id"] != manifest["bank"]["instance_id"]:
                    raise ValueError("preparation bank restarted")
            finally:
                bridge.client.close()
            return manifest
    else:
        directory.mkdir(parents=True, exist_ok=False)
        private.mkdir(mode=0o700)
        if shutil.which("node") is None:
            raise RuntimeError("pinned Node is required; run the payout bootstrap")
        plan = {
            "schema_version": SCHEMA,
            "created_at": datetime.now(UTC).isoformat(),
            "config": config.model_dump(mode="json"),
            "source": source_fingerprint(),
            "schedule": schedule(config),
            "sampling": (
                "fresh distinct recipients per arm; matched scenarios, not identical inputs"
            ),
            "partitions": "collection is fit/calibration/qualification; final queues are fresh",
            "cache_policy": "clear pure address cache before every case; reuse within each queue",
            "comparison_scope": (
                "one offline bank, legacy transactions, zero CU price, no public RPC"
            ),
            "fixture_funding": {
                "owner_lamports": 1_000_000_000_000,
                "executor_lamports": 100_000_000_000,
                "per_case_executor_reset_lamports": 100_000_000,
            },
            "host": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "node": subprocess.check_output(["node", "--version"], text=True).strip(),
            },
        }
        save_checkpoint(directory / "plan.json", plan)
    frozen_source = plan["source"]
    runtime_config = private / "runtime.json"
    if not resume:
        with (private / "runtime.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    "node",
                    str(ROOT / "examples/payouts/comparison_runtime.mjs"),
                    str(runtime_config),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    attempts_path = directory / "preparation.json"
    preparation_state = json.loads(attempts_path.read_text()) if attempts_path.exists() else {}
    attempts = preparation_state.get("attempts", [])
    bank_instance_id = preparation_state.get("bank_instance_id")
    attempt_started = time.perf_counter()
    application = None
    bank_ready = resume
    try:
        if process is not None:
            wait_runtime(runtime_config, process)
            bank_ready = True
        expected_bank = json.loads(runtime_config.read_text())["instance_id"]
        if resume and bank_instance_id != expected_bank:
            raise ValueError("preparation bank identity changed or was not frozen")
        bank_instance_id = expected_bank
        save_checkpoint(
            attempts_path,
            {"status": "preparing", "bank_instance_id": bank_instance_id, "attempts": attempts},
        )
        data = private / "collection"
        private_copy(runtime_config, data / "runtime.json")
        application = Application(data, compute_unit_cap=config.compute_unit_cap)
        bank = application.info
        if bank["instance_id"] != expected_bank:
            raise ValueError("preparation runtime identity changed")
        bank_ready = True
        started = time.perf_counter()
        collection = application.collect(config.groups)
        collection_ms = (time.perf_counter() - started) * 1000
        application.close()
        application = None
        rows = [
            PayoutObservation.model_validate_json(line)
            for line in (data / "observations.jsonl").read_text().splitlines()
            if line
        ]
        old = fit_bundle(rows)
        old.save(data / "candidate.json")
        fit_baselines(old, rows, policy=PlanningPolicy(compute_cap=config.compute_unit_cap)).save(
            data / "baselines.json"
        )
        hybrid = fit_hybrid(rows, alpha=config.alpha)
        hybrid.save(data / "hybrid-candidate.json")
        # Same split and calibration recipe; this ablation never selects the live method.
        fit_hybrid(rows, alpha=config.alpha, method="count_missing").save(
            data / "count-only-candidate.json"
        )
        qualifications = {}
        for method in METHODS:
            arm = private / "arms" / method
            private_copy(runtime_config, arm / "runtime.json")
            for name in ("candidate.json", "baselines.json", "hybrid-candidate.json"):
                private_copy(data / name, arm / name)
            app = Application(arm, compute_unit_cap=config.compute_unit_cap)
            try:
                qualifications[method] = (
                    app.qualify_hybrid() if method == "adaptive_derivation" else app.qualify()
                )
            finally:
                app.close()
        if source_fingerprint() != frozen_source:
            raise RuntimeError("source changed during preparation; start a new campaign")
        manifest = {
            **plan,
            "plan_sha256": sha_file(directory / "plan.json"),
            "bank": {
                k: bank[k]
                for k in (
                    "instance_id",
                    "runtime",
                    "elf_digest",
                    "features",
                    "local_dependency_installations",
                )
            },
            "artifacts": {
                name: sha_file(data / name)
                for name in (
                    "candidate.json",
                    "hybrid-candidate.json",
                    "baselines.json",
                    "count-only-candidate.json",
                    "observations.jsonl",
                )
            },
            "qualification": qualifications,
            "preparation_attempts": attempts
            + [
                {
                    "status": "complete",
                    "elapsed_ms": (time.perf_counter() - attempt_started) * 1000,
                }
            ],
            "collection": {
                **collection,
                "path": "private/collection/observations.jsonl",
                "collection_ms": collection_ms,
            },
            "model_selection": "five-term method and alpha frozen before collection",
            "statistical_warning": "local development gates; not a production risk certificate",
        }
        save_checkpoint(directory / "manifest.json", manifest)
        save_checkpoint(
            private / "frozen.json", {"manifest_sha256": sha_file(directory / "manifest.json")}
        )
        save_checkpoint(
            directory / "preparation.json",
            {
                "status": "complete",
                "bank_instance_id": bank_instance_id,
                "attempts": manifest["preparation_attempts"],
            },
        )
        return manifest
    except BaseException as exc:
        if application is not None:
            application.close()
        attempts.append(
            {
                "status": "interrupted"
                if isinstance(exc, (KeyboardInterrupt, SystemExit))
                else "error",
                "error_type": type(exc).__name__,
                "elapsed_ms": (time.perf_counter() - attempt_started) * 1000,
            }
        )
        save_checkpoint(
            attempts_path,
            {"status": "incomplete", "bank_instance_id": bank_instance_id, "attempts": attempts},
        )
        if bank_ready:
            # Preserve ephemeral state and durable collection for a bounded failure.
            # Resumption still requires the exact source/configuration and same bank.
            print(
                "Preparation stopped; the local bank and observations were preserved. "
                "Use prepare --resume-preparation or stop with this directory.",
                flush=True,
            )
        elif process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise


def verify_frozen(directory: Path) -> dict:
    frozen = json.loads((directory / "private/frozen.json").read_text())
    if sha_file(directory / "manifest.json") != frozen["manifest_sha256"]:
        raise ValueError("campaign manifest changed")
    manifest = json.loads((directory / "manifest.json").read_text())
    if sha_file(directory / "plan.json") != manifest["plan_sha256"]:
        raise ValueError("pre-collection plan changed")
    if source_fingerprint() != manifest["source"]:
        raise ValueError("source or executed ELF/JS changed; start a new campaign")
    for name, digest in manifest["artifacts"].items():
        if sha_file(directory / "private/collection" / name) != digest:
            raise ValueError("frozen collection or model changed")
    for method in METHODS:
        for name in ("candidate.json", "hybrid-candidate.json", "baselines.json"):
            if sha_file(directory / "private/arms" / method / name) != manifest["artifacts"][name]:
                raise ValueError("arm artifact changed")
    return manifest


def counters(app: Application) -> dict:
    return {
        "rpc_calls": app.rpc.call_count + app.bridge.rpc_calls,
        "rpc_retries": app.rpc.retry_count,
        "transport_failures": app.bridge.transport_failures,
        "methods": dict(app.rpc.method_counts + app.bridge.method_counts),
    }


def counter_delta(before: dict, after: dict) -> dict:
    return {
        **{
            key: after[key] - before[key]
            for key in ("rpc_calls", "rpc_retries", "transport_failures")
        },
        "methods": dict(Counter(after["methods"]) - Counter(before["methods"])),
    }


def durable_steps(app: Application, queue: str) -> list[dict]:
    return [
        {
            **json.loads(row["body"]),
            **(json.loads(row["outcome"]) if row["outcome"] else {}),
            "durable_phase": row["phase"],
        }
        for row in app.store.db.execute(
            "SELECT body,outcome,phase FROM payout_steps WHERE queue=? ORDER BY rowid", (queue,)
        ).fetchall()
    ]


def durable_planning_attempts(app: Application, queue: str) -> list[dict]:
    return [
        {"id": row["id"], "audit": json.loads(row["body"]), "status": row["status"]}
        for row in app.store.db.execute(
            "SELECT id,body,status FROM payout_planning_attempts WHERE queue=? ORDER BY rowid",
            (queue,),
        ).fetchall()
    ]


def tail_seed(base: str, mint: str, payments: int, threshold: int) -> tuple[str, dict]:
    from solders.keypair import Keypair

    from examples.payouts.app import ATA, TOKEN

    for trial in range(5000):
        seed = f"{base}:tail:{trial}"
        bumps = [
            canonical_ata(
                str(
                    Keypair.from_seed(hashlib.sha256(f"{seed}:{index}".encode()).digest()).pubkey()
                ),
                mint,
                TOKEN,
                ATA,
            )[1]
            for index in range(payments)
        ]
        attempts = [256 - bump for bump in bumps]
        if max(attempts) >= threshold:
            return seed, {
                "search_trials": trial + 1,
                "attempts": attempts,
                "threshold": threshold,
                "kind": "deliberate seed-selected stress",
            }
    raise RuntimeError("bounded tail-fixture search found no qualifying public recipients")


def is_complete(case: dict) -> bool:
    verification = case.get("verification") or {}
    queue = verification.get("queue") or {}
    return bool(
        verification.get("correct")
        and verification.get("duplicate_count") == 0
        and queue.get("paid_count") == case["payments"]
        and queue.get("cursor") == case["payments"]
        and queue.get("status") == 1
    )


def execute_case(app: Application, case: dict, manifest: dict, checkpoint: Path) -> dict:
    config = CampaignConfig.model_validate(manifest["config"])
    trace = {**case, "status": "started", "queue": None, "steps": [], "verification": None}
    save_checkpoint(checkpoint, trace)
    before = counters(app)
    controls_before = {
        record["selection"]["request_id"] for record in app.registry.control_records()
    }
    started = time.perf_counter()
    run_before = None
    run_started = None
    try:
        seed = f"{manifest['bank']['instance_id']}:{config.seed}:{case['case_id']}"
        if case["phase"] == "stress":
            seed, trace["stress_fixture"] = tail_seed(
                seed, app.info["mint"], config.payments, config.stress_attempts
            )
        identifier = hashlib.sha256(seed.encode()).hexdigest()
        trace["identifier"] = identifier
        trace["recipient_seed"] = seed
        save_checkpoint(checkpoint, trace)
        app.bridge.call("reset_allowance")
        created = app.create(
            length=case["payments"],
            existing=case["existing"],
            identifier=identifier,
            recipient_seed=seed,
        )
        trace["queue"] = created["queue"]["address"]
        trace["setup_ms"] = (time.perf_counter() - started) * 1000
        trace["setup_counters"] = counter_delta(before, counters(app))
        save_checkpoint(checkpoint, trace)
        canonical_ata.cache_clear()
        trace["cache_before"] = canonical_ata.cache_info()._asdict()
        run_before = counters(app)
        run_started = time.perf_counter()
        result = app.run(trace["queue"], case["method"])
        trace["verification"] = result.get("verification")
        trace["status"] = "complete" if is_complete(trace) else "incomplete"
    except (KeyboardInterrupt, SystemExit) as exc:
        trace["status"] = "interrupted"
        trace["error"] = {"type": type(exc).__name__, "message": "execution interrupted"}
        raise
    except Exception as exc:
        trace["status"] = "error"
        trace["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        run_after = counters(app)
        if run_started is None:
            trace["setup_ms"] = (time.perf_counter() - started) * 1000
            trace["setup_counters"] = counter_delta(before, run_after)
            trace["run_counters"] = {
                "rpc_calls": 0,
                "rpc_retries": 0,
                "transport_failures": 0,
                "methods": {},
            }
        if run_started is not None:
            trace["attempt_wall_ms"] = (time.perf_counter() - run_started) * 1000
            trace["run_counters"] = counter_delta(run_before, run_after)
        trace["cache_after"] = canonical_ata.cache_info()._asdict()
        if trace["queue"] is not None:
            trace["steps"] = durable_steps(app, trace["queue"])
            trace["planning_attempts"] = durable_planning_attempts(app, trace["queue"])
            try:
                trace["verification"] = app.bridge.call("verify", queue=trace["queue"])
            except Exception as exc:
                trace["verification_error"] = type(exc).__name__
        trace["controls"] = [
            record
            for record in app.registry.control_records()
            if record["selection"]["request_id"] not in controls_before
        ]
        trace["validation_counters"] = counter_delta(run_after, counters(app))
        trace["all_counters"] = counter_delta(before, counters(app))
        trace["case_wall_ms"] = (time.perf_counter() - started) * 1000
        trace["queue_completion_ms"] = (
            trace.get("attempt_wall_ms")
            if trace["status"] == "complete" and is_complete(trace)
            else None
        )
        save_checkpoint(checkpoint, trace)
    return trace


def reconcile_case(app: Application, prior: dict, checkpoint: Path) -> None:
    """Resolve recorded signatures before resetting any shared executor allowance.

    Resume never repeats a measured queue or assigns interruption downtime a fresh timer.
    """
    queue = prior.get("queue")
    interrupted = prior["status"] in {"started", "interrupted", "unresolved"}
    if not queue:
        if interrupted:
            prior["status"] = "interrupted"
            prior["queue_completion_ms"] = None
            save_checkpoint(checkpoint, prior)
        return
    steps = durable_steps(app, queue)

    def pending():
        return any(row.get("durable_phase") in {"signed", "uncertain"} for row in steps)

    if not interrupted and not pending():
        return
    prior["queue_completion_ms"] = None
    prior["status"] = "interrupted"
    before = counters(app)
    started = time.perf_counter()
    error = None
    try:
        for _ in range(3):
            if not pending():
                break
            app.reconcile_pending(queue)
            steps = durable_steps(app, queue)
        if pending():
            raise RuntimeError("recorded transaction remains unresolved")
        prior["verification"] = app.bridge.call("verify", queue=queue)
    except Exception as exc:
        error = exc
        prior["status"] = "unresolved"
        prior["reconciliation_error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        prior["steps"] = durable_steps(app, queue)
        prior["planning_attempts"] = durable_planning_attempts(app, queue)
        prior.setdefault("resume_reconciliation", []).append(
            {
                "wall_ms": (time.perf_counter() - started) * 1000,
                "counters": counter_delta(before, counters(app)),
            }
        )
        save_checkpoint(checkpoint, prior)
    if error is not None:
        raise RuntimeError(
            "campaign paused: prior signature or queue cannot be reconciled"
        ) from error


def run(directory: Path, *, maximum_cases: int | None = None) -> dict:
    manifest = verify_frozen(directory)
    config = CampaignConfig.model_validate(manifest["config"])
    results = directory / "private/cases"
    results.mkdir(exist_ok=True)
    arms = {}
    try:
        for method in METHODS:
            arms[method] = Application(
                directory / "private/arms" / method, compute_unit_cap=config.compute_unit_cap
            )
            if arms[method].info["instance_id"] != manifest["bank"]["instance_id"]:
                raise ValueError("bank restarted; frozen artifacts cannot be reused")
        # Reconcile every recorded case before creating or funding any later case.
        for index, case in enumerate(manifest["schedule"]):
            path = results / f"{index:06d}.json"
            if path.exists():
                prior = json.loads(path.read_text())
                if any(prior.get(key) != value for key, value in case.items()):
                    raise ValueError("case schedule changed")
                reconcile_case(arms[case["method"]], prior, path)
        executed = 0
        for index, case in enumerate(manifest["schedule"]):
            path = results / f"{index:06d}.json"
            if path.exists():
                continue
            verify_frozen(directory)
            result = execute_case(arms[case["method"]], case, manifest, path)
            executed += 1
            print(
                f"{index + 1}/{len(manifest['schedule'])}: "
                f"{case['method']} {case['scenario']} {result['status']}",
                flush=True,
            )
            write_report(directory)
            # Even a returned error cannot leave a pending signed transaction behind.
            reconcile_case(arms[case["method"]], result, path)
            if maximum_cases is not None and executed >= maximum_cases:
                break
    finally:
        for app in arms.values():
            app.close()
        write_report(directory)
    return write_report(directory)


def metrics(cases: list[dict], *, planned: int) -> dict:
    plans = [step for case in cases for step in case.get("steps", []) if "decision" in step]
    steps = [
        step
        for step in plans
        if step.get("durable_phase") in {"signed", "uncertain", "confirmed", "failed"}
        or type(step.get("success")) is bool
    ]
    planned_ids = {step.get("id") for step in plans}
    orphan_planning = [
        record["audit"]
        for case in cases
        for record in case.get("planning_attempts", [])
        if record["id"] not in planned_ids
    ]
    successes = [step for step in steps if step.get("success") is True]
    confirmed = [step for step in steps if type(step.get("success")) is bool]
    cu = [step for step in successes if type(step.get("compute_units")) is int]
    data = [step for step in successes if type(step.get("loaded_accounts_bytes")) is int]
    completed = [case for case in cases if is_complete(case)]
    measured = [case for case in completed if case.get("queue_completion_ms") is not None]
    total_methods = Counter()
    for case in cases:
        total_methods.update(case.get("all_counters", {}).get("methods", {}))
    exhaustions = [
        step
        for step in confirmed
        if step["success"] is False
        and step.get("transaction")
        and confirmed_budget_failure(step["transaction"], step["decision"]["compute_unit_limit"])
    ]

    def total(field: str) -> int | None:
        values = [step.get(field) for step in steps]
        return sum(values) if all(type(v) is int for v in values) else None

    def observed_sum(field: str) -> int:
        return sum(step[field] for step in steps if type(step.get(field)) is int)

    def simulation_summary(field: str) -> dict:
        values = [step.get(field) for step in plans] + [r.get(field) for r in orphan_planning]
        missing = sum(type(value) is not int for value in values)
        # An interrupted/error case can stop after RPC but before the durable planning journal.
        unknown_cases = sum(
            case["status"] in {"started", "interrupted", "unresolved", "error"}
            and not case.get("steps")
            and not case.get("planning_attempts")
            for case in cases
        )
        return {
            "total": sum(values) if not missing and not unknown_cases else None,
            "observed_sum": sum(v for v in values if type(v) is int),
            "missing_records": missing,
            "untracked_failed_cases": unknown_cases,
        }

    def rpc_summary(field: str) -> dict:
        values = [case.get(field, {}).get("rpc_calls") for case in cases]
        known = [value for value in values if type(value) is int]
        return {
            "total": sum(known) if len(known) == len(values) else None,
            "observed_sum": sum(known),
            "missing_cases": len(values) - len(known),
        }

    all_rpc, run_rpc = rpc_summary("all_counters"), rpc_summary("run_counters")
    estimation = simulation_summary("estimation_simulations")
    controls = simulation_summary("control_simulations")
    excess = [max(0, step["decision"]["compute_unit_limit"] - step["compute_units"]) for step in cu]
    observed_paid = sum(
        (case.get("verification") or {}).get("queue", {}).get("paid_count", 0) for case in cases
    )
    return {
        "planned_queues": planned,
        "attempted_queues": len(cases),
        "not_attempted_queues": planned - len(cases),
        "completed_queues": len(completed),
        "incomplete_or_unknown_queues": len(cases) - len(completed),
        "completion_rate": len(completed) / len(cases) if cases else None,
        "timed_completed_queues": len(measured),
        "completion_ms": distribution([case["queue_completion_ms"] for case in measured]),
        "attempt_wall_ms": distribution([case.get("attempt_wall_ms") for case in cases]),
        "setup_ms": distribution([case.get("setup_ms") for case in cases]),
        "preparation_ms": distribution([step.get("preparation_ms") for step in steps]),
        "completed_payments_observed": observed_paid,
        "pending_or_unverified_payments": sum(case["payments"] for case in cases) - observed_paid,
        "duplicates_observed": sum(
            (case.get("verification") or {}).get("duplicate_count", 0) for case in cases
        ),
        "missing_queue_verifications": sum(not case.get("verification") for case in cases),
        "planned_decisions": len(plans),
        "unsigned_plans": len(plans) - len(steps),
        "orphan_planning_attempts": len(orphan_planning),
        "successful_transactions": len(successes),
        "failed_transactions": sum(step.get("success") is False for step in confirmed),
        "unknown_transactions": len(steps) - len(confirmed),
        "confirmed_budget_exhaustions": len(exhaustions),
        "accepted_predictions": sum(step.get("mode") == "prediction" for step in steps),
        "simulation_decisions": sum(step.get("mode") == "fallback" for step in steps),
        "fallback_decisions": sum(
            step.get("mode") == "fallback"
            and not (step.get("control_simulations", 0) and step.get("estimation_simulations") == 0)
            for step in steps
        ),
        "fallback_rate": sum(
            step.get("mode") == "fallback"
            and not (step.get("control_simulations", 0) and step.get("estimation_simulations") == 0)
            for step in steps
        )
        / len(steps)
        if steps
        else None,
        "control_evaluation": summarize_controls(
            [record for case in cases for record in case.get("controls", [])]
        ),
        "accepted_coverage": sum(step.get("mode") == "prediction" for step in steps) / len(steps)
        if steps
        else None,
        "estimation_simulations": estimation["total"],
        "control_simulations": controls["total"],
        "simulation_measurement_coverage": {"estimation": estimation, "controls": controls},
        "rpc_calls_including_setup_and_validation": all_rpc["total"],
        "rpc_calls_observed": all_rpc["observed_sum"],
        "run_rpc_calls": run_rpc["total"],
        "run_rpc_calls_observed": run_rpc["observed_sum"],
        "run_rpc_missing_cases": run_rpc["missing_cases"],
        "rpc_counters_missing_cases": all_rpc["missing_cases"],
        "resume_rpc_calls_observed": sum(
            recovery["counters"]["rpc_calls"]
            for case in cases
            for recovery in case.get("resume_reconciliation", [])
        ),
        "rpc_methods": dict(total_methods),
        "compute_labels": len(cu),
        "missing_compute_labels": len(successes) - len(cu),
        "data_labels": len(data),
        "missing_data_labels": len(successes) - len(data),
        "loaded_data_excess": distribution(
            [
                max(
                    0,
                    step["decision"]["loaded_accounts_data_size_limit"]
                    - step["loaded_accounts_bytes"],
                )
                for step in data
            ]
        ),
        "requested_cu_all_attempts": sum(step["decision"]["compute_unit_limit"] for step in steps),
        "consumed_cu_observed_successes": sum(step["compute_units"] for step in cu),
        "compute_excess": distribution(excess),
        "uncensored_compute_underestimations": sum(
            step["compute_units"] > step["decision"]["compute_unit_limit"] for step in cu
        ),
        "fees_lamports": total("fee_lamports"),
        "fees_lamports_observed": observed_sum("fee_lamports"),
        "rent_lamports": total("rent_deposit_lamports"),
        "rent_lamports_observed": observed_sum("rent_deposit_lamports"),
        "errors": dict(Counter(case["status"] for case in cases if case["status"] != "complete")),
    }


def paired_differences(cases: list[dict], seed: int) -> dict:
    grouped = defaultdict(dict)
    for case in cases:
        if case["phase"] == "measurement" and is_complete(case):
            if case.get("queue_completion_ms") is not None:
                grouped[case["block_id"]][case["method"]] = case["queue_completion_ms"]
    result = {}
    rng = random.Random(seed)
    for comparator in ("adaptive", "always_simulate", "scoped_fixed_batch"):
        differences = [
            values["adaptive_derivation"] - values[comparator]
            for values in grouped.values()
            if "adaptive_derivation" in values and comparator in values
        ]
        interval = None
        if len(differences) >= 2:
            samples = sorted(
                statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(2000)
            )
            interval = [samples[49], samples[1949]]
        result[comparator] = {
            "complete_matched_blocks": len(differences),
            "hybrid_minus_comparator_ms": distribution(differences),
            "descriptive_paired_bootstrap_95pct_interval_ms": interval,
            "caveat": "completion-conditioned; distinct addresses, one shared bank; "
            "small-sample bootstrap is not a production performance guarantee",
        }
    return result


def write_report(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    cases = [
        json.loads(path.read_text())
        for path in sorted((directory / "private/cases").glob("*.json"))
    ]
    groups = {}
    for phase in ("warmup", "measurement", "stress"):
        for scenario in (*SCENARIOS, "derivation_tail"):
            for method in METHODS:
                planned = sum(
                    row["phase"] == phase
                    and row["scenario"] == scenario
                    and row["method"] == method
                    for row in manifest["schedule"]
                )
                if not planned:
                    continue
                chosen = [
                    row
                    for row in cases
                    if row["phase"] == phase
                    and row["scenario"] == scenario
                    and row["method"] == method
                ]
                groups[f"{phase}/{scenario}/{method}"] = metrics(chosen, planned=planned)
    report = {
        "schema_version": SCHEMA,
        "manifest_sha256": sha_file(directory / "manifest.json"),
        "source_sha256": manifest["source"]["sha256"],
        "scope": manifest["comparison_scope"],
        "groups": groups,
        "paired_completion_latency": paired_differences(cases, manifest["config"]["seed"]),
        "limitations": [
            "Warmup and deliberately selected tail stress are separate from measurement.",
            "Execution may omit loaded-account labels; missing labels are never zero.",
            "CU-exhausted transactions have censored demand; count failures separately.",
            "All RPCs include setup, validation and failed calls when counters are available.",
            "Training, qualification and fixture funding are outside queue-completion timing.",
            "At zero priority price tighter CU reservations alone do not save priority fees.",
            "Local bank timing cannot establish remote-RPC or mainnet performance.",
        ],
    }
    save_checkpoint(directory / "report.json", report)
    with (directory / "metrics.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "phase",
                "scenario",
                "method",
                "attempted",
                "planned",
                "completed",
                "mean_completion_ms",
                "p95_completion_ms",
                "run_rpc_calls",
                "accepted_predictions",
                "failed_transactions",
                "mean_excess_cu",
                "fees_lamports",
                "rent_lamports",
            ]
        )
        for key, value in groups.items():
            writer.writerow(
                [
                    *key.split("/"),
                    value["attempted_queues"],
                    value["planned_queues"],
                    value["completed_queues"],
                    value["completion_ms"]["mean"],
                    value["completion_ms"]["p95"],
                    value["run_rpc_calls"],
                    value["accepted_predictions"],
                    value["failed_transactions"],
                    value["compute_excess"]["mean"],
                    value["fees_lamports"],
                    value["rent_lamports"],
                ]
            )
    lines = [
        "# Frozen hybrid comparison",
        "",
        report["scope"],
        "",
        "See metrics.csv for all phases and report.json for failures, coverage, labels, "
        "paired intervals and incomplete cases.",
        "",
        "| Scenario / method | Complete / attempted / planned | Mean ms | p95 ms | "
        "Run RPCs | Predictions | Failed tx |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    def number(value):
        return "—" if value is None else f"{value:.2f}"

    for key, value in groups.items():
        phase, scenario, method = key.split("/")
        if phase != "measurement":
            continue
        lines.append(
            f"| {scenario} / {method} | {value['completed_queues']} / "
            f"{value['attempted_queues']} / {value['planned_queues']} | "
            f"{number(value['completion_ms']['mean'])} | "
            f"{number(value['completion_ms']['p95'])} | {value['run_rpc_calls']} | "
            f"{value['accepted_predictions']} | {value['failed_transactions']} |"
        )
    lines += ["", *["- " + warning for warning in report["limitations"]], ""]
    (directory / "report.md").write_text("\n".join(lines))
    return report


def stop(directory: Path) -> None:
    config_path = directory / "private/runtime.json"
    data = json.loads(config_path.read_text())
    bridge = Bridge(config_path)
    try:
        bridge.call("stop_comparison", instance_id=data["instance_id"])
    finally:
        bridge.client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "report", "stop", "all"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--groups", type=int, default=80)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--payments", type=int, default=16)
    parser.add_argument("--compute-unit-cap", type=int, default=1_400_000)
    parser.add_argument("--seed", type=int, default=20260927)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--stress-blocks", type=int, default=1)
    parser.add_argument("--stress-attempts", type=int, default=8)
    parser.add_argument("--maximum-cases", type=int)
    parser.add_argument(
        "--resume-preparation",
        action="store_true",
        help="Resume the frozen preparation in its still-running local bank",
    )
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.maximum_cases is not None and args.maximum_cases < 1:
        parser.error("--maximum-cases must be positive")
    if args.resume_preparation and args.command not in {"prepare", "all"}:
        parser.error("--resume-preparation is only valid with prepare or all")
    if args.command in {"prepare", "all"}:
        config = CampaignConfig(
            groups=args.groups,
            repeats=args.repeats,
            payments=args.payments,
            compute_unit_cap=args.compute_unit_cap,
            seed=args.seed,
            alpha=args.alpha,
            stress_blocks=args.stress_blocks,
            stress_attempts=args.stress_attempts,
        )
        if args.resume_preparation:
            configuration_flags = (
                "--groups",
                "--repeats",
                "--payments",
                "--compute-unit-cap",
                "--seed",
                "--alpha",
                "--stress-blocks",
                "--stress-attempts",
            )
            if any(
                value == flag or value.startswith(flag + "=")
                for value in sys.argv[1:]
                for flag in configuration_flags
            ):
                parser.error("resume uses frozen configuration; omit collection and model options")
            config = CampaignConfig.model_validate(
                json.loads((directory / "plan.json").read_text())["config"]
            )
        prepare(directory, config, resume=args.resume_preparation)
        print(f"Frozen campaign ready: {directory / 'manifest.json'}", flush=True)
    if args.command in {"run", "all"}:
        try:
            run(directory, maximum_cases=args.maximum_cases)
        finally:
            if args.command == "all":
                stop(directory)
    elif args.command == "report":
        write_report(directory)
    elif args.command == "stop":
        stop(directory)
    if args.command in {"run", "all", "report"}:
        print(f"Comparison report: {directory / 'report.md'}")


if __name__ == "__main__":
    main()
