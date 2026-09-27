# Adaptive payout planning

The optional `adaptive` strategy chooses the largest currently verified payment
prefix that fits the application constraints. It can simulate a larger prefix
even when a smaller learned prediction is eligible. This addresses the case where
a suspended eight-payment profile previously caused many two-payment transactions.
It preserves the original `learned` strategy and the CLI default strategy selection.

Use the existing application interface with an already running isolated local
bank and an owner-approved queue:

```sh
.venv/bin/python -m examples.payouts.app run \
  --directory artifacts/payouts --queue QUEUE_ADDRESS --method adaptive
```

Python callers use `Application(state_directory).run(queue_address, "adaptive")`.
An explicit application ceiling can be supplied as
`Application(state_directory, compute_unit_cap=1_400_000)`. The CLI equivalent is
`--compute-unit-cap 1400000` on `run`, `step` or the demo server. Values must be
integers from 1 through 1,400,000; the default remains 100,000 CU. The account-data
ceiling stays 1 MiB. Each strategy uses the same configured ceiling, records it in
its decision journal, and requests only its estimated/simulated limit beneath it.
A higher ceiling does not authorize a stale model or force every transaction to
request that ceiling.

In the browser, choose **Adaptive · estimate with fallback** explicitly. The default is
**Always simulate**. The page displays the configured ceiling and each decision's
actual strategy, estimate source, fallback reason, candidate probes and RPC counts.
The worker keeps the selected strategy for that run. Previously signed decisions
are reconciled with their original settings before any newly configured plan.
The state directory must contain this bank's private runtime connection and its
own observation journal. A new bank cannot inherit authorization from an old
model, connection file or registry snapshot. A missing or ineligible model causes
fresh simulation, not automatic activation. No on-chain program change is needed.

## Selection policy

The objective is the largest verified contiguous prefix, rather than the fewest
simulation calls. Minimizing transaction count does not universally minimize
latency or cost. The mode uses no exchange rate or assumed RPC price.

Candidate construction already reads one common state snapshot and uses a common
blockhash for the existing menu. Each prefix has its own exact serialized message,
identity and frozen decision. The menu has at most four counts: 8, 4, 2 and 1,
or an exact final tail replacing 8 when fewer than eight payments remain.

The adaptive strategy examines that menu from largest to smallest:

1. A candidate must have supported account state and fit the wire, account and
   transaction-version constraints. Unsupported state stops planning.
2. Use an eligible learned prediction if its limits fit both resource ceilings.
   The core still rechecks release eligibility and performs any selected control.
3. Otherwise simulate that exact candidate. Record the result before considering
   another candidate. Accept only a successful result with both resource limits
   inside the application ceilings.
4. Consider a smaller candidate only after structural infeasibility or successful
   paired simulation evidence shows the larger candidate exceeds a resource cap.
   Stop at the first verified feasible candidate.

There are at most four resource-simulation calls per selection attempt, including
controls. The RPC client's existing bounded transport retry policy remains in
effect and RPC attempts are counted separately. An unresolved simulation does not
prove that a smaller transfer is safe. Semantic transaction errors, missing
measurements, stale evidence and invalid message binding stop the adaptive search.
Failed high-budget simulations are not reclassified as resource pressure from
their partial CU consumption.

After selection, the original application checks the state snapshot and its age,
deployment fingerprints and applicable release identity again before signing.
The signer receives the selected decision's exact resource-adjusted message.
State mutation requires a new plan. The existing spending allowance, bounded
failed-transaction recovery, signature reconciliation and duplicate-payment
checks remain in effect. Unknown submission outcomes must be reconciled before
another transaction is prepared.

Simulation does not reactivate a suspended profile. A simulated decision may
retain the original profile identity as provenance while its status remains
`simulation_success`. Quarantine and control/execution history remain durable.

## Estimate sources and fitted constants

The normal adaptive path considers only qualified learned predictions and fresh
simulation. Fitted constants remain explicit opt-in strategies. Their source is
recorded separately from a released learned profile. `fixed_batch` retains the
legacy policy of choosing an estimated prefix at or below its fitted count cap.
It can shrink when the large constant becomes ineligible.

The new `scoped_fixed_batch` method uses exactly the fitted count, or the remaining
tail when fewer payments remain. If its fitted constant is ineligible, it simulates
that same prefix instead of switching to a smaller constant. If the fixed prefix
cannot fit the caps, it stops. Select it with `--method scoped_fixed_batch`.
This is the strong fixed-eight comparator when fitting selects eight. Both fixed
methods share the same artifact/count invalidation key, so switching their names
cannot bypass a recorded failure. `formula` remains a separate fitted source.

A fitted estimate needs the matching frozen bundle, runtime, deployment, exact
supported state and sufficiently fresh fitting evidence. It cannot bypass the
corresponding profile's suspension or retirement. Lifecycle checks bind the frozen
revision and artifact digest even when another revision is active; renumbering a
quarantined artifact cannot authorize it. A genuinely new independent fitted
artifact needs calibration later than the recorded suspension and current
deployment evidence. No active learned release is required for that separate
baseline. After confirmed resource exhaustion, the
application durably invalidates that fitted artifact/method/count for subsequent
queues as well as forcing simulation for the failed queue. A historical p99
constant is not a universal upper bound, and changing its label does not clear
invalidation or authorize an old suspended learned limit.

The CLI default remains `learned` for compatibility. Existing strategy names retain
their selection policies. Unknown strategy names are rejected instead of silently
acting like always-simulate. Fitted-source scope and invalidation checks are an
intentional safety tightening, and comparisons must identify that difference.

## Audit and measurement

The observation journal freezes every candidate before receiving simulation
labels. `payout_planning_attempts` retains the adaptive policy, rejected candidates,
selected source, probe outcomes, resource simulations, controls and actual RPC
method counts, including unsuccessful searches. Submitted step records retain the
chosen candidate and the usual confirmation/fee evidence. The planning audit is
not proof that a transaction was signed, sent or completed.

Report all extra probes and controls in total execution work. Queue timing must
include normal planning, reads, guards, signing, submission, confirmation and
persistence. Keep setup and diagnostic verification separate, preserve original
failed outcomes and report latency conditional on completion. Lower reservations
do not imply lower fees when CU pricing is zero.

Planner evaluation must freeze model parameters and use comparable starting
eligibility across methods. Separate registries can be explicitly qualified in
one valid bank scope, but each arm must retain its own failure history. A dedicated
post-suspension scenario should use the public lifecycle API with the actual slot
and an honest operator reason. It must not fabricate a resource failure or reset
quarantine after an unfavorable result.

Address-derivation features are a separate research milestone. The adaptive planner
does not change the model's feature contract, quantiles, support thresholds or
calibration policy. Any feature experiment needs versioned artifacts and fresh
cohort-separated fitting, calibration and holdout evidence before it can support
a deployment claim. Local runtime results do not establish mainnet or remote-RPC
performance.

The [historical Mac comparison](benchmarks/planner-mac-20260927.md) preserves the
pre-integration measurements and their exact denominators. Later lifecycle and
demo changes are tested separately and do not inherit those latency measurements.
