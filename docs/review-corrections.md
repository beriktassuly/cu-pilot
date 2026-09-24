# Policy consistency corrections — 2026-09-24

This batch addresses the six findings from the review of `40bd16b4`, merged through
PR #1 as `8b68efa3`. It changes policy enforcement, evaluation and audit reporting;
it does not qualify a production model or introduce a new workload claim.

## Corrections

1. TypeScript binds `max_control_failure_streak` to the persisted decision's
   profile, revision and artifact. Restart, recovery and multiple-profile tests
   cover released thresholds of one and three. An optional store constructor
   threshold is a compatibility check, never an override.
2. Complete-policy replay counts failed/incomplete controls, resets the failure
   streak on complete success and suspends on a known successful resource excess
   even when the other measurement is missing. State covers one released profile
   revision across its patterns. Outcomes affect only later slots; paired-risk
   denominators remain separate. Both runtime adapters retain validated partial
   simulation evidence while leaving incomplete estimates unresolved. Invalid
   companion measurements cannot erase a valid known excess.
3. Timing joins require matching observation origin, collection method and mode,
   in addition to holdout membership. Independent timing datasets can still use
   `preparation_report` without asserting a join to resource labels.
4. Both languages accept positive finite deployment freshness intervals through
   3,600 seconds, including fractional intervals. Shared fixtures cover exact and
   just-expired boundaries at 0.5 and 1.5 seconds.
5. Aggregate execution status reflects the strongest latest evidence across
   attached signatures. A later pending or unavailable retry cannot erase another
   attempt's finalized status. Per-attempt errors, revisions and labels remain
   separate; finalized status does not imply successful execution.
6. `estimate_resources` returns RPC attempts and retries for its entire operation,
   including lookup preparation and preparation failures. Previous calls on a
   reused client are excluded.

## Compatibility and operating guidance

The TypeScript control journal is now `cu-pilot-controls-v2`. Older journals fail
closed because they did not bind the released threshold. Preserve their audit and
quarantine history and use forced simulation until an operator-reviewed
reconstruction can retain that evidence under verified release policies. There
is no automatic migration. See [recovery requirements](typescript-recovery.md).

TypeScript simulation observations now distinguish `incomplete` (an error-free
transaction with missing/invalid measurements) from `failed`. Both leave resource
estimation unresolved; only validated successful measurements count as demand.
Existing Python partial-control audits can conflict with the corrected evidence
mapping on resume. The original events are preserved and replay stops explicitly;
see the [Python audit upgrade limitation](lifecycle.md).

Observation JSON gains optional `collection_method` and `collection_mode` fields.
Older observations remain valid for resource fitting and evaluation, but cannot
be joined to timing traces without known collection semantics. Reconciled
historical labels retain their original preparation provenance. Resource artifact
and release snapshot versions are unchanged.

`evaluate_resources(..., max_control_failure_streak=...)` must receive the released
threshold when it differs from the research default of three. Evaluate different
profile revisions separately. Its slot ordering is an explicit replay assumption,
not a reconstruction of unknown real outcome-arrival times.

Existing [integration](integration.md), [TypeScript](typescript.md),
[model/evaluation](resource-model.md), and [local runtime](local-runtime.md)
workflows remain the runnable entry points. The original
[verification report](verification.md) retains its original measurements; those
numbers are not new results from this correction batch.

## Validation

| Check | Result |
| --- | --- |
| `uv run --locked pytest -q` | 407 passed, one existing Starlette deprecation warning |
| `uv run --locked ruff check .` | Passed |
| `uv run --locked ruff format --check .` | Passed |
| `uv run --locked mypy src` | Passed, 19 source modules |
| `uv run --locked cu-pilot demo` and `uv build` | Passed; demo remains synthetic |
| `npm --prefix typescript run build` | Passed |
| `npm --prefix typescript test` | 53 passed, none skipped |
| Actual TypeScript Surfpool suite | Legacy/v0/v1 serialization, simulation and signed execution passed |
| Actual Python collector | 500 paired prospective observations, 100 untouched holdout rows, restart/reconciliation, activation and control suspension passed |
| Actual program and known CPI upgrades | Both signed upgrades, suspension and fallback passed |

The native checks ran through Linux Node 24.14.1 in WSL, using Kit 8.3.0 and
Surfpool 1.5.0 (reported core 4.1.2, feature set 3345198602). Python checks used
3.12.14 and Solders 0.29.0. Commands and the supported feature matrix are in
[local runtime setup](local-runtime.md). No public-network transactions were sent.

The rerun retained 280 fitting and 120 calibration slot groups. Simulation and
finalized execution both reported 600 CU for the legacy batch. Growing account
data produced 65,572 loaded bytes and a selected control suspended the profile.
Upgrade tests reload the same executable, establishing deployment invalidation
rather than a resource-demand change from different code.

The local comparison measured 100 requests per method, with 5% configured control
sampling. The released policy selected six controls and avoided 94 sizing calls;
94 requests consequently had no observed demand label. It recorded 207 state
reads and 213 RPC attempts versus 200 reads and 300 attempts for always-simulate.
No preflight or business-validation calls were counted as avoided.

| Method | Mean ms | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: |
| Always simulate | 51.14 | 51.03 | 53.69 | 54.74 |
| Released policy, including controls | 40.35 | 33.09 | 87.55 | 95.34 |

These are observed preparation spans, not a coverage-times-mean latency estimate.
The separate 500-row shadow phase had a 65.60 ms median, 167.35 ms p95 and
588.73 ms p99, but an anomalous **21,866.17 ms mean**. The local run spanned roughly
three hours and its retained summary cannot identify the individual pause/outlier
cause. Retain this anomaly; do not treat this run as a clean performance benchmark.
Both local comparison tail percentiles worsened. No production latency or
reliability improvement is established.

The next qualification step remains an application-owned repeated builder in
prospective shadow mode, with frozen chronological holdout and agreed risk and
latency targets. Compare static/formula/cache policies on varied account states
and deployment epochs, including control, watcher and storage overhead. Local
System-transfer tests establish integration behavior, not production risk,
customer demand or an advantage over a deterministic sizing rule.
