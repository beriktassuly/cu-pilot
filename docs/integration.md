# Bound estimation, collection, and execution reconciliation

## What the application calls

```python
from cu_pilot.integration import EstimationContext, estimate_resources
from cu_pilot.lifecycle import ProfileRegistry
from cu_pilot.rpc import RpcClient

registry = ProfileRegistry("artifacts/profiles.sqlite")
manifest, model = registry.load_active("settlement-batch")
context = EstimationContext(
    context=manifest.context,
    current_slot=current_slot,
    cluster_identity=manifest.cluster_identity,
    runtime_identity=manifest.runtime_identity,
    workload="batch-transfer",
    budget_independent=True,
)
with RpcClient(endpoint, timeout=15, attempts=3, requests_per_second=10) as rpc:
    result = estimate_resources(
        serialized_transaction_base64,
        rpc=rpc,
        context=context,
        estimator=model,
        registry=registry,
        profile_id=manifest.profile_id,
    )
if result.status == "unresolved":
    # Surface the reason; there is no safe output to sign.
    raise RuntimeError(result.reason)
unsigned_bytes = result.unsigned_transaction_base64
# Continue the application's existing validation and wallet/signing flow.
```

Omit the model/registry to start with simulation. The example workload is a controlled batch
of System Program transfers. It is a test workload, not an external customer. An application
must establish its own state, dependency, and budget-independence assumptions before release.

The result retains the pre-label prediction, eligibility reason, model digest/version,
profile revision, policy version, context, lookup evidence, observation ID/slot, simulation
measurements, resource limits, unsigned output, full preparation timing, RPC attempts/retries,
and control selection. Failed, timed-out, cancelled, incomplete, stale-context and over-cap
simulations yield `unresolved`, with no final resource limit or transaction.

`estimate_resources` measures RPC attempts and retries over the entire operation,
including lookup preparation and preparation failures. Earlier calls on the same
client are excluded. A retried state read remains one logical state read; attempts,
retries and resource simulations are separate counters. The lower-level
`execute_decision` counters cover execution of its already prepared plan only.

## Message binding and transformations

The authoritative input is the serialized transaction. Solders/Kit decodes it; both features
and simulation bytes derive from that message. There is no public feature-plus-unrelated-wire
estimation input. `shape-v1` groups structurally similar workloads. `message-v1:<sha256>` hashes
the complete SDK message encoding, excluding signatures and including the lifetime/blockhash.
Python and TypeScript test both hashes against real SDK-generated fixtures.

The initial adapter operation fills CU and loaded-data placeholders before feature extraction.
For legacy/v0 it replaces existing valid resource instructions in place and appends missing
ones after existing instructions. An absent Compute Budget program key is appended as a
readonly static account; v0 lookup indices shift consistently. Existing fees, heap, instruction
ordering, account roles and nonce-first ordering are preserved. Invalid/duplicate budgets
are rejected. v1 resources use its config; the absolute lamport fee is unchanged.

Changing resource requests changes signed bytes. Every prepared output is unsigned, including
when the input happened to be signed. CU Pilot never claims existing signatures remain valid.
`verify_final_message(expected, actual)`/the TypeScript equivalent checks exact message equality
while permitting caller-added signatures. An explicit `allow_blockhash_refresh=True` permits
only a normal blockhash replacement and returns the new identity. Durable nonce refresh is
rejected. Other changes, including amounts, account identities, fees, heap, topology and final
limits, require re-estimation. Call this verifier at the signing boundary, not just immediately
after estimation. A pattern match alone is insufficient.

Production simulation runs the recorded maximum-budget message without implicit blockhash
replacement. Supply a valid current blockhash from the builder, or an intact durable nonce.
The ledger records original, prepared and final identities separately; they are not claimed
equal. Maximum simulation budgets avoid measuring a deliberately truncated execution. Limits
are rounded upward with an explicit margin; a required value over a protocol cap is unresolved.

