# Recovering a TypeScript control quarantine

`FileControlStore` persists decisions before control outcomes in an append-only,
fsynced local journal. Use one store instance in one process per journal. It verifies
that each outcome has the same message identity, profile revision, artifact digest,
prediction, sampling probability and selected status as the frozen decision. It
calculates resource excess from the measured labels; callers cannot override the
result by changing `resourceExcess`. Invalid or conflicting records are rejected
before writing. Reopening with a different failure-streak policy fails closed.

A quarantine blocks the profile until an explicit recovery operation qualifies a
new release. Changing a revision number or reusing a previous digest does not
recover it. The original quarantines and outcomes remain in the journal.

First collect new paired evidence, fit and calibrate a new artifact, and explicitly
release a new revision through the Python registry. Export a fresh active release
snapshot. Then call:

```typescript
const recoveryId = controlStore.recover({
  release: freshPythonReleaseSnapshot,
  artifact: freshlyQualifiedArtifact,
  bound: bindMessage(prepareResources(builderMessage), lookupEvidence),
  context: {
    context: workloadContext,
    cluster: clusterIdentity,
    runtime: runtimeIdentity,
    workload: workloadName,
    currentSlot,
    budgetIndependent: true,
  },
  actor: "operator-name",
  reason: "Reviewed new paired shadow evidence after the resource excess",
});
```

Recovery checks the snapshot's active state, quarantine, artifact content/digest,
cluster/runtime/workload bindings, complete dependency tracking, deployment freshness,
observation freshness and emergency switches. It also requires an accepted joint
resource prediction for the supplied bound message, a strictly newer revision and
different digest than every rejected release, and a chronological calibration
boundary later than every recorded rejection slot. A stale watcher, stale snapshot,
incompatible context, unchanged artifact or insufficient requalification fails.

The journal retains operator/reason/time and the release, message and context used
to authorize recovery. On reopening, it reconstructs that audited decision using
its original check time; every subsequent runtime estimate still checks current
snapshot and deployment freshness. Only the exact recovered revision and digest
can proceed. Prior revisions, digest aliases and other revisions remain blocked.
A new control excess or deterioration suspends the recovered revision again.

The current journal requires artifact digests and an immutable failure-streak
policy on its entries. Earlier experimental journals without those fields fail
closed; no automatic migration clears their suspension evidence. Preserve such
logs for review. Do not delete or replace a journal to bypass quarantine.
