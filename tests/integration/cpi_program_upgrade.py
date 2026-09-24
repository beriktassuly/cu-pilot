"""Real ATA -> Token CPI, dependency-only Upgrade, suspension and fallback.

Run after npm ci in typescript and tests/integration/runtime:
    uv run python tests/integration/cpi_program_upgrade.py
On Windows set CU_PILOT_CPI_UPGRADE_NODE to the full JSON WSL launch command.
Missing runtime prerequisites fail as an incomplete check, never a passing skip.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
from collections.abc import Sequence
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
from cu_pilot.resources import ResourceEstimator, ResourcePolicy
from cu_pilot.rpc import RpcClient
from cu_pilot.schemas import Observation, ResourceLabel


class AtomicAccountReader:
    """Slice one actual RPC account batch; no account or context is fabricated.

    The local fixture already knows its ProgramData pointer. Fetching the Program
    and ProgramData together avoids crossing bank contexts between RPC calls.
    refresh_deployments still checks the real Program pointer against this map.
    """

    def __init__(self, rpc: RpcClient, addresses: Sequence[str], current_slot: int) -> None:
        response = rpc.get_multiple_accounts(
            addresses, min_context_slot=current_slot, commitment="finalized"
        )
        self.context = response["context"]
        self.values = dict(zip(addresses, response["value"], strict=True))

    def get_multiple_accounts(
        self, addresses: Sequence[str], *, min_context_slot: int, commitment: str
    ) -> dict[str, Any]:
        assert commitment == "finalized"
        assert self.context["slot"] >= min_context_slot
        return {"context": self.context, "value": [self.values[item] for item in addresses]}


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    command = json.loads(os.environ.get("CU_PILOT_CPI_UPGRADE_NODE", '["node"]'))
    if not os.environ.get("CU_PILOT_CPI_UPGRADE_NODE"):
        command.append(str(root / "tests/integration/cpi_upgrade.mjs"))
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        raise RuntimeError(
            "INCOMPLETE INTEGRATION: Node launch failed; configure the local runtime executable"
        ) from None
    lines: queue.Queue[str] = queue.Queue()
    errors: list[str] = []

    def output_pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put("")

    def error_pump() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            if len(errors) < 80:
                errors.append(line)

    threading.Thread(target=output_pump, daemon=True).start()
    threading.Thread(target=error_pump, daemon=True).start()

    def receive() -> dict[str, Any]:
        try:
            line = lines.get(timeout=45)
        except queue.Empty as exc:
            raise RuntimeError("INCOMPLETE INTEGRATION: local upgrade runtime timed out") from exc
        if not line:
            raise RuntimeError(
                "INCOMPLETE INTEGRATION: local upgrade runtime failed; install native Surfpool "
                "and loader-v3 SDK, or configure Linux Node under WSL. " + "".join(errors)
            )
        result = json.loads(line)
        assert isinstance(result, dict)
        return result

    def send(action: str, slot: int | None = None) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(dict(action=action, slot=slot)) + "\n")
        process.stdin.flush()

    try:
        ready = receive()
        assert ready["actual_cpi_succeeded"] is True
        with tempfile.TemporaryDirectory(prefix="cu-pilot-upgrade-") as directory:
            with RpcClient(ready["rpc_url"], requests_per_second=100) as rpc:
                cluster = rpc.get_genesis_hash()
                runtime = (
                    runtime_identity_from_version(rpc.get_version()) + ":surfpool-default-features"
                )
                observations: list[Observation] = []
                for index in range(6):
                    send("sample", 200 + 100 * index)
                    fresh = receive()
                    context = EstimationContext(
                        context="local:upgrade-test",
                        current_slot=int(fresh["current_slot"]),
                        cluster_identity=cluster,
                        runtime_identity=runtime,
                        workload="local-associated-token-create",
                        budget_independent=True,
                    )
                    measured = estimate_resources(
                        fresh["serialized_base64"], rpc=rpc, context=context
                    )
                    assert measured.status == "simulation_success", measured
                    assert measured.plan is not None and measured.simulation is not None
                    observations.append(
                        Observation(
                            record_id=f"local-upgrade-{index}",
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
                    )
                # Deliberately weak test-only support/risk settings exercise the release
                # transition with measured labels. They are not production qualification.
                estimator = ResourceEstimator.fit(
                    observations,
                    ResourcePolicy(
                        min_samples=2,
                        min_calibration_samples=2,
                        max_joint_underestimation_rate=0.8,
                        calibration_fraction=0.5,
                    ),
                )
                artifact = estimator.model.model_dump_json()
                registry = ProfileRegistry(Path(directory) / "profiles.sqlite")
                send("sample", 1000)
                fresh = receive()
                context = context.model_copy(update={"current_slot": int(fresh["current_slot"])})
                top_level_programs = sorted(set(observations[0].features.program_ids))
                assert ready["top_level_program"] in top_level_programs
                assert ready["dependency_program"] not in top_level_programs
                programs = sorted(
                    {*top_level_programs, ready["dependency_program"], ready["system_program"]}
                )
                before = refresh_deployments(
                    registry,
                    AtomicAccountReader(
                        rpc, [*programs, *ready["program_data_accounts"]], context.current_slot
                    ),
                    programs,
                    current_slot=context.current_slot,
                    cluster_identity=cluster,
                    runtime_identity=runtime,
                )
                # Surfpool getSlot can lag an account response context by one slot.
                # Use the latest actually observed pre-execution context, never invent one.
                context = context.model_copy(
                    update={"current_slot": max(item.observed_slot for item in before)}
                )
                manifest = ProfileManifest(
                    profile_id="local-upgrade",
                    revision=1,
                    artifact_sha256=artifact_digest(artifact),
                    context=context.context,
                    cluster_identity=cluster,
                    runtime_identity=runtime,
                    workload_allowlist=(context.workload,),
                    deployment_bindings={item.program_id: item.fingerprint for item in before},
                    dependencies={
                        program: (ready["dependency_program"], ready["system_program"])
                        if program == ready["top_level_program"]
                        else ()
                        for program in programs
                    },
                    dependency_closure_verified=True,
                    budget_independent=True,
                    evidence_min_slot=min(item.slot for item in observations),
                    evidence_max_slot=estimator.model.max_slot,
                    provenance="simulation",
                    control_probability=0.0,
                )
                registry.register(manifest, artifact, actor="local-test")
                registry.transition(
                    "local-upgrade", 1, "shadow", actor="local-test", reason="fixture review"
                )
                environment = dict(
                    current_slot=context.current_slot,
                    context=context.context,
                    cluster_identity=cluster,
                    runtime_identity=runtime,
                    workload=context.workload,
                    program_ids=top_level_programs,
                )
                registry.activate(
                    "local-upgrade",
                    1,
                    actor="local-test",
                    reason="explicit local test only; not production evidence",
                    **environment,
                )
                calls = rpc.call_count
                accepted = estimate_resources(
                    fresh["serialized_base64"],
                    rpc=rpc,
                    context=context,
                    estimator=estimator,
                    registry=registry,
                    profile_id="local-upgrade",
                )
                assert accepted.status == "accepted_prediction", accepted
                assert rpc.call_count == calls

                send("upgrade", 2000)
                upgrade = receive()
                assert upgrade["actual_loader_upgrade"] and upgrade["err"] is None
                assert int(upgrade["new_deployment_slot"]) > int(upgrade["old_deployment_slot"])
                context = context.model_copy(update={"current_slot": int(upgrade["current_slot"])})
                after = refresh_deployments(
                    registry,
                    AtomicAccountReader(
                        rpc, [*programs, *ready["program_data_accounts"]], context.current_slot
                    ),
                    programs,
                    current_slot=context.current_slot,
                    cluster_identity=cluster,
                    runtime_identity=runtime,
                )
                context = context.model_copy(
                    update={"current_slot": max(item.observed_slot for item in after)}
                )
                old = next(item for item in before if item.program_id == ready["program"])
                new = next(item for item in after if item.program_id == ready["program"])
                assert old.fingerprint != new.fingerprint
                assert new.deployment_slot == int(upgrade["new_deployment_slot"])
                changed = {
                    item.program_id
                    for item in after
                    if item.fingerprint
                    != next(old.fingerprint for old in before if old.program_id == item.program_id)
                }
                assert changed == {ready["dependency_program"]}, changed
                environment["current_slot"] = context.current_slot
                eligibility = registry.check("local-upgrade", **environment)
                assert eligibility.reason == "deployment_changed", eligibility
                assert registry.active_snapshot("local-upgrade")[2] == "suspended"
                send("sample", 2100)
                post = receive()
                context = context.model_copy(update={"current_slot": int(post["current_slot"])})
                fallback = estimate_resources(
                    post["serialized_base64"],
                    rpc=rpc,
                    context=context,
                    estimator=estimator,
                    registry=registry,
                    profile_id="local-upgrade",
                )
                assert fallback.status == "simulation_success", fallback
                assert fallback.resource_simulation_calls == 1
                assert fallback.reason == "profile_suspended", fallback
                print(
                    json.dumps(
                        dict(
                            evidence="local-runtime",
                            surfpool="1.5.0",
                            runtime=ready["runtime"],
                            feature_configuration="default (not allFeatures)",
                            instruction_sdk=upgrade["instruction_sdk"],
                            elf_source="bundled local Token",
                            elf_bytes=ready["elf_bytes"],
                            signed_upgrade_succeeded=True,
                            upgrade_compute_units=upgrade["units_consumed"],
                            old_deployment_slot=upgrade["old_deployment_slot"],
                            new_deployment_slot=upgrade["new_deployment_slot"],
                            code_payload_unchanged=upgrade["code_payload_unchanged"],
                            qualifying_local_observations=len(observations),
                            test_only_joint_risk_threshold=0.8,
                            accepted_before_upgrade=True,
                            actual_token_and_system_cpi_succeeded=True,
                            cpi_compute_units=ready["cpi_compute_units"],
                            top_level_program_unchanged=True,
                            changed_programs=sorted(changed),
                            suspension_reason=eligibility.reason,
                            post_upgrade_status=fallback.status,
                            post_upgrade_reason=fallback.reason,
                            post_upgrade_simulation_calls=fallback.resource_simulation_calls,
                        ),
                        indent=2,
                    )
                )
    finally:
        if process.poll() is None:
            try:
                send("stop")
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
