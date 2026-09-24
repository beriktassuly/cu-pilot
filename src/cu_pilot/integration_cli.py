"""Explicit network and offline workflows for the bound resource integration."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import typer

from cu_pilot.binding import bind_message
from cu_pilot.data import load_observations, read_jsonl, write_observations
from cu_pilot.integration import EstimationContext, estimate_resources
from cu_pilot.lifecycle import ProfileRegistry
from cu_pilot.resource_evaluation import evaluate_resources, preparation_report
from cu_pilot.resources import ResourceEstimator, ResourcePolicy
from cu_pilot.rpc import RpcClient
from cu_pilot.shadow import ObservationStore, ShadowRequest, collect_shadow

app = typer.Typer(pretty_exceptions_enable=False)


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, allow_nan=False))


def _endpoint() -> str:
    endpoint = os.environ.get("CU_PILOT_RPC_URL")
    if not endpoint:
        raise typer.BadParameter("Set CU_PILOT_RPC_URL for this explicit network operation")
    return endpoint


@app.command("preparation-report")
def report_preparation(database: Path, output: Path | None = None) -> None:
    """Report measured end-to-end shadow latency and calls by evidence origin."""
    with ObservationStore(database) as store:
        traces = store.preparation_traces()
    groups = {(trace.evidence, trace.mode, trace.collection_method) for trace in traces}
    report = {
        evidence + "/" + mode + "/" + collection: preparation_report(
            [
                trace
                for trace in traces
                if (trace.evidence, trace.mode, trace.collection_method)
                == (evidence, mode, collection)
            ]
        )
        for evidence, mode, collection in sorted(groups)
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _print(report)


@app.command("estimate-resources")
def estimate(
    transaction: Path,
    context: Annotated[Path, typer.Option()],
    model: Path | None = None,
    registry: Path | None = None,
    profile: str | None = None,
    force_simulation: bool = False,
) -> None:
    """Bound unsigned preparation: local accepted limits, actual simulation, or unresolved."""
    ctx = EstimationContext.model_validate_json(context.read_text(encoding="utf-8"))
    estimator = ResourceEstimator.load(model) if model else None
    profiles = ProfileRegistry(registry) if registry else None
    if profiles is not None and profile is not None and estimator is None:
        _, estimator = profiles.load_active(profile)
    with RpcClient(_endpoint()) as rpc:
        result = estimate_resources(
            transaction.read_text().strip(),
            rpc=rpc,
            context=ctx,
            estimator=estimator,
            registry=profiles,
            profile_id=profile,
            force_simulation=force_simulation,
        )
    _print(result.model_dump(mode="json"))
    if result.status == "unresolved":
        raise typer.Exit(2)


@app.command("shadow")
def shadow(
    inputs: Path,
    database: Path,
    model: Path | None = None,
    replay: bool = False,
    stream: str = "default",
    max_records: Annotated[int, typer.Option(min=1, max=100000)] = 1000,
    requests_per_second: Annotated[float, typer.Option(min=0.01)] = 10,
) -> None:
    """Persist prediction first and simulate every input; --replay uses recorded responses."""
    estimator = ResourceEstimator.load(model) if model else None
    current: list[ShadowRequest] = []

    def requests() -> Iterator[ShadowRequest]:
        for row in read_jsonl(inputs):
            request = ShadowRequest.model_validate(row)
            if replay:
                request = request.model_copy(update={"collection_method": "offline-replay"})
            current[:] = [request]
            yield request

    def replay_transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        record = current[0]
        if record.replay_response is None:
            raise ValueError("offline replay requires a recorded simulation response")
        expected = bind_message(
            record.wire_base64,
            current_slot=record.context.current_slot,
            lookups={table.address: table for table in record.lookups},
        )
        if (
            payload["method"] != "simulateTransaction"
            or payload["params"][0] != expected.wire_base64
        ):
            raise ValueError("replay has no evidence for this simulation message")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": record.replay_response}
        )

    with (
        ObservationStore(database) as store,
        RpcClient(
            "http://offline-replay.invalid" if replay else _endpoint(),
            transport=httpx.MockTransport(replay_transport) if replay else None,
            requests_per_second=1e9 if replay else requests_per_second,
        ) as rpc,
    ):
        result = collect_shadow(
            requests(),
            store=store,
            rpc=rpc,
            estimator=estimator,
            stream=stream,
            max_records=max_records,
        )
    _print({**result, "collection_mode": "offline-replay" if replay else "prospective-shadow"})


@app.command("export-shadow")
def export_shadow(
    database: Path,
    output: Path,
    training_source: str | None = None,
    evidence_origin: str | None = None,
) -> None:
    """Export full audit JSONL, or explicitly selected simulation/execution training labels."""
    with ObservationStore(database) as store:
        if training_source is None:
            store.export_jsonl(output)
        else:
            if training_source not in {"simulation", "historical"} or evidence_origin is None:
                raise typer.BadParameter("Choose simulation/historical and --evidence-origin")
            source: Literal["simulation", "historical"] = (
                "simulation" if training_source == "simulation" else "historical"
            )
            write_observations(
                output,
                list(store.training_observations(source=source, evidence_origin=evidence_origin)),
            )
    _print({"output": str(output)})


@app.command("reconcile")
def reconcile(
    database: Path,
    observation_id: str,
    signed_transaction: Path,
    result: Path | None = None,
    commitment: str = "finalized",
    allow_blockhash_refresh: bool = False,
    registry: Path | None = None,
) -> None:
    """Join caller-signed bytes to supplied execution metadata or a bounded RPC read."""
    with ObservationStore(database) as store:
        signature = store.attach_signature(
            observation_id,
            signed_transaction.read_text().strip(),
            commitment=commitment,
            allow_blockhash_refresh=allow_blockhash_refresh,
        )
        if result is not None:
            outcome = json.loads(result.read_text(encoding="utf-8"))
        else:
            with RpcClient(_endpoint()) as rpc:
                outcome = rpc.get_transaction_wire(signature, commitment=commitment)
        status = store.reconcile(
            signature,
            outcome,
            commitment=commitment,
            registry=ProfileRegistry(registry) if registry else None,
        )
    _print({"observation_id": observation_id, "signature": signature, "outcome": status})


@app.command("train-resources")
def train_resources(dataset: Path, output: Path, policy: Path | None = None) -> None:
    """Fit a paired-resource candidate. Training never activates a release."""
    configuration = (
        ResourcePolicy.model_validate_json(policy.read_text()) if policy else ResourcePolicy()
    )
    estimator = ResourceEstimator.fit(load_observations(dataset), policy=configuration)
    output.parent.mkdir(parents=True, exist_ok=True)
    estimator.save(output)
    _print(
        {
            "artifact": str(output),
            "version": estimator.model.artifact_version,
            "patterns": len(estimator.model.patterns),
            "state": "candidate",
        }
    )


@app.command("evaluate-resources")
def evaluate_resource_profiles(dataset: Path, output: Path, policy: Path | None = None) -> None:
    """Compare dual-resource baselines on an untouched holdout; no network calls."""
    configuration = (
        ResourcePolicy.model_validate_json(policy.read_text()) if policy else ResourcePolicy()
    )
    report = evaluate_resources(load_observations(dataset), policy=configuration)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _print(
        {"report": str(output), "evidence": "offline-replay", "production_release_evaluated": False}
    )
