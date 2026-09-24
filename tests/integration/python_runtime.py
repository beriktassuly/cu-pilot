"""Explicit real local integration; no skip when native prerequisites are missing.

Run: uv run python tests/integration/python_runtime.py
Windows: set CU_PILOT_LOCAL_NODE to a JSON command array invoking WSL's Linux node.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from cu_pilot.integration import EstimationContext, estimate_resources
from cu_pilot.lifecycle import (
    ProfileManifest,
    ProfileRegistry,
    artifact_digest,
    refresh_deployments,
    runtime_identity_from_version,
)
from cu_pilot.resource_evaluation import PreparationTrace, evaluate_resources, preparation_report
from cu_pilot.resources import ResourceEstimator
from cu_pilot.rpc import RpcClient
from cu_pilot.shadow import ObservationStore, ShadowRequest, collect_shadow


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    launcher = root / "tests/integration/runtime/serve.mjs"
    command = json.loads(os.environ.get("CU_PILOT_LOCAL_NODE", '["node"]'))
    # WSL callers supply a complete command including the Linux launcher path.
    if not os.environ.get("CU_PILOT_LOCAL_NODE"):
        command.append(str(launcher))
    process = subprocess.Popen(
        command,
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    lines: queue.Queue[str] = queue.Queue()
    runtime_errors: list[str] = []

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put("")

    threading.Thread(target=pump, daemon=True).start()

    def drain_errors() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            runtime_errors.append(line)
            del runtime_errors[:-20]

    threading.Thread(target=drain_errors, daemon=True).start()

    def receive() -> dict[str, Any]:
        try:
            line = lines.get(timeout=45)
        except queue.Empty as exc:
            raise RuntimeError("INCOMPLETE INTEGRATION: local runtime did not respond") from exc
        if not line:
            raise RuntimeError(
                "INCOMPLETE INTEGRATION: local runtime exited. Check native prerequisites. "
                + "".join(runtime_errors)[-3000:]
            )
        return json.loads(line)

    def send(command: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(command) + "\n")
        process.stdin.flush()

    try:
        ready = receive()
        context = EstimationContext(
            context="local:batch",
            current_slot=int(ready["current_slot"]),
            cluster_identity="local-surfpool",
            runtime_identity="surfpool-1.5.0",
            workload="system-transfer-batch",
            budget_independent=True,
        )
        request = ShadowRequest(
            observation_id="local-runtime-1",
            wire_base64=ready["serialized_base64"],
            context=context,
            evidence_origin="local-runtime",
        )
        with tempfile.TemporaryDirectory(prefix="cu-pilot-local-") as directory:
            database = Path(directory) / "observations.sqlite"
            with RpcClient(ready["rpc_url"], requests_per_second=100) as rpc:
                with ObservationStore(database) as store:
                    result = collect_shadow([request], store=store, rpc=rpc, stream="local")
                    record = store.get(request.observation_id)
                    assert record["plan"] is not None
                    assert record["result"]["status"] == "simulation_success", record
                    assert record["outcome"] == "missing"
                    calls = rpc.call_count
                    final = record["result"]["unsigned_transaction_base64"]
                # Reopen the durable store and resume without simulating the completed row again.
                with ObservationStore(database) as store:
                    collect_shadow([request], store=store, rpc=rpc, stream="local")
                    assert rpc.call_count == calls
                    assert len(list(store.export_records())) == 1
                    send({"action": "execute", "serialized_base64": final})
                    execution = receive()
                    signature = store.attach_signature(
                        request.observation_id,
                        execution["serialized_base64"],
                        commitment="finalized",
                    )
                    assert signature == execution["signature"]
                    # Actual metadata comes from getTransaction, not a present-day replay.
                    outcome = rpc.get_transaction_wire(signature, commitment="finalized")
                    assert outcome is not None
                    status = store.reconcile(signature, outcome, commitment="finalized")
                    store.reconcile(signature, outcome, commitment="finalized")
                    rows = list(store.export_records())
                    assert len(rows[0]["execution_outcomes"]) == 1
                    simulated = list(
                        store.training_observations(
                            source="simulation", evidence_origin="local-runtime"
                        )
                    )
                    historical = list(
                        store.training_observations(
                            source="historical", evidence_origin="local-runtime"
                        )
                    )
                    assert len(simulated) == len(historical) == 1
                    assert (
                        historical[0].label.compute_units == outcome["meta"]["computeUnitsConsumed"]
                    )
                    print(
                        json.dumps(
                            {
                                "evidence": "local-runtime",
                                "runtime": ready["runtime"],
                                "collection": result,
                                "reconciliation": status,
                                "simulation_compute_units": simulated[0].label.compute_units,
                                "execution_compute_units": historical[0].label.compute_units,
                                "restart_deduplicated": True,
                                "prediction_persisted_before_label": True,
                            },
                            indent=2,
                        )
                    )
                    # Every label below comes from a fresh, real local simulation.
                    # Slot support is useful test evidence, not independent production sampling.
                    cluster = rpc.get_genesis_hash()
                    runtime = runtime_identity_from_version(rpc.get_version())
                    local_context = context.model_copy(
                        update={"cluster_identity": cluster, "runtime_identity": runtime}
                    )
                    registry = ProfileRegistry(Path(directory) / "profiles.sqlite")
                    programs = sorted(set(record["plan"]["features"]["program_ids"]))
                    builder_costs: dict[str, tuple[float, int]] = {}
                    watcher_state: tuple[int, float] | None = None

                    def prospective_requests():
                        nonlocal watcher_state
                        for index in range(500):
                            send({"action": "next", "slot": 1000 + index * 2})
                            fresh = receive()
                            watcher_started = time.perf_counter()
                            watcher_reads = rpc.call_count
                            if (
                                watcher_state is None
                                or int(fresh["current_slot"]) - watcher_state[0] >= 50
                                or time.time() - watcher_state[1] >= 30
                            ):
                                evidence = refresh_deployments(
                                    registry,
                                    rpc,
                                    programs,
                                    current_slot=int(fresh["current_slot"]),
                                    cluster_identity=cluster,
                                    runtime_identity=runtime,
                                )
                                fresh["current_slot"] = str(
                                    max(item.observed_slot for item in evidence)
                                )
                                watcher_state = (int(fresh["current_slot"]), time.time())
                            builder_costs[f"qualification-{index}"] = (
                                fresh["builder_preparation_ms"]
                                + (time.perf_counter() - watcher_started) * 1000,
                                fresh["builder_state_reads"] + rpc.call_count - watcher_reads,
                            )
                            yield ShadowRequest(
                                observation_id=f"qualification-{index}",
                                wire_base64=fresh["serialized_base64"],
                                context=local_context.model_copy(
                                    update={"current_slot": int(fresh["current_slot"])}
                                ),
                                evidence_origin="local-runtime",
                            )

                    collect_shadow(
                        prospective_requests(),
                        store=store,
                        rpc=rpc,
                        registry=registry,
                        stream="qualification",
                    )
                    observations = [
                        row
                        for row in store.training_observations(
                            source="simulation", evidence_origin="local-runtime"
                        )
                        if row.record_id.startswith("qualification-")
                    ]
                    assert len(observations) == 500
                    assert all(
                        store.get(row.record_id)["plan"]["deployment_evidence_status"]
                        == "observed_unreleased"
                        for row in observations
                    )
                    full_traces = [
                        trace.model_copy(
                            update={
                                "total_ms": trace.total_ms + builder_costs[trace.observation_id][0],
                                "state_reads": trace.state_reads
                                + builder_costs[trace.observation_id][1],
                                "rpc_attempts": trace.rpc_attempts
                                + builder_costs[trace.observation_id][1],
                            }
                        )
                        for trace in store.preparation_traces()
                        if trace.observation_id in builder_costs
                    ]
                    # Freeze100 untouched holdout rows before fitting the first400.
                    estimator = ResourceEstimator.fit(observations[:400])
                    evaluation = evaluate_resources(observations)
                    artifact = estimator.model.model_dump_json()
                    send({"action": "next", "slot": 2000})
                    fresh = receive()
                    next_context = local_context.model_copy(
                        update={"current_slot": int(fresh["current_slot"])}
                    )
                    programs = sorted(set(observations[0].features.program_ids))
                    evidence = refresh_deployments(
                        registry,
                        rpc,
                        programs,
                        current_slot=next_context.current_slot,
                        cluster_identity=cluster,
                        runtime_identity=runtime,
                    )
                    next_context = next_context.model_copy(
                        update={"current_slot": max(item.observed_slot for item in evidence)}
                    )
                    environment = dict(
                        current_slot=next_context.current_slot,
                        context=next_context.context,
                        cluster_identity=cluster,
                        runtime_identity=runtime,
                        workload=next_context.workload,
                        program_ids=programs,
                    )
                    manifest = ProfileManifest(
                        profile_id="local-batch",
                        revision=1,
                        artifact_sha256=artifact_digest(artifact),
                        context=next_context.context,
                        cluster_identity=cluster,
                        runtime_identity=runtime,
                        workload_allowlist=(next_context.workload,),
                        deployment_bindings={
                            item.program_id: item.fingerprint for item in evidence
                        },
                        dependencies={program: () for program in programs},
                        dependency_closure_verified=True,
                        budget_independent=True,
                        evidence_min_slot=min(row.slot for row in observations),
                        evidence_max_slot=estimator.model.max_slot,
                        provenance="simulation",
                        control_probability=0.0,
                    )
                    registry.register(manifest, artifact, actor="local-test-operator")
                    registry.transition(
                        "local-batch",
                        1,
                        "shadow",
                        actor="local-test-operator",
                        reason="test review",
                    )
                    registry.activate(
                        "local-batch",
                        1,
                        actor="local-test-operator",
                        reason="explicit local test release; not production",
                        **environment,
                    )
                    before = rpc.call_count
                    accepted = estimate_resources(
                        fresh["serialized_base64"],
                        rpc=rpc,
                        context=next_context,
                        estimator=estimator,
                        registry=registry,
                        profile_id="local-batch",
                    )
                    assert accepted.status == "accepted_prediction", accepted
                    assert rpc.call_count == before
                    sampled = manifest.model_copy(
                        update={"revision": 2, "control_probability": 0.05}
                    )
                    registry.register(sampled, artifact, actor="local-test-operator")
                    registry.transition(
                        "local-batch",
                        2,
                        "shadow",
                        actor="local-test-operator",
                        reason="timing test",
                    )
                    registry.activate(
                        "local-batch",
                        2,
                        actor="local-test-operator",
                        reason="explicit sampled local comparison",
                        **environment,
                    )
                    timings: dict[str, list[PreparationTrace]] = {
                        "always_simulate": [],
                        "released_policy": [],
                    }
                    statuses: dict[str, Counter[str]] = {method: Counter() for method in timings}
                    # Interleave actual methods. Account state is unchanged by simulation.
                    # Fresh builder reads and periodic watcher cost are included; local
                    # signed bank-advancement fixture transactions are excluded as above.
                    for index in range(100):
                        for method in timings:
                            send({"action": "next", "slot": 3000 + index * 2})
                            fresh = receive()
                            next_context = local_context.model_copy(
                                update={"current_slot": int(fresh["current_slot"])}
                            )
                            started = time.perf_counter()
                            rpc_start = rpc.call_count
                            watcher_reads = 0
                            if method == "released_policy" and (
                                next_context.current_slot
                                - min(item.observed_slot for item in evidence)
                                >= 50
                                or time.time() - min(item.checked_at for item in evidence) >= 30
                            ):
                                evidence = refresh_deployments(
                                    registry,
                                    rpc,
                                    programs,
                                    current_slot=next_context.current_slot,
                                    cluster_identity=cluster,
                                    runtime_identity=runtime,
                                )
                                watcher_reads = rpc.call_count - rpc_start
                                next_context = next_context.model_copy(
                                    update={
                                        "current_slot": max(item.observed_slot for item in evidence)
                                    }
                                )
                            measured = estimate_resources(
                                fresh["serialized_base64"],
                                rpc=rpc,
                                context=next_context,
                                estimator=estimator if method == "released_policy" else None,
                                registry=registry if method == "released_policy" else None,
                                profile_id="local-batch" if method == "released_policy" else None,
                            )
                            elapsed = (time.perf_counter() - started) * 1000
                            assert measured.status != "unresolved", measured
                            if method == "released_policy":
                                assert measured.plan.eligibility_reason == "calibrated_resources", (
                                    measured
                                )
                            statuses[method][measured.status] += 1
                            timings[method].append(
                                PreparationTrace(
                                    observation_id=measured.observation_id,
                                    evidence="local-runtime",
                                    mode="deployment",
                                    total_ms=elapsed + fresh["builder_preparation_ms"],
                                    resource_estimation_calls=(
                                        measured.resource_simulation_calls
                                        if not measured.control_selected
                                        else 0
                                    ),
                                    control_simulation_calls=(
                                        measured.resource_simulation_calls
                                        if measured.control_selected
                                        else 0
                                    ),
                                    rpc_attempts=(
                                        rpc.call_count - rpc_start + fresh["builder_state_reads"]
                                    ),
                                    state_reads=fresh["builder_state_reads"] + watcher_reads,
                                )
                            )
                    environment["current_slot"] = next_context.current_slot
                    audited = manifest.model_copy(
                        update={"revision": 3, "control_probability": 1.0}
                    )
                    registry.register(audited, artifact, actor="local-test-operator")
                    registry.transition(
                        "local-batch", 3, "shadow", actor="local-test-operator", reason="audit test"
                    )
                    registry.activate(
                        "local-batch",
                        3,
                        actor="local-test-operator",
                        reason="explicit fully audited local test release",
                        **environment,
                    )
                    send({"action": "grow", "bytes": 65536})
                    receive()
                    controlled = estimate_resources(
                        fresh["serialized_base64"],
                        rpc=rpc,
                        context=next_context,
                        estimator=estimator,
                        registry=registry,
                        profile_id="local-batch",
                    )
                    assert controlled.control_selected, controlled
                    assert controlled.simulation is not None, controlled
                    assert (
                        controlled.simulation.loaded_accounts_bytes
                        > accepted.loaded_accounts_data_size_limit
                    )
                    assert not registry.check("local-batch", **environment).eligible
                    print(
                        json.dumps(
                            {
                                "evidence": "local-runtime",
                                "prospective_paired_samples": 500,
                                "untouched_holdout_samples": evaluation["test_count"],
                                "fit_slot_groups": next(
                                    iter(estimator.model.patterns.values())
                                ).train_count,
                                "calibration_slot_groups": next(
                                    iter(estimator.model.patterns.values())
                                ).calibration_count,
                                "explicit_test_operator_activation": True,
                                "accepted_prediction_sizing_rpc_calls": 0,
                                "grown_account_data_bytes": (
                                    controlled.simulation.loaded_accounts_bytes
                                ),
                                "sampled_control_suspended_profile": True,
                                "production_validation": False,
                                "measured_shadow_preparation": preparation_report(full_traces),
                                "measurement_scope": (
                                    "Sequential SDK blockhash/slot reads and builder binding plus "
                                    "Python adapter, RPC simulation and durable collection; "
                                    "local test bank-advancement transactions excluded"
                                ),
                                "evaluation_methods": evaluation["methods"],
                                "measured_method_comparison": {
                                    method: {
                                        "preparation": preparation_report(traces),
                                        "decision_statuses": dict(statuses[method]),
                                        "observed_labels": sum(
                                            t.resource_estimation_calls + t.control_simulation_calls
                                            for t in traces
                                        ),
                                        "unobserved_requests": 100
                                        - sum(
                                            t.resource_estimation_calls + t.control_simulation_calls
                                            for t in traces
                                        ),
                                        "configured_control_probability": (
                                            0.05 if method == "released_policy" else 0
                                        ),
                                        "actual_sizing_calls_avoided": 100
                                        - sum(
                                            t.resource_estimation_calls + t.control_simulation_calls
                                            for t in traces
                                        ),
                                    }
                                    for method, traces in timings.items()
                                },
                            },
                            indent=2,
                        )
                    )
    finally:
        if process.poll() is None:
            try:
                send({"action": "stop"})
                process.wait(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
