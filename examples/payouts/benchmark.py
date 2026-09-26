"""Actual local queue completions, causal ablation and worker restart evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from pathlib import Path

from examples.payouts.app import Application, confirmed_budget_failure
from examples.payouts.model import PayoutObservation, evaluate_bundle
from examples.payouts.planning import PLANNING_POLICY, fit_baselines

METHODS = ("learned", "fixed_batch", "formula", "cache", "pattern_p99", "always_simulate")


def save_checkpoint(path: Path, report: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(report, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def distribution(values):
    values = sorted(value for value in values if value is not None)
    return {
        "measured_count": len(values),
        "mean": statistics.mean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p95": values[math.ceil(len(values) * 0.95) - 1] if values else None,
    }


def summarize_controls(records):
    """Score selected controls against frozen predictions, separate from execution.

    Candidate planning can select a control that is never simulated because a
    different batch wins. Such an absent outcome is neither success nor failure.
    """
    selected = [r for r in records if r["selection"]["selected"]]
    observed = [r for r in selected if r["outcome"] is not None]
    successful = [r for r in observed if r["outcome"]["success"]]
    cu = [r for r in successful if r["outcome"].get("compute_units") is not None]
    data = [r for r in successful if r["outcome"].get("loaded_accounts_bytes") is not None]
    paired = [r for r in cu if r["outcome"].get("loaded_accounts_bytes") is not None]

    def cu_exceeds(row):
        return row["outcome"]["compute_units"] > row["selection"]["compute_unit_limit"]

    def data_exceeds(row):
        return (
            row["outcome"]["loaded_accounts_bytes"]
            > (row["selection"]["loaded_accounts_data_size_limit"])
        )

    cu_fail = sum(cu_exceeds(row) for row in cu)
    data_fail = sum(data_exceeds(row) for row in data)
    joint = sum(cu_exceeds(row) or data_exceeds(row) for row in paired)
    return {
        "candidate_selections": len(records),
        "selected_candidates": len(selected),
        "observed_controls": len(observed),
        "selected_without_outcome": len(selected) - len(observed),
        "failed_controls": sum(not row["outcome"]["success"] for row in observed),
        "compute_labels": len(cu),
        "loaded_data_labels": len(data),
        "paired_labels": len(paired),
        "compute_underestimations": cu_fail,
        "loaded_data_underestimations": data_fail,
        "joint_exceedances": joint,
        "compute_underestimation_rate": cu_fail / len(cu) if cu else None,
        "loaded_data_underestimation_rate": data_fail / len(data) if data else None,
        "joint_exceedance_rate": joint / len(paired) if paired else None,
    }


def budget_failure_resource(step):
    """Classify confirmed censored failures, never infer total resource demand."""
    if step.get("success") is not False:
        return None
    transaction = step.get("transaction")
    if not transaction or "err" not in transaction.get("meta", {}):
        return None
    return confirmed_budget_failure(transaction, step["decision"]["compute_unit_limit"])


def active_step_time(run):
    """Sum measured attempt intervals; this is not full queue wall time."""
    attempts = [step for step in run["steps"] if "decision" in step]
    if not attempts or any(step.get("complete_step_ms") is None for step in attempts):
        return None
    return sum(step["complete_step_ms"] for step in attempts)


def summarize(runs):
    steps = [step for run in runs for step in run["steps"] if "decision" in step]
    confirmed = [step for step in steps if type(step.get("success")) is bool]
    compute_exhaustions = sum(budget_failure_resource(step) == "compute" for step in steps)
    data_exhaustions = sum(budget_failure_resource(step) == "loaded_data" for step in steps)
    application_retries = 0
    for run in runs:
        attempts = [step for step in run["steps"] if "decision" in step]
        for previous, current in zip(attempts, attempts[1:], strict=False):
            if (
                budget_failure_resource(previous)
                and previous.get("cursor") is not None
                and previous["cursor"] == current.get("cursor")
            ):
                application_retries += 1
    cu_labels = [s for s in steps if s.get("compute_units") is not None and s.get("success")]
    data_labels = [
        s for s in steps if s.get("loaded_accounts_bytes") is not None and s.get("success")
    ]
    paired = [s for s in cu_labels if s.get("loaded_accounts_bytes") is not None]
    cu_fail = sum(s["compute_units"] > s["decision"]["compute_unit_limit"] for s in cu_labels)
    data_fail = sum(
        s["loaded_accounts_bytes"] > s["decision"]["loaded_accounts_data_size_limit"]
        for s in data_labels
    )
    joint = sum(
        s["compute_units"] > s["decision"]["compute_unit_limit"]
        or s["loaded_accounts_bytes"] > s["decision"]["loaded_accounts_data_size_limit"]
        for s in paired
    )

    def measured_total(field):
        values = [s[field] for s in steps if s.get(field) is not None]
        return {
            "total": sum(values) if len(values) == len(steps) else None,
            "observed_sum": sum(values),
            "measured_decisions": len(values),
            "missing_decisions": len(steps) - len(values),
        }

    quantities = {
        field: measured_total(field)
        for field in (
            "estimation_simulations",
            "control_simulations",
            "rpc_calls",
            "rpc_retries",
            "state_deployment_reads",
            "fee_lamports",
            "rent_deposit_lamports",
        )
    }
    methods = {}
    for step in steps:
        for method, count in (step.get("rpc_method_counts") or {}).items():
            methods[method] = methods.get(method, 0) + count
    simulation_decisions = [s for s in steps if s["mode"] == "fallback"]
    control_only = [
        s
        for s in simulation_decisions
        if s.get("control_simulations", 0) and s.get("estimation_simulations") == 0
    ]
    fallback_count = len(simulation_decisions) - len(control_only)
    learned_options = [
        option
        for step in steps
        if step.get("method") == "learned"
        for option in step.get("options", [])
    ]
    eligible_options = sum(bool(option["eligible"]) for option in learned_options)
    return {
        "queues": len(runs),
        "transactions": len(steps),
        "confirmed_attempts": len(confirmed),
        "successful_transactions": sum(step.get("success") is True for step in steps),
        "recovered_decisions": sum(bool(s.get("recovered")) for s in steps),
        "completed_obligations": sum(r["verification"]["queue"]["paid_count"] for r in runs),
        "pending_obligations": sum(
            r["verification"]["queue"]["length"] - r["verification"]["queue"]["cursor"]
            for r in runs
        ),
        "duplicate_count": sum(r["verification"]["duplicate_count"] for r in runs),
        "all_payments_correct": all(r["verification"]["correct"] for r in runs),
        "failed_attempts": sum(s.get("success") is False for s in steps),
        "confirmed_compute_budget_exhaustions": compute_exhaustions,
        "confirmed_loaded_data_budget_exhaustions": data_exhaustions,
        "confirmed_budget_exhaustions": compute_exhaustions + data_exhaustions,
        "confirmed_budget_exhaustion_rate": (compute_exhaustions + data_exhaustions)
        / len(confirmed)
        if confirmed
        else None,
        "application_retries_after_confirmed_budget_failure": application_retries,
        "total_retries": quantities["rpc_retries"]["total"] + application_retries
        if quantities["rpc_retries"]["total"] is not None
        else None,
        "outcome_unknown_decisions": sum(s.get("success") is None for s in steps),
        "prediction_decisions": sum(s["mode"] == "prediction" for s in steps),
        "simulation_decisions": len(simulation_decisions),
        "control_only_decisions": len(control_only),
        "fallback_decisions": fallback_count,
        "fallback_rate": fallback_count / len(steps) if steps else None,
        "model_eligible_candidates": eligible_options,
        "model_evaluated_candidates": len(learned_options),
        "model_candidate_eligibility_rate": eligible_options / len(learned_options)
        if learned_options
        else None,
        **{
            field: quantities[field]["total"]
            for field in (
                "estimation_simulations",
                "control_simulations",
                "rpc_calls",
                "rpc_retries",
                "state_deployment_reads",
            )
        },
        "measurement_coverage": quantities,
        "rpc_method_counts_observed": methods,
        "execution_compute_labels": len(cu_labels),
        "execution_loaded_data_labels": len(data_labels),
        "execution_paired_labels": len(paired),
        "compute_underestimations": cu_fail,
        "data_underestimations": data_fail,
        "joint_exceedances": joint,
        "compute_underestimation_rate": cu_fail / len(cu_labels) if cu_labels else None,
        "loaded_data_underestimation_rate": data_fail / len(data_labels) if data_labels else None,
        "joint_exceedance_rate": joint / len(paired) if paired else None,
        "compute_over_allocation": distribution(
            [max(0, s["decision"]["compute_unit_limit"] - s["compute_units"]) for s in cu_labels]
        ),
        "loaded_data_over_allocation": distribution(
            [
                max(
                    0, s["decision"]["loaded_accounts_data_size_limit"] - s["loaded_accounts_bytes"]
                )
                for s in data_labels
            ]
        ),
        "preparation_ms": distribution([s.get("preparation_ms") for s in steps]),
        "local_transport_ms": distribution([s.get("local_transport_ms") for s in steps]),
        "step_ms": distribution([s.get("complete_step_ms") for s in steps]),
        "inference_and_candidate_ms": distribution(
            [s.get("inference_and_candidate_ms") for s in steps]
        ),
        "signing_ms": distribution([s.get("signing_ms") for s in steps]),
        "submission_confirmation_ms": distribution(
            [s.get("bridge_submission_confirmation_ms") for s in steps]
        ),
        "queue_completion_ms": distribution([r["queue_completion_ms"] for r in runs]),
        "active_step_time_ms": distribution([active_step_time(run) for run in runs]),
        "observed_fee_lamports": quantities["fee_lamports"]["total"],
        "rent_deposit_lamports": quantities["rent_deposit_lamports"]["total"],
    }


def run_benchmark(directory: Path, repeats: int = 1):
    if not 1 <= repeats <= 3:
        raise ValueError("benchmark limited to one through three repeats")
    app = Application(directory)
    if app.bundle is None:
        raise ValueError("train and qualify the local artifact first")
    rows = [
        PayoutObservation.model_validate_json(line)
        for line in (directory / "observations.jsonl").read_text().splitlines()
    ]
    fitted = fit_baselines(app.bundle, rows)
    (directory / "baselines.json").write_text(fitted.model_dump_json(indent=2) + "\n")
    app.baselines = fitted.model_dump(mode="json")
    initial_controls = {r["selection"]["request_id"] for r in app.registry.control_records()}
    initial_audit_sequence = max(
        (event["sequence"] for event in app.registry.audit_events()), default=0
    )
    report = {
        "schema_version": "payout-execution-evidence-v1",
        "runtime": app.info,
        "policy": PLANNING_POLICY.model_dump(mode="json"),
        "bundle_digest": app.bundle.digest,
        "resource_holdout": evaluate_bundle(app.bundle, rows),
        "baseline_fit": fitted.model_dump(mode="json"),
        "runs": [],
        "ablation": {},
        "ablation_attempts": [],
        "limitations": [
            "Local emulator measurements; no production risk or commercial advantage established.",
            "Confirmation is confirmed with identical submission semantics for every method.",
            "Queue creation/funding and allowance resets are setup, outside completion timing.",
            "Rent is an account deposit, reported separately from fees; the sender does not "
            "own recipient ATA rent and this program does not reclaim queue/vault rent.",
            "Missing historical loaded-data labels remain missing; simulation holdout is separate.",
            "Ablation is a fixed-estimate intervention, separate from the tuned fixed batch.",
            "Unmeasured recovery timings and RPC totals remain null with measurement counts.",
            "Fallback excludes control-only simulation decisions; control underestimation is "
            "scored separately against its frozen original prediction, before simulated limits.",
            "Selected candidate controls without outcomes were not necessarily chosen for "
            "execution and are not counted as observed successes or failures.",
            "Confirmed resource-budget failures are censored outcomes: fees/attempts/retries "
            "are retained, but consumed units do not become successful demand labels.",
            "Active-step time sums measured attempt intervals for every method, including "
            "failures. It excludes administrative pauses and gaps after the outcome timer; "
            "it is not full queue-completion wall time.",
        ],
    }
    report_path = directory / "execution-report.json"
    if report_path.exists():
        prior = json.loads(report_path.read_text())
        if (
            prior["runtime"]["instance_id"] != app.info["instance_id"]
            or prior["bundle_digest"] != app.bundle.digest
            or prior["policy"] != report["policy"]
            or prior["baseline_fit"] != report["baseline_fit"]
            or prior.get("repeats", 1) != repeats
        ):
            app.close()
            raise ValueError("benchmark checkpoint belongs to different inputs; use isolated state")
        report = prior
        if report.get("status") == "all_local_execution_gates_passed":
            app.close()
            return report
        initial_controls -= {
            row["selection"]["request_id"] for row in report.get("control_records", [])
        }
        initial_audit_sequence = min(
            (row["sequence"] - 1 for row in report.get("lifecycle_events", [])),
            default=initial_audit_sequence,
        )
        if report.get("failure"):
            report.setdefault("interruption_history", []).append(report.pop("failure"))
    report["repeats"] = repeats
    report["status"] = "in_progress"

    def checkpoint():
        save_checkpoint(report_path, report)

    try:
        # Fresh unrelated recipient cohorts; all candidate features withheld from fitting.
        for method in ("learned", "fixed_estimate_ablation"):
            if method in report["ablation"]:
                continue
            for attempt in range(5 if method == "learned" else 1):
                app.bridge.call("reset_allowance")
                queue = app.create(length=8, existing=8)["queue"]["address"]
                step = app.step(queue, method)
                report["ablation_attempts"].append(
                    {
                        "method": method,
                        "attempt": attempt + 1,
                        "step": step,
                    }
                )
                # Controls intentionally replace model limits with a simulation.
                # Retain their complete evidence, and bound retries solely for
                # this preselected reason. No thresholds or model change here.
                if not (
                    method == "learned"
                    and step["decision"].get("control_selected")
                    and step["decision"]["status"] == "simulation_success"
                ):
                    break
            report["ablation"][method] = step
            checkpoint()
            assert step["verification"]["correct"]
            assert step["verification"]["queue"]["cursor"] == step["chosen_count"]
            assert step["verification"]["queue"]["paid_count"] == step["chosen_count"]
        learned = report["ablation"]["learned"]
        fixed = report["ablation"]["fixed_estimate_ablation"]
        report["ablation"]["causal_requirement_passed"] = (
            learned["decision"]["status"] == "accepted_prediction"
            and learned["chosen_count"] != fixed["chosen_count"]
            and learned["verification"]["correct"]
            and fixed["verification"]["correct"]
        )
        if not report["ablation"]["causal_requirement_passed"]:
            raise AssertionError(
                "causal gate incomplete: qualified model did not change executed count"
            )
        for index in range(repeats):
            for scenario, existing in (
                ("existing_atas", 16),
                ("missing_atas", 0),
                ("mixed_atas", 8),
            ):
                for method in METHODS:
                    identity = {"scenario": scenario, "method": method, "repetition": index}
                    if any(
                        all(run[key] == value for key, value in identity.items())
                        for run in report["runs"]
                    ):
                        continue
                    pending = report.get("pending_run")
                    if pending is not None:
                        if not all(pending[key] == value for key, value in identity.items()):
                            raise ValueError("benchmark checkpoint order changed")
                        queue, setup_ms = pending["queue"], pending.get("setup_ms")
                        earlier_steps = [
                            {**json.loads(row["body"]), **json.loads(row["outcome"])}
                            for row in app.store.db.execute(
                                "SELECT body,outcome FROM payout_steps WHERE queue=? "
                                "AND outcome IS NOT NULL ORDER BY rowid",
                                (queue,),
                            )
                        ]
                    else:
                        app.bridge.call("reset_allowance")
                        app.set_setting("estimate_cache", {})
                        setup_started = time.perf_counter()
                        queue = app.create(length=16, existing=existing)["queue"]["address"]
                        setup_ms = (time.perf_counter() - setup_started) * 1000
                        report["pending_run"] = {**identity, "queue": queue, "setup_ms": setup_ms}
                        earlier_steps = []
                        checkpoint()
                    result = app.run(queue, method)
                    if pending is not None:
                        result["steps"] = earlier_steps + result["steps"]
                        result["measured_resume_interval_ms"] = result["queue_completion_ms"]
                        result["queue_completion_ms"] = None
                        result["timing_note"] = (
                            "Interrupted queue: full wall time includes administrative downtime "
                            "and is excluded from uninterrupted completion comparisons. "
                            "Original per-step fees, RPC calls and active timings are retained."
                        )
                    verified = result["verification"]
                    assert verified["correct"] and verified["duplicate_count"] == 0
                    assert verified["queue"]["status"] == 1
                    assert verified["queue"]["cursor"] == verified["queue"]["length"]
                    assert verified["queue"]["paid_count"] == verified["queue"]["length"]
                    result.update(scenario=scenario, repetition=index, setup_ms=setup_ms)
                    report["runs"].append(result)
                    report.pop("pending_run", None)
                    checkpoint()
                    print(
                        f"{scenario} / {method}: {len(result['steps'])} transactions, "
                        f"correct={result['verification']['correct']}",
                        flush=True,
                    )
        for run in report["runs"]:
            run["active_step_time_ms"] = active_step_time(run)
        report["methods"] = {
            method: summarize([r for r in report["runs"] if r["method"] == method])
            for method in METHODS
        }
        report["restart"] = []
        for phase in ("sign", "send"):
            app.bridge.call("reset_allowance")
            queue = app.create(length=4, existing=4)["queue"]["address"]
            interrupted = app.step(queue, **{"interrupt_after_" + phase: True})
            app.close()
            app = Application(directory)
            resumed = app.run(queue)
            assert (
                resumed["verification"]["correct"]
                and resumed["verification"]["duplicate_count"] == 0
            )
            report["restart"].append(
                {"interruption": phase, "signed": interrupted, "resumed": resumed}
            )
        # A valid owner-approved executor recipient creates an unseen account-alias shape.
        # It has no fitting support, so a real successful fallback is required.
        app.bridge.call("reset_allowance")
        queue = app.create(
            payments=[{"recipient": app.info["executor"], "amount": "1000"}], existing=0
        )["queue"]["address"]
        fallback = app.run(queue)
        assert fallback["steps"][0]["mode"] == "fallback" and fallback["verification"]["correct"]
        report["unknown_shape_fallback"] = fallback
        app.store.export_jsonl(directory / "audit.jsonl")
        report["status"] = "all_local_execution_gates_passed"
    except Exception as exc:
        report["status"] = "incomplete"
        report["failure"] = str(exc)
        raise
    finally:
        report["control_records"] = [
            record
            for record in app.registry.control_records()
            if record["selection"]["request_id"] not in initial_controls
        ]
        report["control_evaluation"] = summarize_controls(report["control_records"])
        report["lifecycle_events"] = [
            event
            for event in app.registry.audit_events()
            if event["sequence"] > initial_audit_sequence
        ]
        checkpoint()
        write_markdown(report, directory / "execution-report.md")
        app.close()
    return report


def write_markdown(report, path):
    lines = [
        "# Local payout execution evidence",
        "",
        f"Status: **{report.get('status', 'in progress')}**.",
        "",
        "Results use the compiled payout program and an isolated offline Surfpool runtime.",
        "",
    ]
    ablation = report.get("ablation", {})
    if "learned" in ablation and "fixed_estimate_ablation" in ablation:
        lines += [
            f"Model choice executed {ablation['learned']['chosen_count']} payments; "
            "Fixed-estimate ablation executed "
            f"{ablation['fixed_estimate_ablation']['chosen_count']}. "
            "Both verified actual balances and queue progress.",
            "",
        ]
    lines += [
        "| Method | Attempts | Sizing / controls | RPC calls | Completion mean ms (n) | "
        "Active-step sum mean ms (n) | "
        "Fees lamports | Rent deposits lamports |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for method, metrics in report.get("methods", {}).items():
        completion = metrics["queue_completion_ms"]
        completion_mean = (
            f"{completion['mean']:.1f}" if completion["mean"] is not None else "unmeasured"
        )
        active = metrics["active_step_time_ms"]
        active_mean = f"{active['mean']:.1f}" if active["mean"] is not None else "unmeasured"
        lines.append(
            f"| {method} | {metrics['transactions']} | {metrics['estimation_simulations']} / "
            f"{metrics['control_simulations']} | {metrics['rpc_calls']} | "
            f"{completion_mean} ({completion['measured_count']}) | "
            f"{active_mean} ({active['measured_count']}) | "
            f"{metrics['observed_fee_lamports']} | "
            f"{metrics['rent_deposit_lamports']} |"
        )
    lines += [
        "",
        "Resource exceedance counts use successful execution labels only. Missing loaded-data "
        "labels stay missing; joint scoring requires both resources from one execution.",
        "",
        "| Method | CU exceed / labels | Data exceed / labels | Joint exceed / pairs | "
        "Fallback / decisions | Preparation p95 ms (measured n) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for method, metrics in report.get("methods", {}).items():
        preparation = metrics["preparation_ms"]
        preparation_p95 = (
            f"{preparation['p95']:.1f}" if preparation["p95"] is not None else "unmeasured"
        )
        lines.append(
            f"| {method} | {metrics['compute_underestimations']} / "
            f"{metrics['execution_compute_labels']} | {metrics['data_underestimations']} / "
            f"{metrics['execution_loaded_data_labels']} | {metrics['joint_exceedances']} / "
            f"{metrics['execution_paired_labels']} | {metrics['fallback_decisions']} / "
            f"{metrics['transactions']} | {preparation_p95} "
            f"({preparation['measured_count']}) |"
        )
    lines += [
        "",
        "Resource exhaustion is reported separately because failed usage is censored, not "
        "a successful demand label. Its denominator includes every confirmed attempt.",
        "",
        "| Method | CU / data exhausted attempts | Exhausted / confirmed attempts | "
        "Retries after budget failure / RPC retries |",
        "|---|---:|---:|---:|",
    ]
    for method, metrics in report.get("methods", {}).items():
        lines.append(
            f"| {method} | {metrics['confirmed_compute_budget_exhaustions']} / "
            f"{metrics['confirmed_loaded_data_budget_exhaustions']} | "
            f"{metrics['confirmed_budget_exhaustions']} / {metrics['confirmed_attempts']} | "
            f"{metrics['application_retries_after_confirmed_budget_failure']} / "
            f"{metrics['rpc_retries']} |"
        )
    lines += [
        "",
        "Fallback counts exclude decisions simulated solely as sampled controls. Candidate "
        "eligibility includes every learned candidate considered, including candidates not chosen.",
        "",
        "| Method | Model eligible / candidates | Prediction / executed decisions |",
        "|---|---:|---:|",
    ]
    for method, metrics in report.get("methods", {}).items():
        lines.append(
            f"| {method} | {metrics['model_eligible_candidates']} / "
            f"{metrics['model_evaluated_candidates']} | {metrics['prediction_decisions']} / "
            f"{metrics['transactions']} |"
        )
    controls = report.get("control_evaluation")
    if controls:
        lines += [
            "",
            "Sampled-control prediction errors: "
            f"CU {controls['compute_underestimations']} / {controls['compute_labels']}; "
            f"loaded data {controls['loaded_data_underestimations']} / "
            f"{controls['loaded_data_labels']}; joint {controls['joint_exceedances']} / "
            f"{controls['paired_labels']}. Failed simulations: {controls['failed_controls']}.",
        ]
    sensitivity = report.get("resource_holdout", {}).get("capacity_sensitivity")
    if sensitivity:
        declared = sensitivity["declared_policy"]
        protocol = sensitivity["protocol_compute_ceiling"]
        lines += [
            "",
            "Descriptive held-out simulation capacity: "
            f"{declared['within_both']} / {declared['successful_paired_labels']} paired labels "
            f"fit {declared['compute_cap']:,} CU and {declared['loaded_bytes_cap']:,} bytes; "
            f"{protocol['within_both']} / {protocol['successful_paired_labels']} fit "
            f"{protocol['compute_cap']:,} CU with the same loaded-data cap.",
            "",
            sensitivity["interpretation"],
        ]
        eight = sensitivity["by_candidate_count"].get("8")
        if eight:
            lines += [
                "",
                "Count-eight maximum observed CU: "
                f"{eight['maximum_observed_compute_units']} across "
                f"{eight['successful_paired_labels']} successful paired holdout simulations. "
                "All such labels fit the larger cap: "
                f"{sensitivity['all_observed_count8_pairs_fit_protocol_ceiling']}.",
            ]
    lines += [
        "",
        *["- " + item for item in report["limitations"]],
        "",
        "See execution-report.json for every candidate, decision, signature, metadata, "
        "recipient balance, cursor, denominator and timing distribution.",
    ]
    if "failure" in report:
        lines += ["", "Incomplete gate: " + report["failure"]]
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("artifacts/payouts"))
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    run_benchmark(args.directory, args.repeats)
