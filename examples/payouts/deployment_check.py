"""Actual payout -> Token upgrade, lifecycle invalidation and paid fallback.

This bounded integration qualifies its own simulation profile from 40 distinct
queues (20 fitting, 20 calibration), never retargets another runtime's artifact,
and makes no accuracy/generalization claim. Run with scripts/payouts.sh upgrade-test.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from cu_pilot.binding import verify_final_message
from cu_pilot.integration import EstimationContext, ResourceDecision, estimate_resources
from cu_pilot.lifecycle import (
    ProfileManifest,
    ProfileRegistry,
    artifact_digest,
    refresh_deployments,
    runtime_identity_from_version,
)
from cu_pilot.resources import ResourceEstimator, ResourcePolicy
from cu_pilot.rpc import RpcClient
from cu_pilot.schemas import Observation, ResourceLabel

ROOT = Path(__file__).resolve().parents[2]


class Fixture:
    def __init__(self) -> None:
        self.process = subprocess.Popen(
            ["node", str(ROOT / "tests/integration/payout_upgrade.mjs")],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.errors: list[str] = []

        def output() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put("")

        def errors() -> None:
            assert self.process.stderr is not None
            for line in self.process.stderr:
                self.errors.append(line)
                self.errors[:] = self.errors[-80:]

        threading.Thread(target=output, daemon=True).start()
        threading.Thread(target=errors, daemon=True).start()

    def receive(self) -> dict[str, Any]:
        try:
            line = self.lines.get(timeout=60)
        except queue.Empty as exc:
            raise RuntimeError("Incomplete upgrade integration: runtime timed out") from exc
        if not line:
            raise RuntimeError("Incomplete upgrade integration: " + "".join(self.errors))
        result: dict[str, Any] = json.loads(line)
        if "fixture_error" in result:
            raise RuntimeError(result["fixture_error"])
        return result

    def call(self, action: str, **values: Any) -> dict[str, Any]:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"action": action, **values}) + "\n")
        self.process.stdin.flush()
        return self.receive()

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            assert self.process.stdin is not None
            self.process.stdin.write('{"action":"stop"}\n')
            self.process.stdin.flush()
            self.process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=10)


class AtomicAccountReader:
    """One real confirmed RPC batch, sliced without altering values or context."""

    def __init__(self, rpc: RpcClient, addresses: list[str], slot: int) -> None:
        response = rpc.get_multiple_accounts(
            addresses, min_context_slot=slot, commitment="confirmed"
        )
        self.context = response["context"]
        self.values = dict(zip(addresses, response["value"], strict=True))

    def get_multiple_accounts(
        self, addresses: list[str], *, min_context_slot: int, commitment: str
    ) -> dict[str, Any]:
        assert commitment == "confirmed"
        assert self.context["slot"] >= min_context_slot
        return {"context": self.context, "value": [self.values[a] for a in addresses]}


def main() -> None:
    output = ROOT / "artifacts/payouts/deployment-check"
    output.mkdir(parents=True, exist_ok=True)
    # Each invocation has its own database and archive; never modifies demo releases.
    fixture = Fixture()
    try:
        ready = fixture.receive()
        run = output / ready["instance_id"]
        run.mkdir()
        with RpcClient(ready["rpc_url"], requests_per_second=100) as rpc:
            cluster = rpc.get_genesis_hash()
            runtime = (
                runtime_identity_from_version(rpc.get_version()) + ":surfpool-default-features"
            )
            context = EstimationContext(
                context="local:payout-dependency-integration",
                current_slot=ready["slot"],
                cluster_identity=cluster,
                runtime_identity=runtime,
                workload="payout-one-existing-ata",
                budget_independent=True,
            )

            def candidate(index: int, model: str = "0" * 64) -> dict[str, Any]:
                fixture.call("advance", slot=200 + index * 100)
                created = fixture.call(
                    "create", length=1, existing=1, recipient_seed=f"dependency-test:{index}"
                )
                return fixture.call(
                    "candidate",
                    queue=created["queue"]["address"],
                    count=1,
                    decision=f"{index:064x}",
                    model=model,
                )

            observations: list[Observation] = []
            for index in range(40):
                fresh = candidate(index)
                context = context.model_copy(update={"current_slot": fresh["slot"]})
                measured = estimate_resources(fresh["wire"], rpc=rpc, context=context)
                assert measured.status == "simulation_success", measured
                assert measured.plan is not None and measured.simulation is not None
                row = Observation(
                    record_id=f"payout-upgrade-{index}",
                    slot=measured.simulation.slot,
                    context=context.context,
                    source="simulation",
                    label_source="simulation",
                    evidence_origin="local-runtime",
                    features=measured.plan.features,
                    label=ResourceLabel(
                        success=True,
                        compute_units=measured.simulation.units_consumed,
                        loaded_accounts_bytes=measured.simulation.loaded_accounts_bytes,
                    ),
                )
                observations.append(row)
                with (run / "observations.jsonl").open("a") as stream:
                    stream.write(row.model_dump_json() + "\n")
            estimator = ResourceEstimator.fit(
                observations,
                ResourcePolicy(
                    min_samples=12,
                    min_calibration_samples=20,
                    max_joint_underestimation_rate=0.15,
                    # This is a lifecycle regression, not the comparative model.
                    # Wider fixed padding tolerates fresh-identity PDA search costs.
                    compute_margin_bps=5000,
                ),
                calibration_boundary_slot=observations[20].slot,
            )
            artifact = estimator.model.model_dump_json()
            (run / "candidate.json").write_text(artifact)
            registry = ProfileRegistry(run / "profiles.sqlite")
            top_level = sorted(set(observations[0].features.program_ids))
            programs = sorted(
                set(top_level)
                | {
                    ready["dependency"],
                    ready["ata"],
                    ready["system"],
                }
            )
            assert ready["program"] in top_level and ready["dependency"] not in top_level

            def deployments(slot: int) -> Any:
                return refresh_deployments(
                    registry,
                    AtomicAccountReader(rpc, programs + ready["program_data_accounts"], slot),
                    programs,
                    current_slot=slot,
                    cluster_identity=cluster,
                    runtime_identity=runtime,
                    commitment="confirmed",
                )

            fresh = candidate(40, artifact_digest(artifact))
            before = deployments(fresh["slot"])
            context = context.model_copy(
                update={
                    "current_slot": max(item.observed_slot for item in before),
                }
            )
            manifest = ProfileManifest(
                profile_id="payout-upgrade-integration",
                revision=1,
                artifact_sha256=artifact_digest(artifact),
                context=context.context,
                cluster_identity=cluster,
                runtime_identity=runtime,
                workload_allowlist=(context.workload,),
                deployment_bindings={item.program_id: item.fingerprint for item in before},
                dependencies={
                    program: (
                        (ready["dependency"], ready["ata"], ready["system"])
                        if program == ready["program"]
                        else (ready["dependency"], ready["system"])
                        if program == ready["ata"]
                        else ()
                    )
                    for program in programs
                },
                dependency_closure_verified=True,
                budget_independent=True,
                evidence_min_slot=observations[0].slot,
                evidence_max_slot=estimator.model.max_slot,
                provenance="simulation",
                control_probability=0.0,
            )
            registry.register(manifest, artifact, actor="local-integration")
            registry.transition(
                manifest.profile_id,
                1,
                "shadow",
                actor="local-integration",
                reason="bounded local lifecycle integration qualification",
            )
            environment = dict(
                current_slot=context.current_slot,
                context=context.context,
                cluster_identity=cluster,
                runtime_identity=runtime,
                workload=context.workload,
                program_ids=top_level,
            )
            registry.activate(
                manifest.profile_id,
                1,
                actor="local-integration",
                reason="explicit isolated test release; not production qualification",
                **environment,
            )

            def execute(decision: ResourceDecision, fresh: dict[str, Any], name: str) -> Any:
                assert decision.unsigned_transaction_base64 is not None
                # Freeze the entire bound decision before either signing or execution labels.
                (run / f"{name}-decision.json").write_text(decision.model_dump_json(indent=2))
                signed = fixture.call("sign", wire=decision.unsigned_transaction_base64)
                verify_final_message(decision.unsigned_transaction_base64, signed["wire"])
                (run / f"{name}-signature.json").write_text(
                    json.dumps(
                        {
                            "decision": decision.observation_id,
                            "signature": signed["signature"],
                        }
                    )
                )
                sent = fixture.call("send", wire=signed["wire"])
                assert sent["transaction"]["meta"]["err"] is None, sent
                verified = fixture.call("verify", queue=fresh["queue"]["address"])
                assert verified["correct"] and verified["duplicate_count"] == 0
                assert verified["queue"]["cursor"] == verified["queue"]["paid_count"] == 1
                assert verified["balances"][0]["balance"] == "1000"
                assert verified["vault_balance"] == "0"
                evidence = {"decision": decision.observation_id, **sent, "verification": verified}
                (run / f"{name}-outcome.json").write_text(json.dumps(evidence, indent=2))
                return evidence

            accepted = estimate_resources(
                fresh["wire"],
                rpc=rpc,
                context=context,
                estimator=estimator,
                registry=registry,
                profile_id=manifest.profile_id,
            )
            assert accepted.status == "accepted_prediction", accepted
            assert accepted.resource_simulation_calls == 0
            first = execute(accepted, fresh, "qualified")
            fixture.call("advance", slot=5000)
            upgrade = fixture.call("test_upgrade_dependency")
            assert upgrade["actual_loader_upgrade"] and upgrade["code_payload_unchanged"]
            after = deployments(upgrade["current_slot"])
            context = context.model_copy(
                update={
                    "current_slot": max(item.observed_slot for item in after),
                }
            )
            changed = sorted(
                item.program_id
                for item in after
                if item.fingerprint
                != next(
                    previous.fingerprint
                    for previous in before
                    if previous.program_id == item.program_id
                )
            )
            assert changed == [ready["dependency"]], changed
            environment["current_slot"] = context.current_slot
            eligibility = registry.check(manifest.profile_id, **environment)
            assert eligibility.reason == "deployment_changed", eligibility
            assert registry.active_snapshot(manifest.profile_id)[2] == "suspended"
            fresh = candidate(50, artifact_digest(artifact))
            context = context.model_copy(update={"current_slot": fresh["slot"]})
            fallback = estimate_resources(
                fresh["wire"],
                rpc=rpc,
                context=context,
                estimator=estimator,
                registry=registry,
                profile_id=manifest.profile_id,
            )
            assert fallback.status == "simulation_success", fallback
            assert (
                fallback.reason == "profile_suspended" and fallback.resource_simulation_calls == 1
            )
            second = execute(fallback, fresh, "fallback")
            report = dict(
                evidence="actual isolated local runtime",
                runtime=runtime,
                fixture_setup=ready["fixture"],
                dependency_elf_sha256=ready["dependency_elf_sha256"],
                payout_elf_sha256=ready["elf_digest"],
                fit_count=20,
                calibration_count=20,
                holdout_count=0,
                qualification_scope="lifecycle integration, one existing ATA, count one",
                local_joint_risk_threshold=0.15,
                integration_compute_margin_bps=5000,
                profile_state="suspended",
                changed_programs=changed,
                top_level_program_unchanged=True,
                upgrade=upgrade,
                accepted_status=accepted.status,
                accepted_simulations=accepted.resource_simulation_calls,
                accepted_execution=first,
                fallback_status=fallback.status,
                fallback_reason=fallback.reason,
                fallback_simulations=fallback.resource_simulation_calls,
                fallback_execution=second,
                evidence_directory=str(run.relative_to(ROOT)),
            )
            (ROOT / "artifacts/payouts/deployment-check.json").write_text(
                json.dumps(report, indent=2)
            )
            print(
                json.dumps(
                    {
                        key: report[key]
                        for key in (
                            "fit_count",
                            "calibration_count",
                            "changed_programs",
                            "profile_state",
                            "accepted_status",
                            "fallback_status",
                            "fallback_reason",
                            "evidence_directory",
                        )
                    },
                    indent=2,
                )
            )
    finally:
        fixture.close()


if __name__ == "__main__":
    main()
