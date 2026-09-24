"""Durable prospective collection and signature-bound execution reconciliation.

One worker and no prefetched queue provide bounded memory and natural backpressure.
Plans commit before simulation; completion and stream checkpoints commit together.
Retries are attempt events, never additional training observations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import Field, ValidationError

from cu_pilot.binding import (
    LookupEvidence,
    bind_message,
    decode_wire,
    message_identity,
    replace_resources,
    unsigned_wire,
    verify_final_message,
)
from cu_pilot.integration import (
    DecisionPlan,
    EstimationContext,
    ResourceDecision,
    execute_decision,
    prepare_decision,
)
from cu_pilot.resource_evaluation import PreparationTrace
from cu_pilot.resources import ResourceEstimator
from cu_pilot.rpc import RpcClient, RpcError
from cu_pilot.schemas import Observation, ResourceLabel, StrictModel

if TYPE_CHECKING:
    from cu_pilot.lifecycle import ProfileRegistry


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class RecordConflict(ValueError):
    """An identifier was reused for different evidence; the original is retained."""


class ShadowRequest(StrictModel):
    observation_id: str = Field(min_length=1)
    wire_base64: str
    context: EstimationContext
    lookups: tuple[LookupEvidence, ...] = ()
    evidence_origin: Literal["synthetic", "local-runtime", "live-simulation"]
    collection_method: Literal["prospective", "offline-replay"] = "prospective"
    replay_response: dict[str, Any] | None = None


class ObservationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self.db.close()
            raise ValueError("unsupported observation database version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY, input TEXT NOT NULL, plan TEXT, result TEXT,
                phase TEXT NOT NULL DEFAULT 'pending', outcome TEXT NOT NULL DEFAULT 'missing'
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES observations(id),
                result TEXT NOT NULL, recorded_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                stream TEXT PRIMARY KEY, cursor INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conflicts (
                id INTEGER PRIMARY KEY, observation_id TEXT NOT NULL, kind TEXT NOT NULL,
                incoming_digest TEXT NOT NULL, recorded_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signatures (
                signature TEXT PRIMARY KEY,
                observation_id TEXT NOT NULL REFERENCES observations(id),
                wire TEXT NOT NULL, commitment TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY, signature TEXT NOT NULL REFERENCES signatures(signature),
                body TEXT NOT NULL, digest TEXT NOT NULL, recorded_at REAL NOT NULL,
                UNIQUE(signature, digest)
            );
            CREATE TABLE IF NOT EXISTS preparation_traces (
                observation_id TEXT PRIMARY KEY REFERENCES observations(id), body TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)

    def __enter__(self) -> ObservationStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.db.close()

    def _conflict(self, identifier: str, kind: str, incoming: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO conflicts VALUES(NULL,?,?,?,?)",
                (identifier, kind, hashlib.sha256(incoming.encode()).hexdigest(), time.time()),
            )
        raise RecordConflict(f"conflicting {kind}; original observation preserved")

    def conflict_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0])

    def ingest(self, identifier: str, request: dict[str, Any]) -> bool:
        payload = _json(request)
        with self.db:
            changed = self.db.execute(
                "INSERT OR IGNORE INTO observations(id,input) VALUES(?,?)", (identifier, payload)
            ).rowcount
        row = self.db.execute("SELECT input FROM observations WHERE id=?", (identifier,)).fetchone()
        if row[0] != payload:
            # Schema additions may supply defaults that were absent in a durable
            # older request. Compare validated values without rewriting original
            # input or its pre-label plan. Arbitrary ingest payloads remain exact.
            try:
                previous = ShadowRequest.model_validate_json(row[0])
                incoming = ShadowRequest.model_validate_json(payload)
                equivalent = previous.model_dump() == incoming.model_dump()
            except ValidationError:
                equivalent = False
            if not equivalent:
                self._conflict(identifier, "input", payload)
        return bool(changed)

    def get(self, identifier: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM observations WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        result = dict(row)
        for key in ("input", "plan", "result"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def freeze_plan(self, identifier: str, plan: dict[str, Any]) -> None:
        payload = _json(plan)
        with self.db:
            self.db.execute(
                "UPDATE observations SET plan=?, phase='planned' WHERE id=? AND plan IS NULL",
                (payload, identifier),
            )
        if _json(self.get(identifier)["plan"]) != payload:
            self._conflict(identifier, "pre-label plan", payload)

    def finish(self, identifier: str, result: dict[str, Any], *, stream: str, cursor: int) -> None:
        if type(cursor) is not int or cursor < 0:
            raise ValueError("invalid checkpoint cursor")
        payload = _json(result)
        with self.db:
            # Multiple local collectors may race; only the first completed label
            # wins. Conflicting outcomes are evidence, never silent replacements.
            self.db.execute("BEGIN IMMEDIATE")
            prior = self.db.execute(
                "SELECT result FROM observations WHERE id=?", (identifier,)
            ).fetchone()
            if prior is None:
                raise KeyError(identifier)
            if prior[0] is not None and prior[0] != payload:
                self.db.rollback()
                self._conflict(identifier, "completed result", payload)
            if prior[0] is None:
                self.db.execute(
                    "INSERT INTO attempts VALUES(NULL,?,?,?)", (identifier, payload, time.time())
                )
                self.db.execute(
                    "UPDATE observations SET result=?,phase='complete' WHERE id=?",
                    (payload, identifier),
                )
            self.db.execute(
                "INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(stream) DO UPDATE "
                "SET cursor=MAX(cursor,excluded.cursor)",
                (stream, cursor),
            )

    def checkpoint(self, stream: str) -> int:
        row = self.db.execute("SELECT cursor FROM checkpoints WHERE stream=?", (stream,)).fetchone()
        return int(row[0]) if row else 0

    def record_preparation(self, trace: PreparationTrace) -> None:
        """Separate telemetry commit after the measured durable completion commit."""
        payload = _json(trace.model_dump(mode="json"))
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO preparation_traces VALUES(?,?)",
                (trace.observation_id, payload),
            )
        old = self.db.execute(
            "SELECT body FROM preparation_traces WHERE observation_id=?", (trace.observation_id,)
        ).fetchone()
        if old[0] != payload:
            self._conflict(trace.observation_id, "preparation trace", payload)

    def preparation_traces(self) -> list[PreparationTrace]:
        return [
            PreparationTrace.model_validate_json(row[0])
            for row in self.db.execute("SELECT body FROM preparation_traces ORDER BY rowid")
        ]

    def export_records(self) -> Iterator[dict[str, Any]]:
        for row in self.db.execute("SELECT id FROM observations ORDER BY rowid"):
            record = self.get(row[0])
            timing = self.db.execute(
                "SELECT body FROM preparation_traces WHERE observation_id=?", (row[0],)
            ).fetchone()
            yield {
                "schema_version": "cu-pilot-shadow-v1",
                "observation_id": row[0],
                "preparation_trace": json.loads(timing[0]) if timing else None,
                "execution_signature_count": self.db.execute(
                    "SELECT COUNT(*) FROM signatures WHERE observation_id=?", (row[0],)
                ).fetchone()[0],
                **record,
                "execution_outcomes": [
                    json.loads(item[0])
                    for item in self.db.execute(
                        "SELECT o.body FROM outcomes o "
                        "JOIN signatures s ON s.signature=o.signature "
                        "WHERE s.observation_id=? ORDER BY o.id",
                        (row[0],),
                    )
                ],
            }

    def export_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as output:
            for row in self.export_records():
                output.write(_json(row) + "\n")

    def training_observations(
        self, *, source: Literal["simulation", "historical"], evidence_origin: str
    ) -> Iterator[Observation]:
        """Explicit source/origin selection; never substitutes simulation for execution."""
        if evidence_origin not in {"synthetic", "local-runtime", "live-simulation"}:
            raise ValueError("training export requires the original evidence origin")
        for record in self.export_records():
            if record["input"].get("evidence_origin") != evidence_origin or not record["plan"]:
                continue
            plan = DecisionPlan.model_validate(record["plan"])
            if source == "simulation":
                result = record["result"] or {}
                sim = result.get("simulation")
                failure = result.get("simulation_failure")
                if sim:
                    slot = sim["slot"]
                    label = ResourceLabel(
                        success=True,
                        compute_units=sim["units_consumed"],
                        loaded_accounts_bytes=sim["loaded_accounts_bytes"],
                    )
                elif failure and failure.get("slot") is not None:
                    slot = failure["slot"]
                    label = ResourceLabel(
                        success=failure.get("success", False),
                        error=failure.get("error"),
                        compute_units=failure.get("compute_units"),
                        loaded_accounts_bytes=failure.get("loaded_accounts_bytes"),
                    )
                else:
                    continue
            else:
                final = [
                    row for row in record["execution_outcomes"] if row.get("status") == "finalized"
                ]
                if not final:
                    continue
                # One request contributes at most one execution label, even after retries.
                # A retried request can have failed attempts before succeeding.
                # Retain every event in export, but contribute at most one label.
                outcome = next(
                    (item for item in final if item.get("label", {}).get("success") is True),
                    final[0],
                )
                slot, label = outcome["slot"], ResourceLabel.model_validate(outcome["label"])
            yield Observation(
                record_id=plan.observation_id,
                slot=slot,
                context=plan.context.context,
                source="synthetic" if evidence_origin == "synthetic" else source,
                label_source=source,
                evidence_origin=cast(
                    Literal["synthetic", "local-runtime", "live-simulation"], evidence_origin
                ),
                features=plan.features,
                label=label,
            )

    def attach_signature(
        self,
        identifier: str,
        signed_wire_base64: str,
        *,
        commitment: str = "finalized",
        allow_blockhash_refresh: bool = False,
    ) -> str:
        if commitment not in {"confirmed", "finalized"}:
            raise ValueError("execution commitment must be confirmed or finalized")
        record = self.get(identifier)
        result = record["result"]
        if not result or not result.get("unsigned_transaction_base64"):
            raise ValueError("observation has no resolved final message")
        if record["plan"] is None:
            raise ValueError("final message has no frozen decision")
        plan = DecisionPlan.model_validate(record["plan"])
        rebound = bind_message(
            plan.prepared_wire_base64,
            current_slot=plan.context.current_slot,
            lookups={table.address: table for table in plan.lookup_evidence},
            max_lookup_age_slots=plan.context.max_lookup_age_slots,
        )
        if (
            rebound.prepared_identity != plan.prepared_identity
            or rebound.original_identity != plan.prepared_identity
            or rebound.features != plan.features
        ):
            raise ValueError("stored plan is not bound to the prepared message")
        final = replace_resources(
            decode_wire(plan.prepared_wire_base64).message,
            result.get("compute_unit_limit"),
            result.get("loaded_accounts_data_size_limit"),
        )
        if message_identity(final) != result.get("final_identity"):
            raise ValueError("stored final identity differs from the controlled resource result")
        verify_final_message(unsigned_wire(final), result["unsigned_transaction_base64"])
        verify_final_message(
            result["unsigned_transaction_base64"],
            signed_wire_base64,
            allow_blockhash_refresh=allow_blockhash_refresh,
        )
        signed = decode_wire(signed_wire_base64)
        if not signed.verify_with_results() or not all(signed.verify_with_results()):
            raise ValueError("invalid signatures for the final message")
        signature = str(signed.signatures[0])
        with self.db:
            changed = self.db.execute(
                "INSERT OR IGNORE INTO signatures VALUES(?,?,?,?)",
                (signature, identifier, signed_wire_base64, commitment),
            ).rowcount
        row = self.db.execute("SELECT * FROM signatures WHERE signature=?", (signature,)).fetchone()
        if (
            row["observation_id"] != identifier
            or row["wire"] != signed_wire_base64
            or row["commitment"] != commitment
        ):
            self._conflict(identifier, "signature", signed_wire_base64)
        if changed:
            with self.db:
                self.db.execute(
                    "UPDATE observations SET outcome='pending' WHERE id=? "
                    "AND outcome NOT IN ('confirmed','finalized')",
                    (identifier,),
                )
        return signature

    def reconcile(
        self,
        signature: str,
        result: dict[str, Any] | None,
        *,
        commitment: str,
        registry: ProfileRegistry | None = None,
    ) -> str:
        """Ingest caller/RPC execution metadata, never re-simulate historical state."""
        row = self.db.execute("SELECT * FROM signatures WHERE signature=?", (signature,)).fetchone()
        if row is None:
            raise ValueError("attach signed final message before reconciliation")
        if commitment not in {"confirmed", "finalized"}:
            raise ValueError("unsupported reconciliation commitment")
        body: dict[str, Any] = {
            "signature": signature,
            "commitment": commitment,
            "status": "unavailable" if result is None else commitment,
        }
        if result is not None:
            transaction = result.get("transaction")
            if (
                not isinstance(transaction, list)
                or len(transaction) != 2
                or transaction[1] != "base64"
            ):
                raise ValueError("reconciliation requires actual base64 transaction bytes")
            wire = transaction[0]
            verify_final_message(row["wire"], wire)
            signed = decode_wire(wire)
            if str(signed.signatures[0]) != signature or not all(signed.verify_with_results()):
                raise ValueError("execution signature does not match message")
            meta = result.get("meta")
            if not isinstance(meta, dict) or "err" not in meta:
                raise ValueError("execution metadata missing error status")
            slot = result.get("slot")
            if type(slot) is not int or slot < 0:
                raise ValueError("invalid execution slot")
            label = ResourceLabel(
                success=meta["err"] is None,
                error=None if meta["err"] is None else "transaction_error",
                compute_units=meta.get("computeUnitsConsumed"),
                loaded_accounts_bytes=meta.get("loadedAccountsDataSize"),
            )
            body.update(slot=slot, label=label.model_dump(), source="historical-execution")
        previous = self.db.execute(
            "SELECT body FROM outcomes WHERE signature=? ORDER BY id DESC LIMIT 1", (signature,)
        ).fetchone()
        if previous:
            old = json.loads(previous[0])
            if old.get("status") == "finalized" and old != body:
                self._conflict(row["observation_id"], "finalized outcome", _json(body))
        encoded = _json(body)
        if registry is not None and result is not None:
            observation = self.get(row["observation_id"])
            plan = DecisionPlan.model_validate(observation["plan"])
            prediction = plan.prediction
            if (
                plan.profile_id is not None
                and plan.profile_revision is not None
                and not prediction.simulation_recommended
                and plan.eligibility_reason == prediction.reason
                and prediction.compute_unit_limit is not None
                and prediction.loaded_accounts_data_size_limit is not None
            ):
                # Each verified outcome revision is idempotent. Confirmed outcomes
                # may change; all revisions remain in the event log and quarantine
                # evidence is not undone by a later reorg or successful retry.
                registry.record_execution(
                    plan.profile_id,
                    plan.profile_revision,
                    plan.observation_id + ":" + hashlib.sha256(encoded.encode()).hexdigest(),
                    success=label.success,
                    compute_units=label.compute_units,
                    loaded_accounts_bytes=label.loaded_accounts_bytes,
                    compute_unit_limit=prediction.compute_unit_limit,
                    loaded_accounts_data_size_limit=prediction.loaded_accounts_data_size_limit,
                    current_slot=cast(int, slot),
                )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO outcomes VALUES(NULL,?,?,?,?)",
                (signature, encoded, hashlib.sha256(encoded.encode()).hexdigest(), time.time()),
            )
            status = body["status"]
            if (
                commitment == "confirmed"
                and row["commitment"] == "finalized"
                and status != "unavailable"
            ):
                status = "pending_finality"
            self.db.execute(
                "UPDATE observations SET outcome=? WHERE id=?", (status, row["observation_id"])
            )
        return str(status)


def collect_shadow(
    requests: Iterable[ShadowRequest],
    *,
    store: ObservationStore,
    rpc: RpcClient,
    estimator: ResourceEstimator | None = None,
    registry: ProfileRegistry | None = None,
    profile_id: str | None = None,
    stream: str = "default",
    max_records: int = 1000,
    cancellation: threading.Event | None = None,
) -> dict[str, int]:
    if type(max_records) is not int or not 1 <= max_records <= 100_000:
        raise ValueError("collection bound must be between 1 and 100000")

    def audit_control(result: dict[str, Any]) -> None:
        if registry is None or result.get("plan") is None:
            return
        frozen = DecisionPlan.model_validate(result["plan"])
        if not frozen.control_selected:
            return
        simulation = result.get("simulation")
        registry.record_control(
            frozen.observation_id,
            success=simulation is not None,
            compute_units=simulation["units_consumed"] if simulation else None,
            loaded_accounts_bytes=simulation["loaded_accounts_bytes"] if simulation else None,
            current_slot=simulation["slot"] if simulation else frozen.context.current_slot,
            elapsed_ms=simulation["elapsed_ms"] if simulation else result["full_preparation_ms"],
        )

    completed = unresolved = resumed = 0
    # Re-read to detect conflicting IDs; reordering is safe because deduplication
    # uses request identity rather than trusting an old positional checkpoint.
    for cursor, request in enumerate(requests, 1):
        if completed >= max_records or (cancellation is not None and cancellation.is_set()):
            break
        collection_started = time.perf_counter()
        rpc_attempts_before = rpc.call_count
        store.ingest(request.observation_id, request.model_dump(mode="json"))
        prior = store.get(request.observation_id)
        if prior["phase"] == "complete":
            store.finish(request.observation_id, prior["result"], stream=stream, cursor=cursor)
            audit_control(prior["result"])
            resumed += 1
            continue
        try:
            if prior["plan"] is None:
                plan = prepare_decision(
                    request.wire_base64,
                    context=request.context,
                    rpc=rpc,
                    estimator=estimator,
                    registry=registry,
                    profile_id=profile_id,
                    observation_id=request.observation_id,
                    lookups={table.address: table for table in request.lookups},
                )
                store.freeze_plan(request.observation_id, plan.model_dump(mode="json"))
            else:
                plan = DecisionPlan.model_validate(prior["plan"])
            decision = execute_decision(plan, rpc=rpc, shadow=True)
            result = decision.model_dump(mode="json")
        except RecordConflict:
            raise
        except (ValueError, RpcError):
            result = ResourceDecision(
                observation_id=request.observation_id,
                status="unresolved",
                reason="invalid_message_or_preexecution_evidence",
                full_preparation_ms=0,
                shadow=True,
            ).model_dump(mode="json")
        store.finish(request.observation_id, result, stream=stream, cursor=cursor)
        # The primary label commits first. A crash during registry auditing can
        # replay this exact frozen outcome without resimulation or double counting.
        audit_control(result)
        # Includes input/plan writes, preparation, simulation and atomic result+
        # checkpoint durability. Telemetry's own final write is outside the span.
        elapsed = (time.perf_counter() - collection_started) * 1000
        plan_body = result.get("plan") or {}
        store.record_preparation(
            PreparationTrace(
                observation_id=request.observation_id,
                evidence=request.evidence_origin,
                mode="shadow",
                collection_method=request.collection_method,
                total_ms=elapsed,
                resource_estimation_calls=result.get("resource_simulation_calls", 0),
                rpc_attempts=rpc.call_count - rpc_attempts_before,
                state_reads=plan_body.get("state_reads", 0),
            )
        )
        completed += 1
        unresolved += result["status"] == "unresolved"
    return {
        "completed": completed,
        "unresolved": unresolved,
        "deduplicated": resumed,
        "checkpoint": store.checkpoint(stream),
        "avoided_calls": 0,
    }
