# Profile releases and deployment checks

`cu_pilot.lifecycle.ProfileRegistry` stores immutable resource artifacts, profile
manifests, release pointers, deployment evidence, control selections, execution
audits and operator actions in one local SQLite database. SQLite transactions bind
the release pointer to the complete retained artifact. Concurrent readers see a
complete old or new revision. WAL and full synchronous commits protect checkpoints;
an interrupted release does not expose a half-written artifact. Keep this database
and exported artifacts outside Git.

The registry accepts only validated `cu-pilot-resources-v1` artifacts. CU-only
artifacts are rejected. `artifact_digest(payload)` validates the artifact and hashes
canonical portable JSON with sorted keys, compact separators and ASCII escaping.
Formatting changes do not change its identity. Registration verifies the digest,
context, provenance and last evidence slot against the artifact itself. Pattern
ranges, joint-risk summaries and numerical limits remain in the validated resource
artifact; the release manifest adds the operational boundary.

## State and release authority

| From | To | Trigger |
| --- | --- | --- |
| New | candidate | Explicit registration of immutable manifest and artifact |
| candidate | shadow | Explicit operator action |
| shadow | active | Explicit release after compatibility and freshness checks |
| active | shadow | Another revision replaces it; artifact remains retained |
| active | suspended | Stale/missing deployment evidence, watcher failure, program change, stale labels, observed resource excess, or configured control deterioration |
| Any nonretired revision | suspended | Explicit operator suspension |
| Any nonretired revision | retired | Explicit irreversible retirement |

No process automatically promotes a candidate, including the synthetic demo. A
synthetic artifact cannot be activated. A release must contain at least one pattern
meeting its dual-resource support, calibration and limit policy; inference still
checks the actual pattern. These checks establish policy consistency, not production
validation or a guaranteed probability of future success.

A quarantined revision cannot return to shadow or active. Recovery means collecting
new observations, fitting a new revision whose calibration begins after the latest
suspension slot, reviewing it in shadow and explicitly releasing it. Audit history
is retained. Restoring a saved database is not a supported recovery operation.

`rollback(profile_id, revision, ...)` selects an earlier retained, unquarantined
shadow revision. It uses the same deployment, observation age, runtime, workload and
dependency checks as activation. It does not refresh labels or clear the replaced
revision's quarantine. Changing only the revision number cannot bypass recovery
evidence requirements. Quarantine also binds the artifact digest: a pre-registered
alias of the same rejected artifact cannot release, and a late control outcome
invalidates an active alias too. `force_simulation(True, actor=..., reason=...)` is a global
emergency switch; turning it off leaves any suspensions intact.

## Deployment evidence

The adapter refreshes a bounded set of at most 50 programs by default, using at most
two `getMultipleAccounts` batches. The caller supplies its RPC client and schedules
the watcher. This is a read-only operation with no transaction submission. Program
identity includes the program address, owner, cluster genesis identity, runtime
build identity and full account bytes:

