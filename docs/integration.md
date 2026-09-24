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

SQLite schema version 1 uses WAL, parameterized queries, foreign keys and durable transactions.
The single synchronous worker has concurrency one and no prefetched queue, providing natural
backpressure and bounded memory. Each invocation has a record bound and the RPC has a request
rate bound. The prediction plan commits before simulation starts. Outcome and checkpoint commit
together. After interruption a pending plan is reused in shadow mode, preserving its original
prediction. A completed ID is deduplicated; changed inputs or changed frozen predictions under
the same ID raise a conflict and retain a conflict digest. Transport retries remain attempts of
one observation, never extra support. Resume reads the stream to verify IDs, so it is safe for
small append-only files rather than an unbounded backfill platform.

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
