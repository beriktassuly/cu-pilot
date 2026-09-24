"""Explicit local operator actions; network access only through watch-once."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import Field, StrictInt

from cu_pilot.lifecycle import (
    ProfileManifest,
    ProfileRegistry,
    refresh_deployments,
    runtime_identity_from_version,
)
from cu_pilot.rpc import RpcClient
from cu_pilot.schemas import StrictModel

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


class ReleaseEnvironment(StrictModel):
    current_slot: StrictInt = Field(ge=0, lt=2**64)
    context: str = Field(min_length=1)
    cluster_identity: str = Field(min_length=1)
    runtime_identity: str = Field(min_length=1)
    workload: str = Field(min_length=1)
    program_ids: tuple[str, ...] = Field(min_length=1)


def _print(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, allow_nan=False))


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=path.parent, prefix=".profile-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


@app.command("register")
def register(
    database: Path,
    manifest: Path,
    artifact: Path,
    actor: Annotated[str, typer.Option()],
) -> None:
    """Validate and retain an immutable candidate revision."""
    profile = ProfileManifest.model_validate_json(manifest.read_bytes())
    ProfileRegistry(database).register(profile, artifact.read_bytes(), actor=actor)
    _print(dict(profile_id=profile.profile_id, revision=profile.revision, state="candidate"))


@app.command("shadow")
def shadow(
    database: Path,
    profile_id: str,
    revision: int,
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    """Explicitly move a candidate into shadow; no automatic activation."""
    ProfileRegistry(database).transition(profile_id, revision, "shadow", actor=actor, reason=reason)
    _print(dict(profile_id=profile_id, revision=revision, state="shadow"))


@app.command("release")
def release(
    database: Path,
    profile_id: str,
    revision: int,
    environment: Path,
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    """Explicit activation after cached deployment/freshness rechecks."""
    current = ReleaseEnvironment.model_validate_json(environment.read_bytes())
    ProfileRegistry(database).activate(
        profile_id, revision, actor=actor, reason=reason, **current.model_dump()
    )
    _print(dict(profile_id=profile_id, revision=revision, state="active"))


@app.command("rollback")
def rollback(
    database: Path,
    profile_id: str,
    revision: int,
    environment: Path,
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    """Restore a prior compatible revision without clearing quarantine."""
    current = ReleaseEnvironment.model_validate_json(environment.read_bytes())
    ProfileRegistry(database).rollback(
        profile_id, revision, actor=actor, reason=reason, **current.model_dump()
    )
    _print(dict(profile_id=profile_id, revision=revision, state="active"))


@app.command("suspend")
def suspend(
    database: Path,
    profile_id: str,
    revision: int,
    current_slot: Annotated[int, typer.Option()],
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    """Quarantine a revision; requalification requires a new revision."""
    ProfileRegistry(database).suspend(
        profile_id, revision, current_slot=current_slot, actor=actor, reason=reason
    )
    _print(dict(profile_id=profile_id, revision=revision, state="suspended"))


@app.command("retire")
def retire(
    database: Path,
    profile_id: str,
    revision: int,
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    """Permanently retire a revision while retaining its artifact and history."""
    ProfileRegistry(database).transition(
        profile_id, revision, "retired", actor=actor, reason=reason
    )
    _print(dict(profile_id=profile_id, revision=revision, state="retired"))


@app.command("force-simulation")
def force_simulation(
    database: Path,
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
    enabled: bool = True,
) -> None:
    """Set or clear the emergency switch without changing quarantine evidence."""
    ProfileRegistry(database).force_simulation(enabled, actor=actor, reason=reason)
    _print(dict(force_simulation=enabled))


@app.command("status")
def status(database: Path, profile_id: str) -> None:
    """Inspect current snapshot, controls and audit history without network calls."""
    registry = ProfileRegistry(database)
    try:
        snapshot = registry.export_snapshot(profile_id)
        snapshot.pop("artifact_canonical_json")
    except ValueError:
        snapshot = dict(profile_id=profile_id, state="no_active_profile")
    _print(
        dict(snapshot=snapshot, audit=registry.audit_events(), controls=registry.control_records())
    )


@app.command("export")
def export(database: Path, profile_id: str, output: Path) -> None:
    """Atomically export an expiring portable local runtime snapshot."""
    _atomic_json(output, ProfileRegistry(database).export_snapshot(profile_id))
    _print(dict(output=str(output)))


@app.command("watch-once")
def watch_once(
    database: Path,
    manifest: Path,
    requests_per_second: float = 5.0,
    timeout_seconds: float = 10.0,
    attempts: int = 2,
) -> None:
    """Bounded explicit RPC refresh; reads CU_PILOT_RPC_URL and never submits."""
    endpoint = os.environ.get("CU_PILOT_RPC_URL")
    if not endpoint:
        raise typer.BadParameter("Set CU_PILOT_RPC_URL for this explicit network operation")
    profile = ProfileManifest.model_validate_json(manifest.read_bytes())
    registry = ProfileRegistry(database)
    try:
        with RpcClient(
            endpoint,
            requests_per_second=requests_per_second,
            timeout=timeout_seconds,
            attempts=attempts,
        ) as rpc:
            cluster = rpc.get_genesis_hash()
            runtime = runtime_identity_from_version(rpc.get_version())
            if cluster != profile.cluster_identity or runtime != profile.runtime_identity:
                raise ValueError("Configured RPC cluster/runtime differs from profile")
            observations = refresh_deployments(
                registry,
                rpc,
                tuple(profile.deployment_bindings),
                current_slot=rpc.get_slot(),
                cluster_identity=cluster,
                runtime_identity=runtime,
            )
            _print(
                dict(
                    deployments=[item.model_dump(mode="json") for item in observations],
                    rpc_calls=rpc.call_count,
                    rpc_retries=rpc.retry_count,
                )
            )
    except Exception:
        registry.watcher_failed()
        raise typer.BadParameter("Deployment watcher failed; cached evidence invalidated") from None
