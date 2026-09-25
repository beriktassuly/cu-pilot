# Paired resource profiles

The original `cu-pilot-pattern-estimator-v1` CU-only format remains available for
legacy inspection. The integrated path uses **`cu-pilot-resources-v1`**. Loading a
CU-only artifact with `ResourceEstimator.load` fails; there is no migration that
can manufacture absent loaded-data labels. Collect paired observations and refit.

`ResourceEstimator.fit(observations, ResourcePolicy(...))` produces validated,
non-executable JSON. `predict(features, context=..., current_slot=...)` returns the
existing `Prediction` type with both `compute_unit_limit` and
`loaded_accounts_data_size_limit`, or neither and a simulation reason. This model
is a statistical component: only the integrated message adapter can authorize an
actual skip after binding features, deployments, workload policy and lifecycle.

## Labels and chronology

Fit exactly one context and label source at a time. Historical CU comes from
`meta.computeUnitsConsumed`; simulation CU and bytes come from `unitsConsumed` and
`loadedAccountsDataSize`. No loaded-data measurement means no paired example.
Neither account count nor `costUnits` can substitute. Failure and missing-label
counts remain in diagnostics. A successful zero-resource label is valid, but the
prepared limit always rounds up to at least one configured rounding unit.

Observations and artifacts carry optional `label_source` and `evidence_origin`.
Old inputs without these fields retain their existing source semantics. Shadow
exports always fill them: synthetic execution and synthetic simulation remain
`source="synthetic"`, while `label_source` records historical versus simulation.
The origin separately identifies synthetic, local-runtime, live-simulation, or
historical-execution evidence. Fitting and evaluation reject mixed effective label
sources and mixed origins, including known/unknown origins. Replaying a recording
does not change its original origin; `collection_method="offline-replay"` records
how it was ingested. A synthetic origin cannot be relabeled as simulation evidence
in a resource artifact.

Observations can also retain optional `collection_method` (`prospective` or
`offline-replay`) and `collection_mode` (`shadow` or `deployment`). Shadow training
exports populate these from the persisted preparation trace when available;
manually ingested records and older datasets may leave them unknown. Historical
labels retain their original preparation provenance: reconciling execution does
not turn a shadow preparation into a deployment preparation. These metadata fields
do not change fitting or require artifact migration.

Deduplicate record IDs and reject conflicting duplicates before splitting.
Freeze chronological fit/calibration boundaries from **all** remaining rows,
before label, feature, or policy filtering. Evaluation freezes the outer test
boundary first. The configurable `independence_window_slots` groups nearby slots;
a whole window belongs to only one partition. Default one-slot windows avoid
counting many transactions in the same slot as independent evidence. Larger
windows can reduce burst inflation, but do not prove temporal independence.

Learn resource quantiles, numerical feature ranges, and instruction length ranges
from successful paired fitting rows only. Within each evidence window use the
maximum CU and maximum loaded bytes. These maxima can come from different paired
transactions: their union conservatively describes whether **any** transaction in
the window exceeds either limit. Never pair a CU-only row with another data-only
row to create an example.

Calibration includes only rows passing the same non-label feature, shape, support,
minimum-fitting-support and cap checks as prediction. Calibration freshness is
conservatively anchored to the fitting maximum slot; stale late rows do not rescue
old limits. Calibration support/risk gates themselves are evaluated after counting,
not used recursively to select calibration rows. Ineligible rows cannot dilute
failures or refresh profile evidence. Test data does not tune limits, ranges,
margins, sampling probabilities or risk targets.

## Joint risk and rounding

For each eligible paired calibration window count
`CU > final_CU_limit OR loaded_bytes > final_data_limit`. Calculate the one-sided
95% Wilson upper bound of this **joint** event, with distinct eligible windows as
the denominator. Two separate marginal bounds are not used as a joint guarantee.
The accepted prediction requires the joint bound at or below the configured
`max_joint_underestimation_rate`, enough fitting/calibration windows, and all other
eligibility checks. Marginal and joint failure counts are retained for inspection.