Programs inspecting budget instructions or remaining compute can behave differently after
resource replacement. `budget_independent` defaults false. Without an explicit reviewed
contract, successful fallback retains maximum budgets rather than reducing them. A release
also requires that assumption in its manifest. Shape-only models do not certify balances,
account state, slippage, ownership, security or payment validation.

## Pre-execution evidence and transport

v0 resolution uses raw lookup-table account data, SDK decoding, verified owner and fresh slot
evidence. Deactivating tables and unavailable/newly extended indices are rejected; this adapter
does not guess at SlotHashes. A supplied cached table avoids a read, otherwise the caller's RPC
fetches it. Stale evidence is never accepted as fresh. Current slots must be exact nonnegative
integers obtained from the application's bounded current-slot feed; callers must not reuse a
frozen historical slot as current time. Profile deployment evidence also expires by wall clock.

RPC endpoints come from caller configuration. Error text never contains endpoint credentials
or arbitrary provider logs. HTTP 429/502/503/504, transport timeouts/network failures and
recognized retryable RPC conditions have bounded retries. Deterministic transaction errors
do not retry. Calls are rate-limited, cancellable between requests/backoff, and individually
timeout-bounded; Python synchronous in-flight cancellation completes at the request timeout.
Commitment and minimum context slot are explicit and checked in responses.

## Prospective shadow workflow

Each JSONL input is a `ShadowRequest` with `observation_id`, `wire_base64`, `context`, optional
raw `lookups`, and `evidence_origin` (`synthetic`, `local-runtime`, or `live-simulation`).
`collection_method` separately identifies offline replay. `examples/shadow_replay.py` writes a
fully runnable input using real SDK messages and synthetic recorded measurements.

```sh
# Optional bounded network simulation of explicitly supplied inputs; never sends.
uv run cu-pilot shadow requests.jsonl artifacts/events.sqlite --max-records 100 --requests-per-second 5
# Offline replay requires recorded replay_response objects; makes no network calls.
uv run cu-pilot shadow requests.jsonl artifacts/replay.sqlite --replay
uv run cu-pilot export-shadow artifacts/events.sqlite artifacts/audit.jsonl
```

For initial qualification before any profile is released, populate the registry
through the bounded `refresh_deployments(registry, rpc, program_ids, ...)` watcher,
using verified cluster/runtime identities, then supply `--registry` alone (library
equivalent: `registry=` on `collect_shadow`). The collector freezes already cached
watcher evidence for the bound message's actual top-level programs without another
RPC read. Configure bootstrap expiry with `EstimationContext.max_deployment_age_slots`
and `max_deployment_age_seconds` (defaults 100 slots and 60 seconds). The caller must
refresh the watcher during longer collections; its reads and latency belong in
reported collection costs.

```sh
uv run cu-pilot shadow requests.jsonl artifacts/events.sqlite --registry artifacts/profiles.sqlite
```

For a released profile, add `--profile` (library equivalent: `profile_id=`); this
uses the manifest's reviewed dependency closure and release policy:

```sh
uv run cu-pilot shadow requests.jsonl artifacts/events.sqlite --registry artifacts/profiles.sqlite --profile controlled-batch --model artifacts/resources.json
```

The frozen `DecisionPlan` records `deployment_snapshot` and
`deployment_evidence_status`. An `eligible` snapshot captures the manifest,
dependency fingerprints, deployment slots, watcher timestamps, release state,
emergency flags, check time and slot in the same registry transaction as the
eligibility check. `observed_unreleased` records fresh cached top-level program
observations before release, explicitly setting `dependency_closure_verified`,
`release_authorized` and `eligible` to false. It never enables skipping and does
not imply that CPI dependencies or budget independence were reviewed. `ineligible`
preserves stale, incompatible or failed evidence; `unavailable` means evidence is
missing (the bootstrap snapshot lists missing programs) or no registry was supplied
(null snapshot). Old plans missing these optional fields default to unavailable.
Neither missing nor ineligible evidence is presented as verified deployment eligibility. The artifact
digest is retained without duplicating the full artifact in every observation.
Runtime/cluster/workload context strings remain explicit application inputs; a
snapshot does not infer deployment epochs for an untracked historical dataset.

