# Hybrid integration verification, 2026-09-27

This is a functional comparison on one disposable native Surfpool/LiteSVM bank
under WSL. It establishes that training, qualification, accepted predictions,
fallback, guarded refusal, execution and reporting work together. It does not
establish a performance winner or production reliability. The workstation was
not isolated: read-only diagnostic inspection ran concurrently during parts of
this integration check. Its timing and guarded refusals must not be treated as
a controlled comparison between algorithms or machines.

## Frozen experiment

- Node 24.14.1, Surfpool 1.5.0, embedded Solana core 4.1.2.
- Source fingerprint: 2c85d3e5adfec4f9c2fd5588dde9b3c9ad915e5672812e7f1dc4e4278a206382.
- Unchanged program ELF SHA-256: a1e04ed95beb488f8c9e28dd493c9a1e150d9439b3f835901bef4d785bebd027.
- Seed 20260927; CU ceiling 1,400,000; priority price zero; alpha 0.05.
- 80 fresh cohorts / 3,520 observations; 40 fitting, 24 calibration,
  16 qualification cohorts. Collection took approximately 19 minutes.
- 12 warmup, 12 ordinary measurement and four derivation-stress queues.
  Each queue requested 16 payments. Distinct recipients were used per arm.
- Original quantile profiles: 35/44 active. Hybrid profiles: 36/44 active.
- No deployment watcher failures in this complete collection.
- The owned bank was stopped after the run. Its qualification is not portable
  to another bank.

The final commit also contains a reporting-only correction after this run:
a current formula prediction that now requires simulation preserves its actual
reason, such as stale_state, rather than the generic prediction_artifact_mismatch.
No limits, acceptance checks, freshness policy, model parameters or trials were
changed. The source fingerprint above identifies the code used for the live run.

## Ordinary measurement phase

Only three queues per method were measured; this is too small to select a
winner. Completed-only timing must be read with the completion denominator.

| Method | Complete queues | Successful transactions | Accepted predictions | Estimation simulations | Run RPC calls | Mean completed queue, ms |
|---|---:|---:|---:|---:|---:|---:|
| adaptive | 1/3 | 2 | 2 | 0 | 52 | 2069.8 |
| adaptive_derivation | 3/3 | 6 | 6 | 0 | 90 | 2390.5 |
| always_simulate | 3/3 | 6 | 0 | 6 | 96 | 1768.1 |
| scoped_fixed_batch | 3/3 | 6 | 6 | 0 | 84 | 2361.8 |

The hybrid completed 48 measured payments and avoided six estimation
simulations, but was slower than always_simulate in this small local run.
These timings do not isolate the cause. No latency benefit was demonstrated. The fitted
fixed-batch baseline also skipped every estimation simulation and used fewer
run RPC calls. Formula accuracy alone cannot establish product value.

All three fully completed measurement arms paid 30,000 lamports in transaction
fees. The partially completed adaptive arm paid less because it performed less
work; that is not an economic advantage. Priority price was zero.

## Failures and boundaries

Across all phases, 23/28 queues completed and 376/448 payments were verified.
There were zero duplicate payments, zero failed or unknown submitted
transactions, and zero confirmed compute-budget exhaustions. There were
47 successful submitted transactions; execution metadata lacked loaded-account
size for all 47, so this run cannot claim observed execution-time joint-resource
coverage. Simulation labels did contain both resources.

Five queues stopped before signing their next transaction:

- adaptive: warmup/no-existing, measurement/half-existing,
  measurement/all-existing;
- adaptive_derivation: warmup/all-existing, stress/derivation-tail.

All five reported that pre-execution state required replanning. One hybrid queue
had already completed eight payments; incomplete outcomes remain in the report.
No retry, resampling, exclusion or parameter tuning turned these into successes.

For the examined hybrid warmup failure, the active artifact digest matched,
and recomputed predictions matched through state age eight slots. At age nine,
revalidation correctly returned stale_state. Candidate preparation/persistence
took approximately 672 ms. Surfpool's default clock can advance an expiry block
approximately every 75 ms even in transaction mode; see the
[clock investigation](hybrid-solana-20260927.md#deployment-snapshot-correction-found-during-native-validation).
The existing eight-slot guard was retained.

Before inferring real-network latency or comparing hosts, explicitly freeze
the simulator clock policy as well as machine and runtime configuration. A new
clock configuration requires fresh preparation for every arm; it must not be
changed midway to eliminate unfavorable trials. Planning/persistence costs and
bounded replanning deserve a separate optimization experiment.

## Final source checks

- Python: 641 tests passed, including freshness-boundary and altered-plan regressions.
- TypeScript: build and 53 tests passed; no TypeScript formula runtime was added.
- Ruff lint and formatting checks passed.
- Mypy passed all 22 core source files.
- Source distribution and wheel built successfully.
- One existing Starlette/httpx deprecation warning remains in the Python suite.

## Interpretation and reproduction

The hybrid is operational as an opt-in research candidate. It is not promoted
over the simpler approaches. Its global joint relative correction was 0.405625,
versus 0.937525 for the count-only ablation. These are calibration corrections,
not final error rates or performance improvements. The global correction may
overbudget larger prefixes; do not tune it on these comparison results and then
describe a rerun as untouched evaluation.

The complete local evidence remains under the ignored
artifacts/hybrid-verification-20260927-02 directory. Reports and public metadata
can be shared; private/ contains connection credentials and raw evidence and
must remain private. Only this compact result note is committed.

Use the [comparison guide](../hybrid-comparison.md) for the reproducible command,
longer repetitions, interruption handling and report fields. Repeat on fresh
banks and seeds, then measure an actual application's shadow traffic and remote
RPC latency before changing the default policy.