- Loader-v3 validates the executable Program state, follows its ProgramData pointer,
  verifies ProgramData owner/state, and hashes the full ProgramData bytes, including
  deployment slot and authority. Its fixed Program and ProgramData headers are 36
  and 45 bytes. These offsets come from the [official loader-v3 interface source](https://docs.rs/crate/solana-loader-v3-interface/latest/source/src/state.rs).
- Immutable BPF loader-v1/v2 programs use their executable account code bytes and
  owner. Native programs additionally depend on the declared runtime identity;
  their account data alone is not their runtime code. Loader addresses come from
  the [official SDK identifier source](https://docs.rs/solana-sdk-ids/latest/src/solana_sdk_ids/lib.rs.html).
- Unknown loaders, including loader-v4, require simulation. Loader-v4 deployment
  decoding has not been validated by this implementation.

For loader-v3, both batches must report exactly the same slot. A slot change between
batches produces an explicit failed refresh; it does not certify a mixed snapshot.
An observation at or before the deployment slot is rejected because deployment
visibility can lag. This conservative policy can require retrying a refresh.
Activation, rollback and inference also require each tracked deployment slot to be
strictly earlier than the artifact's frozen calibration start. An old artifact
cannot be rebound to a newer deployment merely by creating a fresh manifest. The
current artifact does not record the earliest fitting slot: fitting may predate
deployment, but all validating calibration must follow it. Immutable/native loaders
without a deployment slot still rely on the explicit context and runtime boundary;
a code fingerprint alone cannot date their historical calibration labels.

`watch-once` verifies `getGenesisHash` and `getVersion` before fetching accounts.
The runtime string is `rpc:<solana-core>:feature-set:<feature-set>`. **The RPC build
feature identifier is not a complete fingerprint of active runtime feature gates.**
Operators must change workload context/runtime assumptions at relevant activation
boundaries. Native runtime changes without a new supplied identity cannot be
inferred from an unchanged program account. A controlled local emulator adapter can
supply its pinned runtime identity explicitly. RPC integrity and the operator's
runtime/dependency declarations are trust assumptions, not cryptographic attestations.

Every bound program needs an explicit dependency entry, including `[]` for leaves.
Every known CPI target must itself be bound and tracked. The operator must set
`dependency_closure_verified=true` for a controlled, audited invocation family.
Top-level instruction inspection cannot discover all dynamic CPI branches. An
untracked dependency or an incomplete closure requires fallback. The manifest must
also set `budget_independent=true`: workloads that inspect budget instructions or
remaining compute in ways that change behavior stay on simulation unless the
controlled adapter has established an appropriate equivalence contract.

Eligibility reads cached evidence with both slot-age and wall-clock-age bounds.
It never fetches deployment accounts on the prediction path. A failed watcher
immediately invalidates its cached certification and is recorded without exception
text that might contain a provider credential. Recovery of the watcher requires a
successful batch covering all programs previously cached in that registry. Use a
registry for one bounded deployment/workload set; a refresh of an unrelated program
cannot clear a failed dependency check. Active profiles already suspended by a
failure still require requalification.

## Local application interfaces

```python
from cu_pilot.lifecycle import ProfileRegistry

registry = ProfileRegistry("artifacts/profile-registry.sqlite")
manifest, estimator = registry.load_active("settlement-batch")
eligibility = registry.check(
    manifest.profile_id,
    current_slot=current_slot,
    context=manifest.context,
    cluster_identity=cluster_genesis_hash,
    runtime_identity=verified_runtime_identity,
    workload="settlement-batch-v1",
    program_ids=bound_message_program_ids,
)
```

`load_active` returns a validated immutable snapshot. The bound estimator must also
compare that model's digest with `eligibility.artifact_sha256` and retain the checked
revision. A later release must not relabel an earlier decision. As with simulation,
these checks cannot guarantee that chain state stays unchanged between preparation
and execution. Signing and sending remain caller responsibilities.
`Eligibility.evidence_snapshot` captures the checked manifest, deployment identities,
watcher/emergency flags, check time and resulting profile state in the same SQLite
transaction as the decision. Persist that snapshot with the decision; an independently
exported later snapshot is not evidence of what the earlier decision checked.

`export_snapshot(profile_id)` emits `cu-pilot-release-snapshot-v1` for TypeScript.
It contains the exact `artifact_canonical_json` hash preimage, manifest, current
state/quarantine, emergency/watcher flags, deployment observations and export time.
Slots are decimal strings. This avoids JavaScript precision loss and differences
between Python and JavaScript float serialization. The CLI writes snapshots through
a flushed temporary file and atomic replacement.

The local consumer must validate artifact hash and compatibility, enforce manifest
and deployment eligibility, reject future/stale timestamps, and enforce snapshot
expiry no longer than `max_deployment_age_seconds`. The caller refreshes snapshots
within that bound. A revocation after export propagates on refresh or expiry; an
offline file is not an instantaneous revocation channel. Extra refresh reads,
watcher calls and disk logging belong in full preparation cost measurements.

## Control simulations and execution outcomes

`select_control(...)` persists an unpredictable sampled choice before its label is
available. The immutable manifest determines sampling probability. The event keeps
request ID, original decision version/revision, eligibility, probability, limits and
selection slot. A retry returns the same selection. Conflicting reuse of the ID
fails. Only a selected observation can receive a control outcome.

`record_control(...)` keeps simulation success/failure, both independently nullable
measurements, elapsed time and context slot separately from the prediction. Any
observed successful resource excess suspends that decision revision. Missing or
failed measurements are not successful demand labels; by default three consecutive
incomplete/failed controls suspend for deterioration. The failure streak resets on
a complete successful control. Probability zero is allowed for explicit experiments
but provides no ongoing audited deployment evidence.

The estimation and shadow adapters retain validated partial RPC measurements when
the estimate remains unresolved. An error-free simulation with a known CU or data
excess suspends immediately even if the other resource is missing. Under-limit
incomplete measurements and failed transactions count toward deterioration instead.
Shadow restart reuses the frozen evidence, including observation slot and elapsed
time, so replay neither repeats the simulation nor increments its failure streak.

There is one explicit upgrade limitation for existing Python registries. Earlier
adapters could audit an unresolved partial simulation as a failed control without
its known measurements. Replaying that completed shadow result now produces the
corrected interpretation, which conflicts with the already immutable control
outcome. Resume stops with `Conflicting control outcome`; it does not overwrite
the previous audit, increment the streak, or resimulate. Preserve both the shadow
database and registry and enable forced simulation while an operator reviews the
raw result and earlier audit. No automatic audit migration is provided. Any reviewed
migration must retain the original selection, both interpretations and quarantine
history; deleting records or switching to an empty registry is not a recovery
procedure. Unaffected existing control outcomes and newly collected partial
outcomes retain normal idempotent replay.

`record_execution(...)` is a separate, idempotent audit path. The reconciliation
caller first verifies message/signature correspondence and then supplies the original
decision revision and limits. Successful measured CU or data excess suspends that
revision, even if the other resource is missing. Failed partial execution remains
an execution failure. Historical data commonly has no loaded-data measurement, so
it does not become paired evidence. A conflicting outcome is retained by the calling
reconciler as a new outcome version; the same registry observation ID cannot silently
change. Reconciliation and controls never overwrite the prediction being audited.

Shadow data is fully observed because every request is simulated. Deployment
controls observe a sampled subset and retain their selection probability; report
its denominator and selection bias. Count control calls and failed controls in
actual overhead. An accepted prediction audited by a control has not avoided that
simulation call.

## Operator commands

Create a `ProfileManifest` JSON with the actual artifact digest and verified
deployment fingerprints. The library schema is the authoritative contract; its
default dependency and budget-independence declarations deliberately prohibit
release until the controlled builder is reviewed. A release environment JSON has
exactly `current_slot`, `context`, `cluster_identity`, `runtime_identity`, `workload`
and `program_ids`, supplied from the current application environment.

```sh
uv run cu-pilot profiles register artifacts/profiles.sqlite manifest.json model.json --actor operator
uv run cu-pilot profiles shadow artifacts/profiles.sqlite settlement-batch 1 --actor operator --reason "Start prospective shadow"
# Explicit network operation, optional; CU_PILOT_RPC_URL stays in the environment.
uv run cu-pilot profiles watch-once artifacts/profiles.sqlite manifest.json --requests-per-second 5 --attempts 2
uv run cu-pilot profiles release artifacts/profiles.sqlite settlement-batch 1 environment.json --actor operator --reason "Reviewed holdout and deployment evidence"
uv run cu-pilot profiles status artifacts/profiles.sqlite settlement-batch
uv run cu-pilot profiles export artifacts/profiles.sqlite settlement-batch artifacts/release.json
uv run cu-pilot profiles force-simulation artifacts/profiles.sqlite --actor operator --reason "Investigate drift"
uv run cu-pilot profiles force-simulation artifacts/profiles.sqlite --no-enabled --actor operator --reason "Investigation resolved"
uv run cu-pilot profiles rollback artifacts/profiles.sqlite settlement-batch 1 environment.json --actor operator --reason "Return to compatible revision"
```

Rollback requires a newer active revision and the old revision still passing every
check. These are explicit operator workflows, not a prescription to release an
unvalidated model. No keys or paid providers are needed for offline lifecycle tests:

```sh
uv run pytest tests/test_lifecycle.py -q
```

Those tests use handcrafted account-state fixtures and exercise the state machine,
loader metadata parser, upgrade detection, RPC failure redaction, reconciliation
hooks, atomic snapshots, rollback and concurrency. They are not evidence of an
actual program upgrade running inside Solana. The separately invoked
[program-upgrade integration](program-upgrade.md) executes a signed loader-v3
Upgrade locally, then verifies suspension and actual fallback. It reloads the same
ELF under default Surfpool features; untested loader/runtime combinations remain
unsupported.
