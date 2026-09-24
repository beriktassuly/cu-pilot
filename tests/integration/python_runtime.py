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
from pathlib import Path
from typing import Any

from cu_pilot.integration import EstimationContext
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

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put("")

    threading.Thread(target=pump, daemon=True).start()

    def receive() -> dict[str, Any]:
        try:
            line = lines.get(timeout=45)
        except queue.Empty as exc:
            raise RuntimeError("INCOMPLETE INTEGRATION: local runtime did not respond") from exc
        if not line:
            raise RuntimeError(
                "INCOMPLETE INTEGRATION: native Surfpool unavailable; install the local runtime "
                "package on Linux/macOS, build TypeScript, and configure Linux Node on WSL"
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
