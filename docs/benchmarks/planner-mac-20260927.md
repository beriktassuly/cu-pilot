# Local Mac planner benchmark, 27 September 2026

The adaptive planner fixed unnecessary batch fragmentation after a profile was
suspended. It completed all 21 measured adaptive queues, but did not establish a
dependable latency advantage over always simulating. A separate offline feature
experiment reduced excess requested CU by 71.1%; it was not used for these payouts.

These are historical measurements of base revision
8771ef79554301fd7d21e3129a857d214cf977f6 plus the development patch identified in
[provenance](planner-mac-20260927-provenance.json). They do not measure subsequent
integration changes, the retired-version guard correction, or current demo defaults.
The experiment froze its profile versions, so the later version-transition defect
did not change these recorded comparisons.

## Workload and measurement

- One Apple M5 Mac, 16 GiB RAM, native ARM Python 3.12.14 and Node 24.14.1,
  macOS 26.6.2, Surfpool 1.5.0 reporting solana-core 4.1.2.
- Local Solana transactions only, 16 payments per queue, with all, none or the
  first half of recipient token accounts already present. Each method received
  fresh addresses and equivalent shapes, rather than identical transaction inputs.
- A fresh bank supplied 3,520 training/calibration/holdout observations. The same
  fitted parameters were frozen across four separately qualified method registries.
  Two historically failed state cells were suspended in every registry.
- The natural phase used five repetitions of each method/scenario: 60 queues.
  Twelve randomized warmup queues were excluded. A separate post-quarantine phase
  used two repetitions: 24 queues. The phases are reported separately.
- Every method had a 1,400,000 CU application ceiling, 1 MiB loaded-account-data
  ceiling, zero CU price and the same executor allowance. The measured source's
  default was 100,000 CU; the experiment explicitly overrode it. Changing the
  ceiling changes batch feasibility and does not reproduce these results by itself.
- Completion time includes planning, guards, RPC work, submission, confirmation
  and persistence. Setup/account creation and later diagnostic reads are excluded.
  Fees include failed execution attempts; rent and setup costs are separate CSV
  columns. There were no real funds or measured dollar savings.

The natural method labels map to the application strategies as follows:
qualified_learned is the original learned strategy; adaptive is the new planner;
always_simulate simulates each chosen batch; fitted_fixed_eight is the scoped
fixed-batch strategy with a fitting-only eight-payment constant and safe fallback.

## Planner results

All 84 measured queues completed, paying 1,344 recipients with zero duplicates.
There were 194 transaction attempts: 192 successful and two confirmed CU failures,
both recovered by the normal bounded retry flow. There were no unknown outcomes
or transport retries. The [planner CSV](planner-mac-20260927.csv) preserves the
original aggregate values without rounding.

| Phase | Strategy | Completed queues | Mean / median / p95 completion, ms | Attempts (failed) | RPC calls | Simulation calls | Execution fee, lamports |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Natural | Original learned | 15/15 | 287.67 / 295.43 / 417.47 | 41 (1) | 607 | 3 | 205,000 |
| Natural | Adaptive | 15/15 | 222.13 / 220.68 / 243.81 | 30 (0) | 448 | 2 | 150,000 |
| Natural | Always simulate | 15/15 | 226.00 / 225.80 / 241.11 | 30 (0) | 476 | 30 | 150,000 |
| Natural | Scoped fixed eight | 15/15 | 219.49 / 215.97 / 287.62 | 31 (1) | 487 | 29 | 155,000 |
| Post-quarantine | Original learned | 6/6 | 452.38 / 414.02 / 617.12 | 26 (0) | 376 | 2 | 130,000 |
| Post-quarantine | Adaptive | 6/6 | 245.34 / 248.55 / 259.34 | 12 (0) | 192 | 12 | 60,000 |
| Post-quarantine | Always simulate | 6/6 | 234.52 / 239.53 / 245.09 | 12 (0) | 188 | 12 | 60,000 |
| Post-quarantine | Scoped fixed eight | 6/6 | 226.76 / 230.59 / 231.67 | 12 (0) | 190 | 12 | 60,000 |

