# Recovering a TypeScript control quarantine

`FileControlStore` persists decisions before control outcomes in an append-only,
fsynced local journal. Use one store instance in one process per journal. It verifies
that each outcome has the same message identity, profile revision, artifact digest,
prediction, sampling probability and selected status as the frozen decision. It
calculates resource excess from the measured labels; callers cannot override the
result by changing `resourceExcess`. Invalid or conflicting records are rejected
before writing. Each decision persists `maxControlFailureStreak` from its release's
`max_control_failure_streak`. Failure streaks and thresholds are keyed by profile,
revision and artifact digest; unrelated profiles do not share counts. Replaying the
journal restores both counts and their released thresholds. A complete successful
control resets only that release's streak. A threshold cannot change within the same
release identity. The optional second constructor argument is a compatibility pin,
not a fallback policy: it rejects any decision or recovery using a different threshold.

An RPC simulation with `err: null` and missing or invalid resource measurements has
status `incomplete`. Its valid individual measurements remain in the separate control
event. Either known resource excess suspends immediately, even if the counterpart is
missing. An incomplete control without known excess increments the failure streak;
it never supplies an accepted estimate or unsigned result. Complete paired measurements
are required for status `success`. Transaction errors and transport failures have
status `failed`; their partial execution usage is not treated as successful demand.
The journal validates these distinctions and recomputes excess before writing/replay.

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
The recovered revision uses its own released threshold, recorded in the recovery
event and subsequent decisions, without altering old revisions' thresholds or history.

The current journal schema is `cu-pilot-controls-v2`. Version 1 recorded a store-wide
threshold that could differ from the actual release, so its entries cannot prove
which release policy governed a decision. Version 1 and earlier experimental journals
fail closed with `incompatible_control_journal`; they are never silently relabeled as
version 2. There is no automatic migration. Preserve the original journal and remain
in forced simulation until operator-reviewed reconstruction can retain its decisions,
outcomes and quarantine evidence under verified release policies. Do not delete or
replace a journal to bypass quarantine.