Shadow collection still simulates every request when a registry is supplied. If an
eligible decision is sampled for a control, that same shadow simulation is recorded
as a separate control audit, preserving its pre-label selection probability and
decision revision. This causes no second simulation and does not count an avoided
call. The observation result commits before the registry control audit; after an
interruption the exact stored outcome can finish that audit without resimulation.

SQLite schema version 1 uses WAL, parameterized queries, foreign keys and durable transactions.
The single synchronous worker has concurrency one and no prefetched queue, providing natural
backpressure and bounded memory. Each invocation has a record bound and the RPC has a request
rate bound. The prediction plan commits before simulation starts. Outcome and checkpoint commit
together. After interruption a pending plan is reused in shadow mode, preserving its original
prediction. A completed ID is deduplicated; changed inputs or changed frozen predictions under
the same ID raise a conflict and retain a conflict digest. Transport retries remain attempts of
one observation, never extra support. Resume reads the stream to verify IDs, so it is safe for
small append-only files rather than an unbounded backfill platform.
On resume, older valid `ShadowRequest` inputs compare using schema defaults for
new optional fields. The original stored input and pre-label plan remain unchanged;
different settings still conflict, and arbitrary untyped records require exact equality.

Failed/missing-label observations remain in the audit export. Resource fitting excludes them;
partial failed execution does not represent successful demand. Simulation labels are
`unitsConsumed`/`loadedAccountsDataSize`. Historical labels are `meta.computeUnitsConsumed`
and optional explicitly reported data size; `costUnits` is never a substitute. Select the
label source **and evidence origin** explicitly when exporting training data. Replaying a
recorded response does not make it a new prospective observation or historical execution.

## Reconciliation

Signing and sending remain outside the library. To attach an execution, supply the exact
caller-signed final transaction and its returned transaction result, or opt into a bounded read:

```sh
uv run cu-pilot reconcile artifacts/events.sqlite REQUEST_ID signed.base64 --result execution.json --commitment finalized
# Without --result, reads that signature from the configured RPC; never submits.
```

`ObservationStore.attach_signature` verifies the final message and every cryptographic
signature before binding the signature to the request. Reconciliation requires real base64
transaction bytes, matching signature/message and explicit metadata error status. Missing
outcomes stay missing/pending/unavailable; unavailable is not proof of a fork rollback.
Confirmed outcomes can change before finality; finalized conflicts are retained as conflicts.
The request's aggregate outcome uses the latest recorded revision of every attached signature:
`finalized`, `confirmed`, `pending_finality`, `pending`, `unavailable`, then `missing`, in descending
precedence. This describes the strongest commitment evidence, including failed execution; it
does not imply success or that every retry has finished. A pending or unavailable retry cannot
erase another attempt's finalized evidence. Per-attempt statuses and labels remain in the audit
export, and duplicate old revisions do not replace newer evidence.
Duplicate results do not add labels, and retries of one request contribute at most one selected
finalized execution label. Simulation and execution error reports remain separate. No method
replays a historical transaction against today's state and calls it historical execution.

Keep the database and wire records private: transactions can contain application data. They,
model outputs and large datasets are ignored by git. Export JSONL for inspection/experiments;
the database schema rejects unknown future versions rather than reinterpreting them.

## Compatibility and limits

Existing CU-only artifacts and `PatternEstimator` remain supported by the old CLI/API. Only
`cu-pilot-resources-v1` artifacts can be used in the integrated dual-resource path. No automatic
migration fabricates missing loaded-data labels. Retrain from explicitly paired evidence.
The local API remains an inspection endpoint; application inference is local in TypeScript.
The operator explicitly releases profiles, maintains the deployment watcher and accounts for
known CPI dependencies. Untracked dependencies require fallback. See lifecycle and local-runtime
documentation for supported loaders, runtime assumptions, tested matrix and remaining gaps.