In the natural phase, adaptive accepted 28 learned estimates and performed two
control simulations. Compared with always-simulate, it used **93.3% fewer
simulation calls**, **5.9% fewer total RPC calls**, and **1.7% less mean time**
(3.87 ms per queue), with equal execution fees. The small timing difference does
not establish a robust speed advantage. Separate addresses and lifecycle histories
also prevent interpreting zero adaptive failures as proof of lower failure risk.

The fixed-eight constant was 318,000 CU, fitted on the new bank. A confirmed failure
invalidated it; this arm then used 29 simulations across 31 attempted transactions.
Its timing is not evidence that an unsimulated fixed constant is universally safe.

For the post-quarantine phase, the union of natural suspensions was applied to all
registries and the eight-payment/all-existing state cell was explicitly suspended
through the public API. No failure was fabricated or profile reactivated. The
original planner used batches of 2, 2, 2, 2, 2, 6 for both all-existing repetitions;
adaptive simulated batches of 8, 8. Across this phase, adaptive reduced mean time
by **45.8%** and transactions and fees by **53.8%** relative to the original planner.
It was still **4.6% slower** than always-simulate and used the same number of
transactions and simulations. This supports a planner correction rather than a
claim that prediction outperforms simulation.

CU and fee metadata were available for all 194 confirmed attempts. CU consumption
at exhaustion is censored and does not reveal full demand. Confirmed execution
metadata provided no loaded-account-data consumption labels, including for the two
failures. The CSV's missing-label column counts successful transactions only (192);
the missing count across all attempts was 194. Simulation labels do not fill those
execution-label gaps.

## Separate derivation-feature experiment

A later fresh bank tested public-input ATA derivation effort, using 256 minus the
public bump as a count of one derivation search per recipient. This is not a count
of all runtime operations. The formula, margins, sample count, split and policy
were frozen before collection; the feature module was not imported by the planner.

The 3,520 successful paired simulation observations were split chronologically by
cohort: 1,760 rows from 40 fitting cohorts, 1,056 from 24 calibration cohorts, and
704 from 16 holdout cohorts. Queue/snapshot/overlapping-slot groups and recipient
sets were disjoint across partitions. The feature collection also had no recipient
overlap with the earlier bank.

| Offline formula | Accepted holdout rows | Mean excess CU | Median excess CU | p95 excess CU | Observed CU / data / joint exceedances |
| --- | ---: | ---: | ---: | ---: | ---: |
| Count and missing accounts | 704/704 | 98,413.62 | 98,997.5 | 129,274 | 0 / 0 / 0 |
| Count, missing accounts and derivation effort | 704/704 | 28,430.81 | 27,988.5 | 38,948 | 0 / 0 / 0 |

The [feature CSV](derivation-mac-20260927.csv) preserves the original aggregate
values. Mean excess requested CU fell by **71.1% on the same 704 observations**.
This measures reserved headroom, not reduced computation, latency or paid fees.

Both formulas passed 44 state cells under the experiment's policy. That policy used
a joint CU/data calibration risk-bound threshold of **0.15** and at least 20
independent paired calibration cohorts per cell. This permissive research setting
is not a production target. The 704 holdout rows represent only 16 independent
cohorts; zero observed exceedances does not establish a rare-failure guarantee.
The artifact has no release authority and is not loaded by the payout application.

## Preservation and next use

The compact CSVs and [sanitized provenance](planner-mac-20260927-provenance.json)
are sufficient to inspect the reported aggregate arithmetic. Full event logs,
signed messages, snapshots, observation datasets and frozen artifacts remain in
the original archive, outside Git. The recorded hashes identify that historical
evidence; they do not provide a public raw-data download. Git normalizes the copied
CSVs to LF line endings; provenance records archive and committed CSV hashes
separately, with unchanged aggregate values. Saved registry snapshots
are evidence, not authorization to release a profile in a new runtime.

The planner bank reused 2,880 public recipient wallet seeds from the prior local
collector; mint, ATA state and bank were fresh. The separate feature bank used new
recipient namespaces. Neither experiment isolates hardware speed, establishes
remote-RPC behavior or demonstrates mainnet performance.

For the next evaluation, retain these holdout results unchanged, collect new
failure-prone states and execute transactions using the proposed feature limits
under fresh qualification. Compare adaptive with always-simulate at equal workload,
resource ceilings and lifecycle policy, then measure realistic remote-RPC latency.
See [adaptive planning](../adaptive-planning.md) and
[derivation features](../derivation-features.md) for implementation boundaries.
