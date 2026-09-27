"""Local payout application. Payment semantics and execution stay outside cu_pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from cu_pilot.binding import (
    bind_message,
    decode_wire,
    message_identity,
    replace_resources,
    unsigned_wire,
)
from cu_pilot.integration import (
    DecisionPlan,
    EstimationContext,
    ResourceDecision,
    execute_decision,
    prepare_decision,
    record_control_outcome,
)
from cu_pilot.lifecycle import ProfileRegistry, refresh_deployments
from cu_pilot.rpc import RpcClient
from cu_pilot.schemas import Observation, ResourceLabel
from cu_pilot.shadow import ObservationStore

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM = "11111111111111111111111111111111"
BUDGET = "ComputeBudget111111111111111111111111111111"
CU_CAP = 100_000
DATA_CAP = 1_048_576
MAX_ATTEMPTS = 2
SOL_ALLOWANCE = 50_000_000
MENU = (1, 2, 4, 8)
METHODS = (
    "learned",
    "adaptive",
    "always_simulate",
    "fixed_batch",
    "scoped_fixed_batch",
    "formula",
    "pattern_p99",
    "cache",
    "fixed_estimate_ablation",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def confirmed_budget_failure(transaction: dict[str, Any], compute_unit_limit: int) -> str | None:
    """Recognize explicit runtime budget failure; never infer uncensored demand."""
    meta = transaction["meta"]
    error = meta["err"]
    if error == "MaxLoadedAccountsDataSizeExceeded":
        return "loaded_data"
    instruction_error = error.get("InstructionError") if isinstance(error, dict) else None
    if not isinstance(instruction_error, list) or len(instruction_error) != 2:
        return None
    if instruction_error[1] == "ComputationalBudgetExceeded":
        return "compute"
    if (
        instruction_error[1] == "ProgramFailedToComplete"
        and type(compute_unit_limit) is int
        and compute_unit_limit > 0
        and type(meta.get("computeUnitsConsumed")) is int
        and meta.get("computeUnitsConsumed") == compute_unit_limit
        and any(
            isinstance(line, str)
            and re.fullmatch(
                r"Program [1-9A-HJ-NP-Za-km-z]{32,44} failed: "
                r"exceeded CUs meter at BPF instruction",
                line,
            )
            for line in (meta.get("logMessages") or [])
        )
    ):
        return "compute"
    return None


class Bridge:
    def __init__(self, path: Path):
        config = json.loads(path.read_text())
        if not config["url"].startswith("http://127.0.0.1:"):
            raise ValueError("only the isolated loopback runtime is supported")
        self.url = config["url"]
        self.client = httpx.Client(
            headers={"Authorization": "Bearer " + config["token"]}, timeout=45
        )
        self.rpc_calls = 0
        self.transport_ms = 0.0
        self.method_counts = Counter()
        self.transport_failures = 0

    def call(self, action: str, **kwargs: Any) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = self.client.post(self.url, json={"action": action, **kwargs})
        except httpx.HTTPError:
            self.transport_failures += 1
            raise
        self.transport_ms += (time.perf_counter() - started) * 1000
        result = response.json()
        self.rpc_calls += result.get("bridge_rpc_calls", 0)
        self.method_counts.update(result.get("bridge_rpc_methods", {}))
        if response.status_code != 200:
            raise RuntimeError(result.get("error", "local runtime failure"))
        return result


class MeasuredRpc(RpcClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.method_counts = Counter()

    def _call(self, method, params):
        before = self.call_count
        try:
            return super()._call(method, params)
        finally:
            self.method_counts[method] += self.call_count - before

    def get_slot(self):
        # Current bank is the freshness clock, not a claim of confirmation.
        # Account reads, simulations and execution outcomes remain confirmed.
        slot = self._call("getSlot", [{"commitment": "processed"}])
        if type(slot) is not int or not 0 <= slot < 2**64:
            raise ValueError("invalid current slot")
        return slot


class Application:
    def __init__(self, directory: Path, *, compute_unit_cap: int | None = None):
        if compute_unit_cap is not None and (
            type(compute_unit_cap) is not int or not 1 <= compute_unit_cap <= 1_400_000
        ):
            raise ValueError("compute unit cap must be an integer from 1 through 1400000")
        self._compute_unit_cap = compute_unit_cap
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.bridge = Bridge(directory / "runtime.json")
        self.info = self.bridge.call("info")
        self.cluster = "isolated-surfpool:" + self.info["instance_id"]
        self.runtime = "surfpool-1.5.0:default:" + canonical(self.info["runtime"])
        self.context = "payout-queue-v1:" + self.info["instance_id"]
        self.rpc = MeasuredRpc(
            self.info["rpc_url"], timeout=15, attempts=2, requests_per_second=200
        )
        self.registry = ProfileRegistry(directory / "profiles.sqlite")
        self.store = ObservationStore(directory / "observations.sqlite")
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS payout_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS payout_queues(address TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS payout_steps(
                id TEXT PRIMARY KEY,queue TEXT NOT NULL,body TEXT NOT NULL,
                phase TEXT NOT NULL,signature TEXT,wire TEXT,outcome TEXT);
            CREATE TABLE IF NOT EXISTS payout_planning_attempts(
                id TEXT PRIMARY KEY,queue TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL);
        """)
        old = self.setting("instance")
        if old is not None and old != self.info["instance_id"]:
            raise RuntimeError("runtime changed; archive/reset this isolated application's state")
        self.set_setting("instance", self.info["instance_id"])
        self.deployment_bindings: dict[str, str] = {}
        self.watcher_at = 0.0
        self.watcher_slot = 0
        self.bundle = None
        baseline_path = directory / "baselines.json"
        self.baselines = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
        bundle_path = directory / "candidate.json"
        if bundle_path.exists():
            from examples.payouts.model import PayoutModelBundle

            self.bundle = PayoutModelBundle.load(bundle_path)

    @property
    def compute_unit_cap(self) -> int:
        configured = getattr(self, "_compute_unit_cap", None)
        return CU_CAP if configured is None else configured

    @property
    def loaded_data_cap(self) -> int:
        return DATA_CAP

    def setting(self, key: str) -> Any:
        row = self.store.db.execute(
            "SELECT value FROM payout_settings WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO payout_settings VALUES(?,?)", (key, canonical(value))
            )

    def refresh(self, slot: int, force: bool = False) -> None:
        if force or slot - self.watcher_slot >= 40 or time.monotonic() - self.watcher_at >= 20:
            for attempt in range(3):
                try:
                    evidence = refresh_deployments(
                        self.registry,
                        self.rpc,
                        [self.info["program"], TOKEN, ATA, SYSTEM, BUDGET],
                        current_slot=slot,
                        cluster_identity=self.cluster,
                        runtime_identity=self.runtime,
                        commitment="confirmed",
                    )
                    break
                except ValueError:
                    if attempt == 2:
                        raise
            self.deployment_bindings = {e.program_id: e.fingerprint for e in evidence}
            self.evidence = evidence
            self.watcher_slot = max(e.observed_slot for e in evidence)
            self.watcher_at = time.monotonic()

    def estimation_context(self, slot: int) -> EstimationContext:
        return EstimationContext(
            context=self.context,
            current_slot=slot,
            cluster_identity=self.cluster,
            runtime_identity=self.runtime,
            workload="payout-queue-v1",
            budget_independent=True,
        )

    def executor_balance(self) -> int:
        # Explicit confirmed commitment also works with an already running bridge.
        value = self.rpc._call("getBalance", [self.info["executor"], {"commitment": "confirmed"}])[
            "value"
        ]
        if type(value) is not int or value < 0:
            raise ValueError("invalid executor balance")
        return value

    def audit_budget_failure(self, body: dict[str, Any], transaction: dict[str, Any]) -> bool:
        """A budget-exhaustion error proves failure, not uncensored resource demand."""
        exhausted = confirmed_budget_failure(transaction, body["decision"]["compute_unit_limit"])
        if not exhausted:
            return False
        plan = body["decision"]["plan"]
        if exhausted and plan.get("profile_id") and plan.get("profile_revision") is not None:
            self.registry.suspend(
                plan["profile_id"],
                plan["profile_revision"],
                actor="payout-execution-reconciler",
                reason="confirmed_resource_budget_exhaustion",
                current_slot=transaction["slot"],
            )
        invalidation_key = body.get("baseline_invalidation_key")
        if invalidation_key:
            invalidated = self.setting("fitted_baseline_invalidations") or {}
            invalidated.setdefault(
                invalidation_key,
                {
                    "method": body["method"],
                    "count": body["chosen_count"],
                    "decision_id": body["id"],
                    "slot": transaction["slot"],
                    "reason": "confirmed_resource_budget_exhaustion",
                    "resource": exhausted,
                },
            )
            self.set_setting("fitted_baseline_invalidations", invalidated)
        return True

    def envelope(self, candidate: dict[str, Any]):
        from examples.payouts.model import PayoutStateEnvelope, RecipientAccountState

        q = candidate["queue"]
        count = candidate["count"]
        accounts = []
        for i, state in enumerate(candidate["ata_states"]):
            evidence = candidate["evidence"][3 + count + i]
            recipient = q["payments"][q["cursor"] + i]["recipient"]
            accounts.append(
                RecipientAccountState(
                    address=evidence["address"],
                    recipient=recipient,
                    exists=state != "missing",
                    data_bytes=evidence["size"],
                    initialized=state == "initialized",
                    frozen=state == "unsupported",
                    token_mint=q["mint"] if state == "initialized" else None,
                    token_authority=recipient if state == "initialized" else None,
                    program_owner=evidence["owner"]
                    if state != "missing" or evidence["lamports"] != 0
                    else None,
                )
            )
        return PayoutStateEnvelope.seal(
            queue_address=q["address"],
            queue_identity=q["queue_id"],
            mint=q["mint"],
            executor=q["executor"],
            cursor=q["cursor"],
            remaining=q["length"] - q["cursor"],
            candidate_count=count,
            queue_data_bytes=candidate["account_sizes"][0],
            vault_data_bytes=candidate["account_sizes"][1],
            mint_data_bytes=candidate["account_sizes"][2],
            vault_initialized=candidate["vault_initialized"],
            vault_frozen=candidate["vault_frozen"],
            mint_initialized=candidate["mint_initialized"],
            recipient_accounts=accounts,
            deployment_bindings=self.deployment_bindings,
            cluster_identity=self.cluster,
            runtime_identity=self.runtime,
            observation_slot=candidate["slot"],
            max_age_slots=8,
            prepared_identity=bind_message(
                candidate["wire"], current_slot=candidate["slot"]
            ).prepared_identity,
            approved_payments_digest=sha(q["payments"]),
            paused=q["paused"],
            terminal=q["status"] != 0,
        )

    def create(
        self,
        *,
        length: int = 16,
        existing: int = 0,
        identifier: str | None = None,
        payments=None,
        recipient_seed=None,
    ):
        identifier = identifier or uuid.uuid4().hex + uuid.uuid4().hex
        args = {"length": length, "existing": existing, "id": identifier}
        if recipient_seed is not None:
            args["recipient_seed"] = recipient_seed
        if payments is not None:
            args["payments"] = payments
            args["length"] = len(payments)
        result = self.bridge.call("create", **args)
        address = result["queue"]["address"]
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO payout_queues VALUES(?,?)", (address, canonical(result))
            )
        return result

    def collect(self, groups: int = 80) -> dict[str, Any]:
        """Resume without turning retried requests into independent labels."""
        from examples.payouts.model import PayoutObservation

        if groups < 1 or groups > 80:
            raise ValueError("collection is bounded to 80 groups")
        schedule = self.setting("collection_schedule") or {}
        completed = 0
        start = time.perf_counter()
        for group in range(groups):
            for count in range(1, 9):
                key = f"{group}:{count}"
                if key not in schedule:
                    queue_id = sha([self.info["instance_id"], "collection", group, count])
                    length = (
                        count + 1
                        if group % 3 == 2
                        else 16
                        if group % 3 == 0 and count in MENU
                        else count
                    )
                    q = self.create(
                        length=length,
                        existing=1 if group % 3 == 2 else 0,
                        identifier=queue_id,
                        recipient_seed=f"payout-collection-v1:1729:{group}:{count}",
                    )
                    schedule[key] = q["queue"]["address"]
                    self.set_setting("collection_schedule", schedule)
                queue = schedule[key]
                cursor = self.bridge.call("verify", queue=queue)["queue"]["cursor"]
                if group % 3 == 2 and cursor == 0:
                    self.step(queue, "always_simulate", count_cap=1)
                    cursor = self.bridge.call("verify", queue=queue)["queue"]["cursor"]
                    if cursor != 1:
                        raise RuntimeError("collection prefix did not reconcile")
                for existing in range(count + 1):
                    record_id = f"collect:{group}:{count}:{existing}"
                    row = self.store.db.execute(
                        "SELECT phase FROM observations WHERE id=?", (record_id,)
                    ).fetchone()
                    if row and row[0] == "complete":
                        completed += 1
                        continue
                    if existing:
                        self.bridge.call("create_ata", queue=queue, index=cursor + existing - 1)
                    candidate = self.bridge.call(
                        "candidate",
                        queue=queue,
                        count=count,
                        decision=sha(record_id),
                        model="0" * 64,
                    )
                    self.refresh(candidate["slot"])
                    state = self.envelope(candidate)
                    if not row:
                        self.store.ingest(
                            record_id,
                            {
                                "state": state.model_dump(mode="json"),
                                "queue_group": f"collection:{group}",
                                "wire": candidate["wire"],
                            },
                        )
                        plan = prepare_decision(
                            candidate["wire"],
                            context=self.estimation_context(candidate["slot"]),
                            observation_id=record_id,
                            registry=self.registry,
                        )
                        self.store.freeze_plan(record_id, plan.model_dump(mode="json"))
                    else:
                        stored = self.store.get(record_id)
                        if stored["plan"] is None:
                            original = stored["input"]
                            plan = prepare_decision(
                                original["wire"],
                                context=self.estimation_context(
                                    int(original["state"]["observation_slot"])
                                ),
                                observation_id=record_id,
                                registry=self.registry,
                            )
                            self.store.freeze_plan(record_id, plan.model_dump(mode="json"))
                        else:
                            plan = DecisionPlan.model_validate(stored["plan"])
                    result = execute_decision(plan, rpc=self.rpc, shadow=True)
                    self.store.finish(
                        record_id,
                        result.model_dump(mode="json"),
                        stream="payout-collection",
                        cursor=completed + 1,
                    )
                    completed += 1
            print(
                f"Collection group {group + 1}/{groups}: {completed} durable observations",
                flush=True,
            )
        rows = []
        for record in self.store.export_records():
            if not record["id"].startswith("collect:") or not record["result"]:
                continue
            plan = DecisionPlan.model_validate(record["plan"])
            result = ResourceDecision.model_validate(record["result"])
            sim = result.simulation
            observation = Observation(
                record_id=record["id"],
                slot=sim.slot if sim else plan.context.current_slot,
                context=self.context,
                source="simulation",
                label_source="simulation",
                evidence_origin="local-runtime",
                collection_method="prospective",
                collection_mode="shadow",
                features=plan.features,
                label=ResourceLabel(
                    success=sim is not None,
                    compute_units=sim.units_consumed if sim else None,
                    loaded_accounts_bytes=sim.loaded_accounts_bytes if sim else None,
                    error=None if sim else result.reason,
                ),
            )
            rows.append(
                PayoutObservation(
                    record_id=record["id"],
                    queue_group=record["input"]["queue_group"],
                    state=record["input"]["state"],
                    observation=observation,
                )
            )
        path = self.directory / "observations.jsonl"
        path.write_text("".join(row.model_dump_json() + "\n" for row in rows))
        (self.directory / "collection-provenance.json").write_text(
            json.dumps(
                {
                    "schedule_version": "payout-collection-v1",
                    "recipient_seed": 1729,
                    "groups": groups,
                    "counts": list(range(1, 9)),
                    "ata_states": "every missing count from count down to zero",
                    "runtime": self.info,
                    "runtime_configuration": {
                        "offline": True,
                        "block_production_mode": "transaction",
                        "initial_slot": 100,
                        "feature_overrides": [],
                        "account_and_simulation_commitment": "confirmed",
                    },
                    "deployment_bindings": rows[0].state.deployment_bindings,
                    "collected_deployment_binding_sets": [
                        json.loads(binding)
                        for binding in sorted(
                            {canonical(row.state.deployment_bindings) for row in rows}
                        )
                    ],
                    "label_source": "simulation",
                    "evidence_origin": "local-runtime",
                    "collection_mode": "prospective shadow",
                    "labels": len(rows),
                    "queue_schedule": "group%3: nonterminal prefixes, terminal tails, "
                    "then cursor-one tails after actual warmup execution",
                    "note": "Mint, owner/executor and queue identities are ephemeral. "
                    "Public recipients use the recorded fixture seed; PDA costs/timings vary.",
                },
                indent=2,
            )
            + "\n"
        )
        return {"records": len(rows), "seconds": time.perf_counter() - start, "path": str(path)}

    def qualify(self):
        from examples.payouts.model import qualify_bundle

        if self.bundle is None:
            raise ValueError("train a candidate first")
        slot = self.rpc.get_slot()
        self.refresh(slot, force=True)
        result = qualify_bundle(
            self.bundle,
            self.registry,
            current_slot=max(slot, self.watcher_slot),
            deployments=self.evidence,
            dependencies={
                self.info["program"]: (TOKEN, ATA, SYSTEM),
                ATA: (TOKEN, SYSTEM),
                TOKEN: (),
                SYSTEM: (),
                BUDGET: (),
            },
        )
        (self.directory / "qualification.json").write_text(json.dumps(result, indent=2) + "\n")
        return result

    def _store_step(self, identifier: str, queue: str, body: dict[str, Any]) -> None:
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO payout_steps VALUES(?,?,?,'planned',NULL,NULL,NULL)",
                (identifier, queue, canonical(body)),
            )

    def reconcile_pending(self, queue: str) -> dict[str, Any] | None:
        row = self.store.db.execute(
            "SELECT * FROM payout_steps WHERE queue=? AND phase IN ('signed','uncertain') "
            "ORDER BY rowid LIMIT 1",
            (queue,),
        ).fetchone()
        if row is None:
            return None
        evidence = self.bridge.call("reconcile", queue=queue, signature=row["signature"])
        self.store.reconcile(
            row["signature"],
            evidence["transaction"],
            commitment="confirmed",
            registry=self.registry,
        )
        if evidence["transaction"] is None:
            body = json.loads(row["body"])
            attempts = self.setting("rebroadcast:" + row["id"]) or 0
            if (
                evidence["verification"]["queue"]["cursor"] == body["cursor"]
                and attempts < MAX_ATTEMPTS
            ):
                # Re-send identical signed bytes only, after checking signature AND queue.
                # This cannot change count/recipients and is safe under runtime signature dedup.
                self.set_setting("rebroadcast:" + row["id"], attempts + 1)
                try:
                    self.bridge.call("send", wire=row["wire"])
                except (httpx.HTTPError, RuntimeError):
                    pass
                evidence = self.bridge.call("reconcile", queue=queue, signature=row["signature"])
                self.store.reconcile(
                    row["signature"],
                    evidence["transaction"],
                    commitment="confirmed",
                    registry=self.registry,
                )
        if evidence["transaction"] is None:
            # An unchanged cursor does not disprove an in-flight submission.
            with self.store.db:
                self.store.db.execute(
                    "UPDATE payout_steps SET phase='uncertain' WHERE id=?", (row["id"],)
                )
            raise RuntimeError(
                "uncertain signature; worker paused until reconciliation finds the outcome"
            )
        body = json.loads(row["body"])
        verification = evidence["verification"]
        resource_exhausted = self.audit_budget_failure(body, evidence["transaction"])
        if resource_exhausted:
            self.set_setting("force_simulation:" + queue, True)
        if evidence["transaction"]["meta"]["err"] is not None:
            phase = "failed"
        else:
            phase = "confirmed"
            if (
                not verification["correct"]
                or verification["queue"]["cursor"] < body["cursor"] + body["chosen_count"]
            ):
                raise RuntimeError(
                    "confirmed transaction did not reconcile to approved balances/cursor"
                )
        meta = evidence["transaction"]["meta"]
        fee = meta.get("fee")
        debit = (
            (meta["preBalances"][0] - meta["postBalances"][0])
            if meta.get("preBalances") and meta.get("postBalances")
            else None
        )
        outcome = {
            **evidence,
            "recovered": True,
            "recorded_rebroadcasts": self.setting("rebroadcast:" + row["id"]) or 0,
            "signature": row["signature"],
            "success": phase == "confirmed",
            "confirmed_resource_exhaustion": resource_exhausted,
            "compute_units": meta.get("computeUnitsConsumed"),
            "loaded_accounts_bytes": meta.get("loadedAccountsDataSize"),
            "fee_lamports": fee,
            "rent_deposit_lamports": debit - fee if debit is not None and fee is not None else None,
            "preparation_ms": None,
            "complete_step_ms": None,
            "local_transport_ms": None,
            "rpc_calls": None,
            "rpc_retries": None,
            "state_deployment_reads": None,
            "rpc_method_counts": None,
            "telemetry_note": "Original in-flight timings/counters unavailable after interruption",
        }
        with self.store.db:
            self.store.db.execute(
                "UPDATE payout_steps SET phase=?,outcome=? WHERE id=?",
                (phase, canonical(outcome), row["id"]),
            )
        return {**body, **outcome}

    def _baseline_result(
        self, plan: DecisionPlan, compute: int, data: int, reason: str
    ) -> ResourceDecision:
        final = replace_resources(decode_wire(plan.prepared_wire_base64).message, compute, data)
        return ResourceDecision(
            observation_id=plan.observation_id,
            status="accepted_prediction",
            reason=reason,
            plan=plan,
            compute_unit_limit=compute,
            loaded_accounts_data_size_limit=data,
            final_identity=message_identity(final),
            unsigned_transaction_base64=unsigned_wire(final),
            full_preparation_ms=plan.preparation_ms,
        )

    def _adaptive_selection(self, queue, identifier, options, plans, candidates):
        """Largest verified prefix, with one bounded decision per exact candidate.

        A successful over-cap measurement permits shrinking. An unknown or failed
        simulation never proves that ignoring part of an approved prefix is safe.
        """
        if (
            not options
            or len(options) > len(MENU)
            or [o["count"] for o in options] != sorted({o["count"] for o in options}, reverse=True)
        ):
            raise ValueError("adaptive selection requires a bounded descending candidate menu")
        started = time.perf_counter()
        calls_before = self.rpc.call_count + self.bridge.rpc_calls
        methods_before = self.rpc.method_counts + self.bridge.method_counts
        retries_before = self.rpc.retry_count
        audit = {
            "policy": "largest-verified-prefix-v1",
            "objective": "largest feasible approved prefix, not a universal latency/cost optimum",
            "max_simulation_probes": len(MENU),
            "compute_unit_cap": self.compute_unit_cap,
            "loaded_data_cap": self.loaded_data_cap,
            "candidate_counts": [o["count"] for o in options],
            "probes": [],
            "selected_count": None,
            "limit_source": None,
            "estimation_simulations": 0,
            "control_simulations": 0,
        }

        def persist(status):
            audit["selection_ms"] = (time.perf_counter() - started) * 1000
            audit["rpc_calls"] = self.rpc.call_count + self.bridge.rpc_calls - calls_before
            audit["rpc_method_counts"] = dict(
                self.rpc.method_counts + self.bridge.method_counts - methods_before
            )
            audit["rpc_retries"] = self.rpc.retry_count - retries_before
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO payout_planning_attempts VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET body=excluded.body,status=excluded.status",
                    (identifier, queue, canonical(audit), status),
                )

        persist("planning")
        try:
            for option in options:
                plan = plans[option["count"]]
                probe = {
                    "count": option["count"],
                    "observation_id": plan.observation_id,
                    "prepared_identity": plan.prepared_identity,
                    "prediction_reason": option["reason"],
                }
                audit["probes"].append(probe)
                if not candidates[option["count"]]["supported"]:
                    probe["reason"] = "unsupported_account_state"
                    raise RuntimeError("adaptive planning stopped: unsupported account state")
                structural = (
                    "unsupported_transaction_version"
                    if option["version"] != "legacy"
                    else "transaction_too_large"
                    if option["serialized_size"] > 1232
                    else "too_many_accounts"
                    if option["account_count"] > 64
                    else None
                )
                if structural:
                    probe.update(status="rejected", reason=structural)
                    persist("planning")
                    continue
                use_prediction = bool(
                    option["eligible"]
                    and option["limits"]
                    and option["limits"][0] <= self.compute_unit_cap
                    and option["limits"][1] <= self.loaded_data_cap
                )
                probe["requested_source"] = (
                    "qualified_learned_prediction" if use_prediction else "fresh_simulation"
                )
                persist("planning")
                trial = execute_decision(
                    plan,
                    rpc=self.rpc,
                    registry=self.registry,
                    force_simulation=not use_prediction,
                    current_slot=self.rpc.get_slot(),
                )
                control_calls = int(plan.control_selected and trial.resource_simulation_calls > 0)
                audit["control_simulations"] += control_calls
                audit["estimation_simulations"] += trial.resource_simulation_calls - control_calls
                probe.update(
                    status=trial.status,
                    reason=trial.reason,
                    compute_unit_limit=trial.compute_unit_limit,
                    loaded_accounts_data_size_limit=trial.loaded_accounts_data_size_limit,
                    resource_simulation_calls=trial.resource_simulation_calls,
                    control_simulations=control_calls,
                    simulation_failure=trial.simulation_failure,
                    final_identity=trial.final_identity,
                )
                self.store.finish(
                    plan.observation_id,
                    trial.model_dump(mode="json"),
                    stream="payout-execution",
                    cursor=0,
                )
                record_control_outcome(trial, self.registry)
                if trial.status == "unresolved":
                    failure = trial.simulation_failure or {}
                    # The RPC adapter emits this code only after a successful
                    # simulation whose padded resource demand exceeds runtime limits.
                    if (
                        trial.reason == "resource_cap_exceeded"
                        and failure.get("success") is True
                        and type(failure.get("compute_units")) is int
                        and type(failure.get("loaded_accounts_bytes")) is int
                    ):
                        probe["rejection"] = "measured_resource_cap"
                        persist("planning")
                        continue
                    raise RuntimeError("adaptive planning stopped: " + trial.reason)
                if (
                    trial.compute_unit_limit is None
                    or trial.loaded_accounts_data_size_limit is None
                ):
                    raise RuntimeError("adaptive planning stopped: missing paired limits")
                if trial.compute_unit_limit > self.compute_unit_cap or (
                    trial.loaded_accounts_data_size_limit > self.loaded_data_cap
                ):
                    if trial.status != "simulation_success" or trial.simulation is None:
                        raise RuntimeError("adaptive revalidated prediction exceeds policy; replan")
                    probe["rejection"] = (
                        "compute_policy_cap"
                        if trial.compute_unit_limit > self.compute_unit_cap
                        else "loaded_data_policy_cap"
                    )
                    persist("planning")
                    continue
                audit["selected_count"] = option["count"]
                audit["limit_source"] = (
                    "qualified_learned_prediction"
                    if trial.status == "accepted_prediction"
                    else "fresh_simulation"
                )
                audit["reason"] = "largest_verified_feasible_prefix"
                persist("selected")
                return trial, option, audit
            raise RuntimeError("no verified adaptive candidate fits fixed limits; worker paused")
        except BaseException:
            # Do not copy arbitrary provider exception text into the journal.
            audit["reason"] = "selection_stopped_without_submission"
            persist("stopped")
            raise

    def step(
        self,
        queue: str,
        method: str = "learned",
        *,
        interrupt_after_sign: bool = False,
        interrupt_after_send: bool = False,
        count_cap: int | None = None,
    ):
        if method not in METHODS:
            raise ValueError("unknown payout strategy: " + method)
        estimate_method = "fixed_batch" if method == "scoped_fixed_batch" else method
        started = time.perf_counter()
        calls_before = self.rpc.call_count + self.bridge.rpc_calls
        methods_before = self.rpc.method_counts + self.bridge.method_counts
        retries_before = self.rpc.retry_count
        transport_before = self.bridge.transport_ms
        recovered = self.reconcile_pending(queue)
        if recovered:
            return recovered
        verification = self.bridge.call("verify", queue=queue)
        q = verification["queue"]
        if not verification["correct"]:
            raise RuntimeError("approved queue balances changed; worker paused for reconciliation")
        if q["paused"] or q["status"] != 0:
            return {"stopped": True, "verification": verification}
        balance = self.executor_balance()
        initial = self.setting("sol:" + queue)
        if initial is None:
            initial = balance
            self.set_setting("sol:" + queue, initial)
        if initial - balance >= SOL_ALLOWANCE or balance < 20_000_000:
            raise RuntimeError("executor SOL allowance exhausted")
        failures = self.store.db.execute(
            "SELECT body,outcome FROM payout_steps WHERE queue=? AND phase='failed'", (queue,)
        ).fetchall()
        failed = len(failures)
        if failed >= MAX_ATTEMPTS:
            raise RuntimeError("bounded failed-attempt limit reached")
        recovery_simulation = bool(self.setting("force_simulation:" + queue))
        if not recovery_simulation:
            for failure in failures:
                outcome = json.loads(failure["outcome"])
                if self.audit_budget_failure(json.loads(failure["body"]), outcome["transaction"]):
                    recovery_simulation = True
                    self.set_setting("force_simulation:" + queue, True)
                    break
        retry_after_failure = any(
            json.loads(row["body"])["cursor"] == q["cursor"] for row in failures
        )
        remaining = q["length"] - q["cursor"]
        counts = sorted(
            {n for n in MENU if n <= remaining} | ({remaining} if remaining <= 8 else set()),
            reverse=True,
        )
        if count_cap is not None:
            counts = [count for count in counts if count <= count_cap]
        if method == "scoped_fixed_batch" and not self.baselines:
            raise RuntimeError("scoped fixed batch requires a fitted baseline artifact")
        if estimate_method == "fixed_batch" and self.baselines:
            cap = self.baselines.get("fixed_batch_count")
            if cap is None:
                raise RuntimeError("fixed batch has no supported fitting count")
            counts = [count for count in counts if count <= cap]
            if method == "scoped_fixed_batch":
                counts = [count for count in counts if count == min(cap, remaining)]
                if not counts:
                    raise RuntimeError("count constraint excludes the scoped fixed batch")
        self.refresh(q["slot"])
        identifier = uuid.uuid4().hex
        model_digest = self.bundle.digest if self.bundle is not None else "0" * 64
        options = []
        plans = {}
        candidates = {}
        inference_start = time.perf_counter()
        candidate_batch = self.bridge.call(
            "candidates",
            queue=queue,
            counts=counts,
            decisions={str(count): sha([identifier, count]) for count in counts},
            model=model_digest,
        )
        by_count = {candidate["count"]: candidate for candidate in candidate_batch["candidates"]}
        for count in counts:
            candidate = by_count[count]
            # A pre-funded system ATA takes a different creation branch; it has
            # no fitted support even though simulation may safely qualify it.
            if any(
                state == "missing" and evidence["lamports"] != 0
                for state, evidence in zip(
                    candidate["ata_states"], candidate["evidence"][3 + count :], strict=True
                )
            ):
                candidate["supported"] = False
            if candidate["queue"]["cursor"] != q["cursor"]:
                raise RuntimeError("queue changed during candidate generation; replan")
            state = self.envelope(candidate)
            request_id = "execute:" + identifier + ":" + str(count)
            profile, estimator, state_reason = None, None, "no_payout_artifact"
            if self.bundle is not None and candidate["supported"]:
                profile, estimator, state_reason = self.bundle.estimator_for(
                    state,
                    current_slot=candidate["slot"],
                    deployment_bindings=self.deployment_bindings,
                    prepared_identity=state.prepared_identity,
                )
            if method not in {"learned", "adaptive"}:
                profile, estimator = None, None
            plan = prepare_decision(
                candidate["wire"],
                context=self.estimation_context(candidate["slot"]),
                estimator=estimator,
                profile_id=profile,
                observation_id=request_id,
                registry=self.registry,
            )
            limits = None
            baseline_key = None
            baseline_reason = None
            eligible = (
                not plan.prediction.simulation_recommended
                and plan.eligibility_reason == plan.prediction.reason
            )
            if method in {"learned", "adaptive"} and eligible:
                limits = (
                    plan.prediction.compute_unit_limit,
                    plan.prediction.loaded_accounts_data_size_limit,
                )
            elif method == "fixed_estimate_ablation":
                limits = (20_000 + 25_000 * count, self.loaded_data_cap)
            elif estimate_method in {"fixed_batch", "formula", "pattern_p99", "cache"}:
                from examples.payouts.baselines import (
                    baseline_estimate,
                    baseline_invalidation_key,
                    fitted_baseline_risk,
                )

                invalidated = self.setting("fitted_baseline_invalidations") or {}
                limits = baseline_estimate(
                    estimate_method,
                    self.bundle,
                    state,
                    plan.features,
                    current_slot=candidate["slot"],
                    cache=self.setting("estimate_cache") or {},
                    fitted_baselines=self.baselines,
                    registry=self.registry,
                    invalidated_keys=invalidated,
                )
                if estimate_method in {"fixed_batch", "formula"} and self.bundle and self.baselines:
                    baseline_key = baseline_invalidation_key(
                        estimate_method, self.bundle, self.baselines, count
                    )
                    if limits is None:
                        baseline_reason = fitted_baseline_risk(
                            estimate_method,
                            self.bundle,
                            state,
                            plan.features,
                            current_slot=candidate["slot"],
                            fitted_baselines=self.baselines,
                            registry=self.registry,
                            invalidated_keys=invalidated,
                        )
            if not candidate["supported"] or recovery_simulation:
                limits = None
                eligible = False
            option = {
                "count": count,
                "observation_id": request_id,
                "state": state.model_dump(mode="json"),
                "limits": list(limits) if limits else None,
                "eligible": eligible,
                "reason": plan.eligibility_reason
                if eligible
                else state_reason + ":" + plan.eligibility_reason,
                "serialized_size": candidate["serialized_size"],
                "account_count": plan.features.account_count,
                "version": plan.features.version,
                "snapshot_digest": candidate["snapshot_digest"],
            }
            if baseline_key is not None:
                option["baseline_invalidation_key"] = baseline_key
                option["baseline_scope_reason"] = baseline_reason or "scoped_fitting_evidence"
            # Commit the exact prediction before any simulation or outcome.
            self.store.ingest(request_id, {"candidate": option, "method": method})
            self.store.freeze_plan(request_id, plan.model_dump(mode="json"))
            plans[count] = plan
            candidates[count] = candidate
            options.append(option)
        inference_ms = (time.perf_counter() - inference_start) * 1000
        feasible = [
            o
            for o in options
            if o["limits"]
            and o["limits"][0] <= self.compute_unit_cap
            and o["limits"][1] <= self.loaded_data_cap
            and o["serialized_size"] <= 1232
            and o["account_count"] <= 64
            and o["version"] == "legacy"
        ]
        selected = feasible[0] if feasible else None
        used_predicted_candidate = selected is not None
        decision = None
        simulations = controls = 0
        planning_audit = None
        if method == "adaptive":
            decision, selected, planning_audit = self._adaptive_selection(
                queue, identifier, options, plans, candidates
            )
            simulations = planning_audit["estimation_simulations"]
            controls = planning_audit["control_simulations"]
        elif selected is not None:
            plan = plans[selected["count"]]
            if method == "learned":
                decision = execute_decision(
                    plan, rpc=self.rpc, registry=self.registry, current_slot=self.rpc.get_slot()
                )
                record_control_outcome(decision, self.registry)
                controls += int(plan.control_selected and decision.resource_simulation_calls > 0)
            else:
                decision = self._baseline_result(
                    plan, *selected["limits"], reason="application_baseline:" + method
                )
        else:
            for option in options:
                if (
                    option["serialized_size"] > 1232
                    or option["account_count"] > 64
                    or option["version"] != "legacy"
                ):
                    continue
                plan = plans[option["count"]]
                trial = execute_decision(
                    plan,
                    rpc=self.rpc,
                    registry=self.registry,
                    force_simulation=True,
                    current_slot=self.rpc.get_slot(),
                )
                record_control_outcome(trial, self.registry)
                simulations += trial.resource_simulation_calls
                self.store.finish(
                    plan.observation_id,
                    trial.model_dump(mode="json"),
                    stream="payout-execution",
                    cursor=0,
                )
                if (
                    trial.status != "unresolved"
                    and trial.compute_unit_limit <= self.compute_unit_cap
                    and trial.loaded_accounts_data_size_limit <= self.loaded_data_cap
                ):
                    decision, selected = trial, option
                    break
        if decision is None or selected is None or decision.status == "unresolved":
            raise RuntimeError("no verified candidate fits fixed limits; worker paused")
        if (
            decision.compute_unit_limit > self.compute_unit_cap
            or decision.loaded_accounts_data_size_limit > self.loaded_data_cap
        ):
            raise RuntimeError("revalidated estimate exceeds fixed policy; replan")
        if method == "learned" and used_predicted_candidate:
            simulations += decision.resource_simulation_calls - controls
        result_record = self.store.get(decision.observation_id)
        if result_record["result"] is None:
            self.store.finish(
                decision.observation_id,
                decision.model_dump(mode="json"),
                stream="payout-execution",
                cursor=0,
            )
        body = {
            "id": identifier,
            "queue": queue,
            "cursor": q["cursor"],
            "method": method,
            "compute_unit_cap": self.compute_unit_cap,
            "loaded_data_cap": self.loaded_data_cap,
            "model_digest": model_digest,
            "chosen_count": selected["count"],
            "options": options,
            "decision": decision.model_dump(mode="json"),
            "reason": decision.reason,
            "mode": "prediction" if decision.status == "accepted_prediction" else "fallback",
            "estimation_simulations": simulations,
            "control_simulations": controls,
            "retry_after_confirmed_failure": retry_after_failure,
            "recovery_simulation": recovery_simulation,
            "inference_and_candidate_ms": inference_ms,
        }
        if planning_audit is not None:
            body["adaptive_planning"] = planning_audit
        body["limit_source"] = (
            "fresh_simulation"
            if decision.status == "simulation_success"
            else "qualified_learned_prediction"
            if method in {"learned", "adaptive"}
            else "scoped_fitted_estimate"
            if estimate_method in {"fixed_batch", "formula"}
            else "application_baseline_estimate"
        )
        if decision.status == "accepted_prediction" and selected.get("baseline_invalidation_key"):
            body["baseline_invalidation_key"] = selected["baseline_invalidation_key"]
        if method == "cache":
            from examples.payouts.baselines import cache_key, update_cache
            from examples.payouts.model import PayoutStateEnvelope

            state = PayoutStateEnvelope.model_validate(selected["state"])
            cache = self.setting("estimate_cache") or {}
            if decision.simulation:
                sim = decision.simulation
                cache = update_cache(
                    cache,
                    state,
                    decision.plan.features,
                    ResourceLabel(
                        success=True,
                        compute_units=sim.units_consumed,
                        loaded_accounts_bytes=sim.loaded_accounts_bytes,
                    ),
                    observed_slot=sim.slot,
                )
            else:
                key = cache_key(state, decision.plan.features)
                cache[key]["uses"] += 1
            self.set_setting("estimate_cache", cache)
        self._store_step(identifier, queue, body)
        # Snapshot is evidence, not a lock. Check it again immediately before signing.
        fresh = self.bridge.call("snapshot", queue=queue, count=selected["count"])
        if (
            fresh["snapshot_digest"] != selected["snapshot_digest"]
            or fresh["slot"] - candidates[selected["count"]]["slot"] > 8
        ):
            raise RuntimeError("pre-execution state changed; this decision requires replanning")
        self.refresh(fresh["slot"])
        if self.deployment_bindings != selected["state"]["deployment_bindings"]:
            raise RuntimeError("deployment evidence changed after estimation; replan")
        if decision.status == "accepted_prediction" and estimate_method in {
            "fixed_batch",
            "formula",
        }:
            from examples.payouts.baselines import baseline_estimate, baseline_invalidation_key
            from examples.payouts.model import PayoutStateEnvelope

            if self.bundle is None or self.baselines is None:
                raise RuntimeError("fitted source disappeared before signing; replan")
            current_key = baseline_invalidation_key(
                estimate_method, self.bundle, self.baselines, selected["count"]
            )
            current_limits = baseline_estimate(
                estimate_method,
                self.bundle,
                PayoutStateEnvelope.model_validate(selected["state"]),
                decision.plan.features,
                current_slot=fresh["slot"],
                cache={},
                fitted_baselines=self.baselines,
                registry=self.registry,
                invalidated_keys=self.setting("fitted_baseline_invalidations") or {},
            )
            if current_key != body.get("baseline_invalidation_key") or current_limits != (
                decision.compute_unit_limit,
                decision.loaded_accounts_data_size_limit,
            ):
                raise RuntimeError("fitted source changed or invalidated before signing; replan")
        if decision.status == "accepted_prediction" and decision.plan.profile_id:
            check = self.registry.check(
                decision.plan.profile_id,
                current_slot=fresh["slot"],
                context=self.context,
                cluster_identity=self.cluster,
                runtime_identity=self.runtime,
                workload="payout-queue-v1",
                program_ids=decision.plan.features.program_ids,
            )
            if (
                not check.eligible
                or check.revision != decision.plan.profile_revision
                or check.artifact_sha256 != decision.plan.artifact_digest
            ):
                raise RuntimeError("deployment/profile changed before signing; replan")
        signing_started = time.perf_counter()
        signed = self.bridge.call("sign", wire=decision.unsigned_transaction_base64)
        verified_signature = self.store.attach_signature(
            decision.observation_id, signed["wire"], commitment="confirmed"
        )
        if verified_signature != signed["signature"]:
            raise RuntimeError("signer response signature does not match verified wire")
        signing_ms = (time.perf_counter() - signing_started) * 1000
        with self.store.db:
            self.store.db.execute(
                "UPDATE payout_steps SET phase='signed',signature=?,wire=? WHERE id=?",
                (signed["signature"], signed["wire"], identifier),
            )
        preparation_ms = (time.perf_counter() - started) * 1000
        if interrupt_after_sign:
            return {
                "interrupted_after_sign": True,
                "signature": signed["signature"],
                "id": identifier,
            }
        try:
            sent = self.bridge.call("send", wire=signed["wire"])
            if interrupt_after_send:
                return {
                    "interrupted_after_send": True,
                    "signature": signed["signature"],
                    "id": identifier,
                }
        except (httpx.HTTPError, RuntimeError):
            with self.store.db:
                self.store.db.execute(
                    "UPDATE payout_steps SET phase='uncertain' WHERE id=?", (identifier,)
                )
            return self.reconcile_pending(queue)
        self.store.reconcile(
            signed["signature"], sent["transaction"], commitment="confirmed", registry=self.registry
        )
        verified = self.bridge.call("verify", queue=queue)
        transaction = sent["transaction"]
        if transaction is None:
            raise RuntimeError("submitted signature has no outcome yet; reconciliation required")
        meta = transaction["meta"]
        resource_exhausted = self.audit_budget_failure(body, transaction)
        if resource_exhausted:
            # Every estimator uses the same bounded recovery policy. Keep the
            # failed attempt and its fee, then simulate this queue's remainder.
            self.set_setting("force_simulation:" + queue, True)
        success = meta["err"] is None
        if success and (
            not verified["correct"]
            or verified["queue"]["cursor"] != q["cursor"] + selected["count"]
            or verified["queue"]["last_decision"] != sha([identifier, selected["count"]])
            or verified["queue"]["last_model"] != model_digest
        ):
            raise RuntimeError("post-execution payment verification failed")
        after_balance = self.executor_balance()
        methods = self.rpc.method_counts + self.bridge.method_counts - methods_before
        fee_payer_debit = meta["preBalances"][0] - meta["postBalances"][0]
        outcome = {
            "signature": signed["signature"],
            "verification": verified,
            "transaction": transaction,
            "compute_units": meta.get("computeUnitsConsumed"),
            "loaded_accounts_bytes": meta.get("loadedAccountsDataSize"),
            "fee_lamports": meta["fee"],
            "rent_deposit_lamports": fee_payer_debit - meta["fee"],
            "executor_spend_reconciled": balance - after_balance == fee_payer_debit,
            "success": success,
            "confirmed_resource_exhaustion": resource_exhausted,
            "preparation_ms": preparation_ms,
            "signing_ms": signing_ms,
            "complete_step_ms": (time.perf_counter() - started) * 1000,
            "local_transport_ms": self.bridge.transport_ms - transport_before,
            "rpc_calls": self.rpc.call_count + self.bridge.rpc_calls - calls_before,
            "rpc_retries": self.rpc.retry_count - retries_before,
            "rpc_method_counts": dict(methods),
            "state_deployment_reads": sum(
                methods[name] for name in ("getAccountInfo", "getMultipleAccounts")
            ),
            "bridge_submission_confirmation_ms": sent["submission_confirmation_ms"],
        }
        with self.store.db:
            self.store.db.execute(
                "UPDATE payout_steps SET phase=?,outcome=? WHERE id=?",
                ("confirmed" if success else "failed", canonical(outcome), identifier),
            )
        if not success and not resource_exhausted:
            raise RuntimeError("confirmed failed batch; no payments advanced")
        return {**body, **outcome}

    def run(self, queue: str, method: str = "learned"):
        started = time.perf_counter()
        results = []
        for _ in range(16 + MAX_ATTEMPTS):
            result = self.step(queue, method)
            results.append(result)
            verified = result.get("verification", {})
            if result.get("stopped") or verified.get("queue", {}).get("status") == 1:
                return {
                    "method": method,
                    "compute_unit_cap": self.compute_unit_cap,
                    "loaded_data_cap": self.loaded_data_cap,
                    "steps": results,
                    "verification": verified,
                    "queue_completion_ms": (time.perf_counter() - started) * 1000,
                }
        raise RuntimeError("bounded worker step limit reached")

    def close(self):
        self.store.db.close()
        self.rpc._client.close()
        self.bridge.client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["info", "collect", "create", "qualify", "run", "step"])
    parser.add_argument("--directory", type=Path, default=Path("artifacts/payouts"))
    parser.add_argument("--groups", type=int, default=80)
    parser.add_argument("--length", type=int, default=16)
    parser.add_argument("--existing", type=int, default=0)
    parser.add_argument("--queue")
    parser.add_argument("--method", choices=METHODS, default="learned")
    parser.add_argument(
        "--compute-unit-cap",
        type=int,
        default=CU_CAP,
        help="Application ceiling from 1 to 1400000 CU; does not set every transaction's limit",
    )
    parser.add_argument(
        "--verbose-json", action="store_true", help="Print the complete saved trace"
    )
    args = parser.parse_args()
    if not 1 <= args.compute_unit_cap <= 1_400_000:
        parser.error("--compute-unit-cap must be from 1 through 1400000")
    app = Application(args.directory, compute_unit_cap=args.compute_unit_cap)
    try:
        if args.command == "info":
            result = app.info
        elif args.command == "collect":
            result = app.collect(args.groups)
        elif args.command == "qualify":
            result = app.qualify()
        elif args.command in {"run", "step"}:
            queue = (
                args.queue
                or app.create(length=args.length, existing=args.existing)["queue"]["address"]
            )
            result = getattr(app, args.command)(queue, args.method)
            (args.directory / "last-run.json").write_text(json.dumps(result, indent=2) + "\n")
        else:
            result = app.create(length=args.length, existing=args.existing)
        printed = result
        if not args.verbose_json and args.command in {"run", "step"}:
            verified = result.get("verification", {})
            queue_state = verified.get("queue", {})
            steps = result.get("steps", [result])
            printed = {
                "queue": queue_state.get("address"),
                "method": args.method,
                "compute_unit_cap": app.compute_unit_cap,
                "loaded_data_cap": app.loaded_data_cap,
                "executed_counts": [step["chosen_count"] for step in steps if step.get("success")],
                "failed_attempts": sum(step.get("success") is False for step in steps),
                "completed": queue_state.get("paid_count"),
                "pending": queue_state.get("length", 0) - queue_state.get("cursor", 0),
                "correct": verified.get("correct"),
                "duplicate_count": verified.get("duplicate_count"),
                "vault_balance": verified.get("vault_balance"),
                "report": str(args.directory / "last-run.json"),
            }
        elif not args.verbose_json and args.command == "qualify":
            printed = {
                key: result[key]
                for key in (
                    "scope",
                    "active_count",
                    "candidate_cell_count",
                    "qualification_failures",
                )
            }
            printed["report"] = str(args.directory / "qualification.json")
        print(json.dumps(printed, indent=2))
    finally:
        app.close()


if __name__ == "__main__":
    main()
