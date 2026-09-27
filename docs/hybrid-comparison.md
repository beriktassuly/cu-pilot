# Run the experimental hybrid comparison

The opt-in adaptive_derivation method combines an NNLS formula, joint cohort
calibration and the existing adaptive planner. The original adaptive method
remains a separate comparator. Read the
[formula decision](research/hybrid-formula-20260927.md) and
[Solana source review](research/hybrid-solana-20260927.md).
This is a local experiment, not a production reliability claim.
The first [native verification](research/hybrid-verification-20260927.md) exercised
all four methods and retained guarded failures; it did not demonstrate a latency
win over local simulation.

## Setup and one-command comparison

Use Linux/WSL with the pinned dependencies and compiled payout program:

~~~sh
bash scripts/payouts.sh bootstrap
bash scripts/payouts.sh build
bash scripts/payouts.sh compare all \
  --directory artifacts/hybrid-comparison-01 \
  --groups 80 --repeats 10 \
  --compute-unit-cap 1400000 --seed 20260927
~~~

The output directory must be new; use ignored artifacts/. The command starts a
separate offline Surfpool bank, supplies disposable fixture funding, collects
fresh observations, fits both methods, qualifies separate profiles and runs the
frozen comparison. It stops its bank after the measurement stage. It never submits to a
public network. A failed preparation preserves its bank and durable collection
so the same frozen campaign can resume; stop it explicitly if abandoning it.

Defaults: 3,520 collection observations in 80 cohorts; 12 warmup queues;
120 measured queues; four separate high-derivation stress queues. Each queue has
16 payments. Collection can be substantially slower than final comparisons.
These are not 3,520 independent trials.

The CU price is zero: this tests reservations, RPC and timing behavior, not
priority-fee savings.

## Separate preparation and measurement

~~~sh
bash scripts/payouts.sh compare prepare \
  --directory artifacts/hybrid-comparison-02 \
  --groups 80 --repeats 20 --seed 20260928

bash scripts/payouts.sh compare run \
  --directory artifacts/hybrid-comparison-02 --maximum-cases 12

bash scripts/payouts.sh compare run \
  --directory artifacts/hybrid-comparison-02

bash scripts/payouts.sh compare report \
  --directory artifacts/hybrid-comparison-02

bash scripts/payouts.sh compare stop \
  --directory artifacts/hybrid-comparison-02
~~~

The first bounded run executes warmups; the next resumes the frozen schedule.
Interrupted cases stay in the report without invented completion times.
Changed source/artifacts/manifest or a restarted bank require a new campaign.
Old-bank qualification is never transferred to another bank.

If preparation fails or is interrupted after bank startup, resume it without
changing its frozen settings:

~~~sh
bash scripts/payouts.sh compare prepare \
  --directory artifacts/hybrid-comparison-02 --resume-preparation
~~~

Resumption requires the same live bank, source and configuration. It records all
preparation attempts in preparation.json. This is bounded recovery: an unfinished
observation whose frozen state became stale still rejects training; it is never
silently relabeled with fresh state. That case, or a dead or restarted bank,
requires a new directory and fresh evidence. Use the stop command when abandoning an
incomplete campaign; the comparison never kills an unrelated process.

Prepare freezes seed, schedule, source/compiled-program hashes, caps, alpha and
model choice before final comparison queues. Each arm has a separate lifecycle
database; within an arm its suspensions persist throughout the campaign.

## Outputs and methods

- report.md: readable comparison table.
- metrics.csv: per-method/scenario summaries, with warmup and stress separate.
- report.json: failures, missing labels, coverage, controls, RPC accounting and
  paired latency differences.
- manifest.json and plan.json: hashes, runtime, qualification and frozen choices.
- private/collection/: observations and candidates, including count-only-candidate.json.
- private/arms/: per-method profiles and durable transaction journals.
- private/cases/: individual trials, including failed and interrupted cases.

Share the reports and public manifest/plan. Do not publish private/: it contains
local connection credentials and large raw evidence. Generated files stay out
of Git.

| Method | Source |
|---|---|
| adaptive | Existing state-conditioned quantiles or simulation |
| adaptive_derivation | New qualified five-term formula or simulation |
| always_simulate | Fresh simulation |
| scoped_fixed_batch | Fitted fixed batch with scope/lifecycle guards |

Both adaptive methods retain 5% sampled controls. The always-simulate arm
simulates each estimate; the existing fitted fixed-batch policy has execution
quarantine but no added random control sampling. Those policy costs are included,
so this is not an equal-control-budget comparison.

All arms share declared caps and scenario/payment counts. Recipients are fresh
and distinct, not identical inputs. Order is randomized within matched scenario
blocks. Pure address caches are cleared before each queue and reused within it.

Stress fixtures deliberately search for an ATA with at least eight derivation
attempts. They are excluded from ordinary performance summaries. Such a fixture
is not necessarily outside fitting support; actual attempts and decisions are
saved.

## Interpreting results

Check completed payments, duplicates, failures, unknown outcomes and profile
coverage first. Then compare complete queue time and all RPCs.
Completion-conditioned latency omits interrupted cases; inspect its denominator.

Loaded-account size can be missing from execution metadata and is never replaced
with zero. Compute-exhausted executions have censored demand, not a true usage
label equal to the limit. Training, qualification, fixture funding and setup
are separate from queue completion. RPC totals cover each case, including its
creation and validation; collection, qualification and runtime startup are
separate campaign overhead.

The permissive local release gate does not certify a rare production failure
rate. The count-only ablation is fitted and frozen for analysis; it is not an
extra live arm in the default four-arm campaign.

Repeat the entire campaign on several fresh banks/seeds before selecting a
winner. Do not tune on test results and call a rerun untouched evaluation.
A later real-application shadow experiment is needed to assess remote RPC value.

## Use an existing local collection

Training creates a candidate; it does not activate it:

~~~sh
.venv/bin/python -m examples.payouts.hybrid \
  artifacts/payouts/observations.jsonl \
  --output artifacts/payouts/hybrid-candidate.json --alpha 0.05

.venv/bin/python -m examples.payouts.app qualify-hybrid \
  --directory artifacts/payouts

.venv/bin/python -m examples.payouts.app run \
  --directory artifacts/payouts --method adaptive_derivation \
  --compute-unit-cap 1400000
~~~

The bank and deployments must still match the evidence. Alpha 0.01 with only
24 calibration cohorts must abstain: a finite 99% conformal bound needs at least
99 calibration cohorts. More correlated rows do not fix that shortage.

This is a Python payout integration. The TypeScript quantile runtime does not
execute the new formula artifact. UI integration and a public-network release
are separate work.
