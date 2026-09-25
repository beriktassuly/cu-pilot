# Local TypeScript integration

The private `typescript/` package uses pinned Solana Kit 8.3.0. Node 24 or later
is required. It does not sign, send, change preflight policy, or contact a prediction
service. Python produces `cu-pilot-resources-v1` artifacts; CU-only artifacts are
rejected. The shared `tests/fixtures/resource_contract.json` tests local policy parity.

```sh
cd typescript
npm ci
npm test
npm run build
```

Public interfaces exported by `src/index.ts` are `buildTransferBatch`,
`estimateResources`, `bindMessage`, `verifyBoundMessage`, `decodeBuilder`,
`loadArtifact`, `predictResources`, `resolveLookupTables`, and `FileControlStore`.
The builder example is
an ordered batch of System transfers, not an external customer workload.

```ts
const result = await estimateResources(message, {
  rpc, artifact: loadArtifact(JSON.parse(artifactJson)), release,
  controlStore: new FileControlStore('./data/controls.jsonl'),
  context: release.manifest.context,
  cluster: release.manifest.cluster_identity,
  runtime: release.manifest.runtime_identity,
  workload: 'system-transfer-batch', currentSlot,
  budgetIndependent: true,
});
// result.status: prediction | simulation | unresolved
// result.unsignedMessage belongs to the caller's signing/sending flow.
```

`release` is `ProfileRegistry.export_snapshot(...)` from Python, refreshed by the
application. Its exact `artifact_canonical_json` is hash-verified without relying
on JavaScript/Python float serialization equivalence. Deployment checks use this
bounded snapshot; changes after export can propagate only by its expiry or refresh.
No extra deployment RPC runs on each accepted prediction. Artifact refresh, lookup
resolution, snapshot refresh and control storage are caller-owned costs and must be
included in application latency measurements.

The adapter compiles the builder and derives features from its wire bytes. Optional
`expectedWire` is an exact unsigned wire equivalence assertion. Missing resource
instructions are appended before binding; existing resource instructions are replaced
in place. Fees, instruction order, heap, and nonce instruction placement are preserved.
Every changed compiled message receives a new `message-v1` identity. `shape-v1` is
only a grouping identifier. This adapter permits no implicit blockhash refresh.
Rebind after any change. All prepared output is unsigned.

For v0, use `resolveLookupTables(rpc, tableAddresses, {currentSlot, cluster})` before
compressing/binding the builder. It checks the actual RPC account owner, layout,
deactivation and same-slot extension visibility with the official SDK decoder.
Advance the application's current-slot view to the returned `checkedSlot` when
that fresh read is newer. The compiled builder's lookup addresses must match the
verified index resolution. Cached evidence remains subject to freshness checks;
arbitrary table mappings are a caller trust boundary, not verified chain evidence.

Skipping requires active, current deployment evidence, an allowlisted workload,
an explicit budget-independent contract and a durable control store. Budget-sensitive
workloads simulate and retain maximum resource budgets. `FileControlStore` writes and
fsyncs the selection before simulation, records outcomes separately, and quarantines
resource excess or repeated failed controls across restarts.
The immutable release's `max_control_failure_streak` is copied into each
persisted decision as `maxControlFailureStreak`; failure counts are isolated by
profile, revision and artifact digest. The store has no overriding default threshold.
Its optional second constructor argument is an explicit compatibility pin: a
different released threshold is rejected. Deployment freshness intervals accept
positive finite seconds through 3,600, including fractional intervals, as Python does.
It is a single-process journal; multiple instances within that process synchronously replay new records
before checks and writes. Each check verifies the existing journal prefix, so its
local read/hash cost grows with retained history. Multiple writer processes are
unsupported. Corrupted/torn records and changed prefixes fail closed. Recovery requires operator review and
requalification; deleting a journal to clear quarantine is not a recovery procedure.
See [typescript-recovery.md](typescript-recovery.md) for the explicit append-only
requalification operation and old-revision quarantine behavior.

Fees and slots use `bigint` in memory and canonical decimal strings in exported JSON.
Resource counts use safe integers. Legacy/v0 priority prices remain micro-lamports
per CU; v1 absolute priority lamports remain unchanged when resource limits change.
v1 skipping stays disabled by default. Fallback uses 10% headroom, rounds CU upward
to 100 and data to 32,768 bytes, and rejects required limits above protocol caps.
Artifact policy controls prediction margins/rounding, independently of fallback.
Successful simulation describes one bank state, not guaranteed later execution success.

Default tests are offline. Their RPC doubles test failure handling, not Solana
execution. See [local-runtime.md](local-runtime.md) for the separately invoked real
SDK and runtime suite, prerequisites, and measured results.