The research defaults are p99, 1,000 basis points (10%) margin per resource,
100-CU and 1,024-byte upward rounding, 30 fitting windows, 100 calibration windows,
and a 5% joint bound threshold. These are **not an approved production SLO**.
Margins and rounding are explicit policy, not a claim that 10% guarantees safety.
The exact formula is

```text
max(step, ceil(value * (10_000 + margin_bps) / (10_000 * step)) * step)
```

Python uses integer arithmetic and TypeScript uses exact integer calculations.
Calibration and evaluation use these final rounded limits. A proposed limit at or
above a protocol cap or the configured near-cap threshold cannot qualify. No
unsafe estimate is silently clamped. The adapter decides whether a valid
simulation-derived limit can resolve the fallback.

The Wilson bound describes the observed window sample under sampling assumptions;
it is not a guaranteed probability of future transaction success. Workload drift,
account changes and temporal correlation remain reasons to collect controls and
requalify. Report abstention and unknown shapes alongside error rates.

## Version semantics and portability

Every input must already contain both resource settings before extraction.
For legacy/v0 the adapter prepares the final ComputeBudget instruction topology.
For v1 resource fields belong in message configuration and unset resource limits
are zero. v1 does not use ComputeBudget instructions to set its limits, and its
priority fee is an absolute lamport amount. Official references checked 2026-09-24:
[compute budgets](https://solana.com/docs/core/fees/compute-budget),
[versioned transactions](https://solana.com/docs/core/transactions/versioned-transactions),
[simulation response](https://solana.com/docs/rpc/http/simulatetransaction).

`allow_v1` defaults to false. Enabling it is necessary but insufficient: a v1
pattern still needs complete paired fitting/calibration evidence and all adapter
and lifecycle gates. Successful parsing or a synthetic test cannot activate a
profile. Fees are preserved independently of limits; reducing v1 limits leaves
an unchanged absolute priority fee unchanged.

Artifact slot fields and `max_age_slots` are canonical unsigned decimal strings
in JSON, with uint64 bounds. Python holds integers and TypeScript uses `bigint`.
Range endpoints are bounded exact integers. The artifact contains no fees or
executable objects. `tests/fixtures/resource_contract.json` is explicitly synthetic
test evidence for accepted/fallback parity, null fields, freshness boundaries,
rounding, controlled resource replacement and unsupported versions. It must never
be activated as deployment evidence.

## Evaluation interface

`evaluate_resources` compares always-simulate, supplied fixed operation limits,
the caller's optional sizing callback, a last-successful cache with explicit
refresh age, per-pattern p95/p99, the joint statistical policy, and the complete
policy. The complete policy takes a `lifecycle_eligibility(features, slot)` callback
returning a fallback reason or `None`. Without that evidence its results explicitly
fall back; it does not pretend a statistical profile is a released deployment.

The cache learns only fitting labels initially. It refreshes from successful
fallback measurements after a slot ends; cache hits do not receive free labels.
Controls are deterministically sampled from request IDs and a seed before looking
at labels. The complete-policy replay represents one frozen profile revision, so
control failure state and suspension cover every pattern in that artifact. Replay
separate reports for different released profiles or revisions; the eligibility
callback cannot silently replace the release under evaluation. Set
`max_control_failure_streak` to the released manifest's value (default three).
Successful complete controls reset the streak. Failed or incomplete controls
increase it, and reaching the threshold suspends later decisions. Any known
successful resource excess also suspends, even if the other resource measurement
is missing; failed partial execution is not treated as demand. A later success
cannot undo suspension.

Apply control outcomes only after their slot ends, in stable `(slot, record_id)`
order, so none can affect an earlier or same-slot decision. This replay ordering
is an explicit assumption when actual outcome arrival order is unavailable.
Unselected requests provide no control outcome. Paired joint-risk denominators
remain separate from known partial excess and failed/incomplete control counts.
Report selected controls separately: they describe eligible selected
traffic, not the whole deployment population, and suspension changes that
population over time. The evaluator does not train from control labels.

Reports retain total, accepted, paired-scored, failed/unpaired accepted counts,
each resource's exceedance, joint exceedance, resource excess, failure reasons,
and per-profile fitting/calibration support and empirical bounds. Historical and
simulation sources are not mixed. A skipped estimate is not a skipped preflight,
wallet check or indispensable business-validation simulation.

Offline call savings are **counterfactual**. Shadow collection still simulates
every input. Control simulations are subtracted when reporting net counterfactual
calls avoided. No mean RPC time is multiplied by coverage to claim p95 savings.
Supply caller-measured `PreparationTrace` records to report p50/p95/p99 of the
complete preparation span, including builder preparation, state reads, deployment
checks, artifact refresh, simulation and logging. It separately counts estimation,
control, preflight and validation calls, state reads and artifact refreshes. Empty
trace lists report null latency percentiles, not invented measurements. Traces
must belong to the frozen test partition and match the observation's evidence
origin, collection method and mode. Equal record IDs alone do not establish this
relationship. Only the synthetic origin can be inferred from an older
`source="synthetic"` record; missing collection semantics are never guessed.
Older datasets remain evaluable without attached timing. Use `preparation_report`
separately for independently measured timing datasets that cannot establish this
join. Mixed evidence modes are rejected.
There is no claim about improved landing, network fees or production reliability.

Each proposed policy also reports configured priority fees by transaction version,
using its final CU limit and the original fee setting. Legacy/v0 use upward lamport
rounding of micro-lamports per CU times the requested CU limit, with the runtime's
uint64 saturation; v1 retains its absolute lamport fee. Totals are exact decimal
strings, including fees above JavaScript's safe integer range. These are calculated
configuration values, not observed charged total fees or savings. Fallback fees
remain unscored until the final configuration is known; base fees and precompile
signature charges are not inferred from the ordinary signature count. Sources:
[official fee semantics](https://solana.com/docs/core/fees/fee-structure) and
[Agave rounding and saturation](https://github.com/anza-xyz/agave/blob/v3.1.8/compute-budget/src/compute_budget_limits.rs).

`ObservationStore.preparation_traces()` exports measured collector spans around
input ingestion, frozen-plan durability, preparation, RPC, and the atomic result
and checkpoint commit. The final telemetry write occurs afterward and is excluded
from its own span; include that instrumentation write when measuring whole-process
throughput. A crash after completion but before telemetry leaves the trace missing.
Uninstrumented component timings stay null. Logical resource simulations and RPC
attempts are separate, so a rate-limit retry is not an extra transaction sample or
an additional avoided estimate. Collection method is included in timing reports;
offline replay timings do not describe live/local RPC latency even when the
recorded labels came from a local runtime. Reports reject mixed collection methods.

Collection keeps completed results immutable. Duplicate completion is idempotent;
a conflicting result from another local collector is retained as a conflict rather
than overwriting the first label. Execution reconciliation rebuilds the expected
final message from the frozen prepared message and recorded resource fields, then
verifies signed bytes. Per-signature outcome changes are auditable. One request
exports at most one historical training label (the first finalized successful
attempt, or first finalized failure if none succeeded), and the exported signature
count makes retries visible. Optional registry reconciliation records each verified
outcome revision and preserves suspension evidence after resource excess.

```python
from cu_pilot.data import load_observations
from cu_pilot.resource_evaluation import evaluate_resources
from cu_pilot.resources import ResourceEstimator, ResourcePolicy

rows = load_observations(observation_path)
model = ResourceEstimator.fit(rows, ResourcePolicy())
model.save(artifact_path)  # save outside source control; operator release is separate
report = evaluate_resources(rows)
```

Run the dedicated offline checks with
`uv run pytest tests/test_resources.py tests/test_resource_evaluation.py`.
The next model validation step is prospective paired shadow data from the
controlled builder across real account-state and deployment variation, followed by
a frozen holdout. Synthetic and local-runtime checks establish implementation
behavior, not production risk calibration.
