"""Exercise the real HTTP demo against a fresh, offline Solana runtime.

Run after building TypeScript and the payout program:
    PYTHONPATH=. .venv/bin/python tests/integration/payout_demo.py

This deliberately does not run in the default offline unit-test suite. It uses
only disposable local keys/test tokens and writes a small public-safe report.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx

from cu_pilot.binding import decode_wire, normalize_message
from cu_pilot.features import extract_features
from examples.payouts.server import create_server

ROOT = Path(__file__).resolve().parents[2]
CAP = 1_400_000
DATA_CAP = 1_048_576


def wait_for_runtime(config: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("isolated runtime exited before readiness")
        if config.exists():
            try:
                value = json.loads(config.read_text())
                if value.get("pid") == process.pid and value.get("url"):
                    return
            except (OSError, ValueError):
                pass
        time.sleep(0.1)
    raise RuntimeError("isolated runtime readiness timed out")


def response_json(response: httpx.Response) -> dict:
    response.raise_for_status()
    value = response.json()
    if value.get("error"):
        raise RuntimeError(value["error"])
    return value


def verify_receipts(directory: Path, queue: str, method: str) -> list[dict]:
    with closing(sqlite3.connect(directory / "observations.sqlite")) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute(
            "SELECT body,outcome,signature,wire,phase FROM payout_steps WHERE queue=?", (queue,)
        ).fetchall()
    assert len(rows) == 1, "a verified eight-payment prefix should complete in one batch"
    receipts = []
    for row in rows:
        body, outcome = json.loads(row["body"]), json.loads(row["outcome"])
        decision = body["decision"]
        assert row["phase"] == "confirmed" and outcome["success"] is True
        assert body["method"] == method and body["chosen_count"] == 8
        assert body["limit_source"] == "fresh_simulation"
        assert body["estimation_simulations"] >= 1
        assert decision["status"] == "simulation_success"
        assert 0 < decision["compute_unit_limit"] <= CAP
        assert 0 < decision["loaded_accounts_data_size_limit"] <= DATA_CAP
        transaction = outcome["transaction"]
        assert transaction["meta"]["err"] is None
        assert transaction["transaction"][0] == row["wire"]
        signed = decode_wire(row["wire"])
        assert all(signed.verify_with_results()), "recorded transaction signatures must verify"
        assert str(signed.signatures[0]) == row["signature"] == outcome["signature"]
        features = extract_features(
            normalize_message(signed.message, current_slot=transaction["slot"])
        )
        assert features.requested_compute_units == decision["compute_unit_limit"]
        assert (
            features.requested_loaded_accounts_bytes == decision["loaded_accounts_data_size_limit"]
        )
        actual_cu = transaction["meta"]["computeUnitsConsumed"]
        assert 0 < actual_cu <= decision["compute_unit_limit"]
        if method == "adaptive":
            planning = body["adaptive_planning"]
            assert planning["selected_count"] == 8
            assert planning["limit_source"] == "fresh_simulation"
            assert len(planning["probes"]) == 1
            assert planning["probes"][0]["requested_source"] == "fresh_simulation"
        receipts.append(
            {
                "signature": row["signature"],
                "signature_verified": True,
                "chosen_count": body["chosen_count"],
                "limit_source": body["limit_source"],
                "compute_unit_limit": decision["compute_unit_limit"],
                "compute_units_consumed": actual_cu,
                "loaded_data_limit": decision["loaded_accounts_data_size_limit"],
                "loaded_data_observed": transaction["meta"].get("loadedAccountsDataSize"),
                "estimation_simulations": body["estimation_simulations"],
                "control_simulations": body["control_simulations"],
                "fee_lamports": transaction["meta"]["fee"],
            }
        )
    return receipts


def exercise(client: httpx.Client, directory: Path, method: str) -> dict:
    draft = response_json(client.get("/api/draft?length=8"))
    created = response_json(
        client.post(
            "/api/create",
            json={"id": draft["id"], "payments": draft["payments"], "existing": 0},
        )
    )
    queue = created["queue"]["address"]
    assert created["correct"] and created["queue"]["cursor"] == 0
    assert all(not row["ata_exists"] for row in created["balances"])
    if method == "adaptive":
        executed = response_json(client.post("/api/step", json={"queue": queue, "method": method}))
        assert executed["method"] == method and executed["limit_source"] == "fresh_simulation"
    else:
        started = response_json(client.post("/api/start", json={"queue": queue, "method": method}))
        assert started == {"started": True, "method": method}
    deadline = time.monotonic() + 45
    while True:
        state = response_json(client.get("/api/state"))
        assert state["worker"]["error"] is None, state["worker"]["error"]
        if not state["worker"]["running"]:
            break
        assert time.monotonic() < deadline, "demo worker completion timed out"
        time.sleep(0.05)
    verified = next(item for item in state["queues"] if item["queue"]["address"] == queue)
    assert verified["correct"] and verified["duplicate_count"] == 0
    assert verified["queue"]["cursor"] == verified["queue"]["paid_count"] == 8
    assert verified["queue"]["status"] == 1 and verified["vault_balance"] == "0"
    assert verified["queue"]["payments"] == draft["payments"]
    assert all(row["balance"] == row["amount"] and row["paid"] for row in verified["balances"])
    assert state["last"]["method"] == method and state["last"]["chosen_count"] == 8
    return {
        "method": method,
        "queue": queue,
        "payments": 8,
        "verified_paid": 8,
        "duplicate_payments": 0,
        "receipts": verify_receipts(directory, queue, method),
    }


def main() -> None:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node is required; use the pinned payout bootstrap")
    elf = ROOT / "programs/payout_queue/target/deploy/payout_queue.so"
    if not elf.is_file():
        raise RuntimeError("build the payout program before running the demo integration test")
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "test": "real-local-http-payout-demo-v1",
        "offline_runtime": True,
        "trained_artifact_loaded": False,
        "compute_unit_cap": CAP,
        "loaded_data_cap": DATA_CAP,
        "elf_sha256": hashlib.sha256(elf.read_bytes()).hexdigest(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "examples/payouts/app.py",
                "examples/payouts/server.py",
                "apps/payout-demo/bridge.mjs",
                "apps/payout-demo/index.html",
            )
        },
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="cu-pilot-demo-test-") as temporary:
        directory = Path(temporary)
        config = directory / "runtime.json"
        stop = threading.Event()
        server = None
        thread = None
        with (directory / "runtime.log").open("wb") as log:
            process = subprocess.Popen(
                [node, str(ROOT / "apps/payout-demo/bridge.mjs"), "serve", str(config)],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_for_runtime(config, process)
                server = create_server(directory, 0, compute_unit_cap=CAP, stop_event=stop)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{server.server_port}",
                    timeout=45,
                    trust_env=False,
                ) as client:
                    page = client.get("/")
                    page.raise_for_status()
                    assert 'id="strategy"' in page.text and 'value="adaptive"' in page.text
                    denied = client.post("/api/step", json={"queue": "unapproved"})
                    assert denied.status_code == 403
                    state = response_json(client.get("/api/state"))
                    assert state["model_available"] is False
                    assert state["default_method"] == "always_simulate"
                    assert state["resource_policy"] == {
                        "compute_unit_cap": CAP,
                        "loaded_data_cap": DATA_CAP,
                    }
                    client.headers["X-Payout-Token"] = state["csrf_token"]
                    report["runtime"] = state["info"]["runtime"]
                    report["runtime_instance"] = state["info"]["instance_id"]
                    for method in ("adaptive", "always_simulate"):
                        report["cases"].append(exercise(client, directory, method))
                        print(f"PASS {method}: eight exact payouts and verified signed receipt")
            finally:
                stop.set()
                if server is not None:
                    if thread is not None and thread.is_alive():
                        server.shutdown()
                    server.server_close()
                if thread is not None:
                    thread.join(timeout=5)
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                report["owned_runtime_stopped"] = process.poll() is not None
                report["owned_http_server_stopped"] = thread is None or not thread.is_alive()
    assert report["owned_runtime_stopped"] and report["owned_http_server_stopped"]
    report["passed"] = True
    output = ROOT / "artifacts/payouts/demo-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print("PASS isolated demo cleanup; evidence: artifacts/payouts/demo-verification.json")


if __name__ == "__main__":
    main()
