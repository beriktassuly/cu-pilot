# Autonomous payout reference application

CU Pilot remains an unsigned transaction resource estimation and planning library.
This application uses it to choose the next prefix of an owner-approved, immutable
queue of classic SPL Token payments. A native Rust program enforces payment
authority; the application supplies ephemeral local test signing keys. The mint
is labelled **CU Pilot test token**. It is neither USDC nor evidence of a real
asset backing the token.

The scope is one mint, one owner, one executor and at most 16 distinct recipient
wallets per queue. There are no swaps, yield strategies, transfer hooks, arbitrary
CPI targets or hosted accounts. The implementation uses native Rust rather than
Anchor because these four instructions need no IDL or additional Anchor tooling.
See the [program specification](../programs/payout_queue/README.md) for exact
instruction bytes, account order, state offsets and authority checks.

## Architecture and authority

```mermaid
flowchart LR
    O[Owner reviews recipients and amounts] --> Q[Create queue and fund PDA vault]
    Q --> S[Read queue and recipient ATA state]
    S --> C[Build eligible contiguous prefixes]
    C --> M[Learned state-conditioned resource profiles]
    M --> P[Deterministic fixed-budget planner]
    P --> B[Core message binding and release checks]
    B --> F{Qualified and fresh?}
    F -->|yes| E[Application executor signs exact message]
    F -->|no| V[Bounded simulation or pause]
    V --> E
    E --> T[Local Solana program validates immutable terms]
    T --> X[Token transfers and atomic cursor update]
    X --> A[Verify balances and reconcile signature]
    A --> S
```

`src/cu_pilot/` still owns parsing, generic features, exact-message binding,
resource quantiles, simulation fallback, immutable profile releases, controls and
the observation journal. Its only application-driven extension is the optional
`ResourceEstimator.fit(..., calibration_boundary_slot=...)` argument, which
preserves a caller's already frozen grouped split. Its default remains unchanged.
It rejects a boundary that divides an evidence window. The generic SDK/API/CLI
does not sign or submit transactions.

`examples/payouts/` owns queue/state semantics, candidate selection, training
partitions, the local worker, its control service and reconciliation. The Node
bridge in `apps/payout-demo/` reuses the existing TypeScript message builder and
Surfpool runtime. It loads the compiled payout ELF into an isolated offline bank,
creates the test mint and keeps signing keys in its process. Browser responses
contain public identities and observed state, never secret keys.

The owner signs queue creation and funding. The program stores the executor,
mint, ordered recipients, integer amounts and expiry. Execution accepts a count,
expected cursor and audit identifiers; it cannot accept replacement beneficiaries
or amounts. Only the designated executor may advance the queue. The program
derives the vault and each destination ATA, validates token state, pins classic
Token/ATA/System programs and updates progress only after all transfers succeed.
Transaction atomicity rolls back earlier transfers and ATA creation on failure.
Duplicate recipients are rejected at creation.

Owner pause/resume preserves the funded terms. Owner refund after expiry enters
a terminal state. Completed and refunded queues remain durable tombstones, so a
queue identity cannot be reused to replay payments. Decision and model digests
record provenance; they prove neither correct inference nor debit authority.
The executor's token authority does not cap its separate SOL fees and ATA rent.
The application gives it a bounded local allowance and bounds failed attempts.
The local executor starts with 100 million lamports. The worker checks a
50-million-lamport per-queue spending threshold and a 20-million-lamport balance
floor before acting. These are stop thresholds, not an exact fee/rent escrow:
the final admitted transaction can cross a threshold. Fixture allowance resets
are explicit benchmark setup, never an automatic retry mechanism.

## Supported local environment

Use Linux x86_64 glibc or WSL Ubuntu on Windows. The bootstrap script pins Node
24.14.1, public Agave 3.1.10 build tooling and platform-tools v1.52 (SBF Rust 1.89.0);
the checked-in package and
Cargo lockfiles pin dependencies. Program interfaces use `solana-program 2.3.0`
and classic `spl-token 8.0.0`. The bridge uses Solana Kit 8.3.0 and Surfpool 1.5.0
with offline/default feature gates. The measured runtime identifies itself as
Solana 4.1.2, feature set 3345198602. The application executes legacy transactions;
the reusable core's separately tested v0/v1 support is unchanged. No application
v1 compatibility is asserted.

Run all payout commands inside the same Linux environment. Windows Node cannot
load Linux native Surfpool, and Windows `uv` must not operate on a WSL `.venv`.
For WSL, open a Linux terminal and change to the checkout under `/mnt/c/...`, or
use a Linux-native checkout for faster dependency installation. The Python
environment is repository-local; no public RPC credentials or real wallet are
needed.

The verified Windows route is WSL Ubuntu 24.04.4, glibc 2.39 and Python 3.12.3.
A separate clean source copy successfully bootstrapped, built and executed the
program using public Agave 3.1.10. Its ELF matched the initial Agave 3.1.15 build.
The build script passes the platform compiler explicitly with `--no-rustup-override`
because older distribution `rustup` rejects the SBF custom toolchain name. Native
Rust 1.98.0 ran the local Rust tests; CI pins host Rust 1.95.0 and builds the ELF
with the separately pinned SBF compiler. These are local build/runtime checks,
not a statement of compatibility with arbitrary validator releases.

From the repository root:

```sh
bash scripts/payouts.sh bootstrap
bash scripts/payouts.sh build
bash scripts/payouts.sh test
bash scripts/payouts.sh start
bash scripts/payouts.sh info
```

`build` compiles a real SBF ELF; `test` executes it in the local runtime. Missing
prerequisites fail the command. `start` deploys the ELF to a new isolated runtime
and initializes local identities and the test mint. The private connection token,
databases, datasets and artifacts stay in ignored `artifacts/payouts/`. The runtime
is local deployment, not publicly accessible devnet deployment.

If a terminal host terminates background WSL processes when a command ends, run
`bash scripts/payouts.sh serve` in a persistent WSL terminal instead of `start`.
Keep that terminal open and run collection, the worker and the page in another
WSL terminal. `stop` supports either launch and verifies the process's executable
arguments and checkout before signaling it. Windows-native SBF/Surfpool execution
is not part of the supported workflow.

The bundled classic Token/ATA programs carry upstream deployment slots. Runtime
setup installs their unchanged ELF bytes locally and records the original headers
and local installation slot 100. Profile fingerprints are measured only after
that disclosed fixture setup; a source-cluster slot is not claimed as local
deployment evidence.

## Collection, learning and local qualification

```sh
bash scripts/payouts.sh collect --groups 80
bash scripts/payouts.sh train
bash scripts/payouts.sh qualify
```

The collection schedule has 80 queue groups. Each group contains independent
queues for candidate counts 1 through 8 and measures every existing/missing ATA
count for each candidate. Groups rotate through nonterminal menu prefixes on a
16-payment queue, exact complete queues, and queues with one already executed
obligation. Counts outside the regular menu are always exact tails. The third
group class first executes a real one-payment prefix with its ATA already present;
that setup execution remains in the journal and is excluded from model labels.
It creates real ATAs between simulations rather than adding artificial compute.
There are 44 candidate state cells per group. The completed 2026-09-25 collection
produced 3,520 distinct successful paired simulation observations, with no duplicate
records or missing resource labels. Seventeen development observations had stale
pre-execution state and were excluded from fitting/calibration. The journal retains
such evidence; future failures or missing measurements never become invented labels.

Recipient identities, exact state snapshots, runtime/deployment identities, slots
and message features are persisted with each observation. Queue identity uses a
deterministic schedule scoped to the runtime instance. Collection recipient
identities use public fixture seed `1729` and namespace `payout-collection-v1`,
indexed by group, count and recipient position; these addresses provide no signing
authority to the model. This is a public test-only fixture seed, never an
owner/executor credential. Owner/executor/mint identities remain ephemeral. Rerunning reproduces
the recipient schedule and partition policy, not a claim of byte-identical runtime
keys, timings or resource labels.

The versioned pre-execution envelope records count, cursor, remaining obligations,
missing/existing ATA evidence, relevant account sizes, initialization/frozen state,
runtime and full deployment identities, observation slot, approved-terms digest
and prepared-message identity. Its digest covers exact identities. Addresses,
outcomes, logs, consumed resources and post-execution balances are not statistical
features. The model key contains exact count, missing ATA count and supported
account-size class; different recipient identities must generalize through those
features. A never-observed count/state receives no confidence from a nearby count.

Related queue identities, snapshot identities and overlapping slot intervals are
grouped before a chronological 50% fit, 30% calibration and 20% holdout split.
Boundaries freeze before filtering labels or unsupported state. The completed run
split 40/24/16 groups into 1,760/1,056/704 rows before filtering. Six fitting and
eleven calibration rows were stale, leaving 1,754 fitting and 1,045 calibration
observations, with 39–40 fitting and 23–24 calibration windows per cell. All 704
holdout rows remained paired. Future runs reproduce the schedule and split rule;
timings, slot grouping, exclusions and qualification can differ. The final holdout
never chooses parameters or margins.

The learned estimator is a set of state-conditioned paired empirical quantiles.
Each cell fits actual CU and loaded-data p99 values, then applies the declared
10% margins and upward rounding of 100 CU and 1,024 bytes. Calibration checks the
joint event `actual CU > limit OR actual loaded bytes > limit`. This reuses the
existing `cu-pilot-resources-v1` artifact and estimator; it does not introduce a
second inference or lifecycle engine.

The local demonstration policy is explicitly weaker than the reusable research
default: at least 12 fitting windows, 20 calibration windows and a one-sided 95%
Wilson joint upper bound at most 0.15. The core default remains 30/100 windows and
0.05. Neither is a production rare-failure guarantee. A single correlated local
workload cannot establish such a guarantee.

Training creates a candidate bundle. Only the separate `qualify` operation
registers immutable per-cell profiles, enters shadow and requests activation
against refreshed deployment evidence. The closure includes the payout program,
classic Token, ATA, System and Compute Budget. Stable profile IDs preserve
quarantine; a new artifact revision does not erase a confirmed underestimate.
Qualification failures are reported per cell. Synthetic test fixtures cannot
qualify the local application.

Simulation uses `unitsConsumed` and `loadedAccountsDataSize`. Execution uses
historical `meta.computeUnitsConsumed` when present; missing historical loaded
data remains missing. Simulation and execution from one decision are not two
independent training samples. The default learner fits paired simulation labels.

## Planning and demonstrated model influence

The menu is 1, 2, 4 and 8, plus an exact final tail of 3, 5, 6 or 7. Every method
uses the same declared constraints: 100,000 CU, 1,048,576 loaded bytes and 1,232
serialized bytes per transaction. The CU cap limits a transaction's compute
reservation well below the protocol maximum; it is an application scheduling
choice fixed before workload collection. The loaded-data cap is expected to be
nonbinding for this small program. An advantage under these constraints does not
establish an advantage under every reasonable policy.

The evaluation also describes frozen holdout label capacity at 100,000 CU versus
the 1,400,000-CU protocol ceiling, retaining the same loaded-data cap. It reports
successful paired-label denominators and maximum observed consumption by count.
This comparison changes no training, qualification or execution policy and runs no
additional transactions. If valid batches of eight fit a larger reservation,
ordinary fixed batching can complete sixteen obligations in two transactions;
gains under the chosen scheduling cap do not establish an economic advantage.

The planner selects the largest eligible prefix with conservative estimates under
both resource caps. Unsupported counts/state, stale observations, missing paired
support, failed deployment checks and unreleased/quarantined profiles require
bounded simulation, a smaller verified candidate or a pause. Failed simulation
is never a usable estimate. The final unsigned transaction remains bound to its
queue, count, cursor, audit IDs and instructions before the application signs it.

The causal ablation replaces model estimates with the predeclared formula
`CU = 20,000 + 25,000 × count`, loaded bytes = 1 MiB. This intentional intervention
is separate from the tuned fixed-batch competitor. The benchmark must execute
both paths from equivalent fresh states and show different chosen **and actually
executed** counts, matching balance changes and cursor increments. A prediction
chart, recorded digest or fallback-only execution does not establish that chain.

The comparative baselines are a fixed batch tuned only on fitting observations,
a fit-only three-term packing formula (intercept, count, missing ATAs), a refreshed
cache, marginal per-pattern p99 across ATA states and always-simulate. The simple
formula includes its maximum positive fitting residual and the declared margin.
Cache entries refresh only after paid successful paired simulations and expire
after 40 slots or eight selected uses. Candidate lookups do not receive labels.
A simple competitor may match or outperform the conditional quantile model.

An additional deployment check runs in its own isolated bank:

```sh
bash scripts/payouts.sh upgrade-test
```

It measures 40 distinct one-payment queues with existing ATAs, freezes a 20-fit /
20-calibration split, and explicitly qualifies its own local lifecycle profile
using the same 0.15 joint-risk threshold. There is no holdout accuracy claim in
this integration and no reuse or retargeting of the main demonstration artifact.
Its explicitly wider 50% CU padding keeps this lifecycle regression independent
of optimization quality; a preliminary 10%-padding candidate correctly failed
release after one of 20 calibration queues exceeded its limit. The main workload's
10% policy and comparative evaluation are unchanged.
The test executes a qualified prediction, performs a real signed Loader-v3 Token
upgrade with identical code bytes and a new deployment slot, confirms that only
the dependency fingerprint changed, and observes profile suspension. It then
executes a simulation fallback and verifies both recipients received exactly
1,000 test-token units, with each queue cursor/paid count 1 and vault balance 0.
The test seeds a local upgrade authority and buffer before collecting labels;
the upgrade itself is an executed Solana instruction. This fixture is unavailable
through the normal bridge unless explicitly enabled by the isolated test process.
`artifacts/payouts/deployment-check.json` links the signatures, actual outcomes and
the separate profile database, observations and frozen decisions. It does not
change the running demo bank or its qualified artifact.

## Headless execution and the page

```sh
bash scripts/payouts.sh run --length 16 --existing 8 --method learned
bash scripts/payouts.sh benchmark
bash scripts/payouts.sh demo
```

Open `http://127.0.0.1:8787`. The page reviews recipient addresses and integer
amounts, creates/funds the approved queue, starts/stops the worker and exposes
on-chain owner pause/resume separately. It reads actual local queue/balance state
and displays candidate counts, choice/reason, prediction or fallback, signature
status and verified progress. It is not recorded playback. The service binds to
loopback and checks local origin/control tokens.

To resume an existing queue, use its public address from the create/run output:

```sh
bash scripts/payouts.sh run --queue QUEUE_ADDRESS --method learned
```

The journal freezes candidates and their estimates before execution. It persists
the chosen plan, signed bytes and signature before submission. Restart first
reconciles the signature and queue. An uncertain send is not interpreted as a
failed send; only identical signed bytes may be rebroadcast within the retry
bound. Expected-cursor enforcement rejects stale concurrent decisions. Before
signing, the worker checks freshness and the selected account snapshot again.
State envelopes expire after eight slots. Deployment observations refresh after
40 slots or 20 seconds, with at most three attempts to obtain a coherent account
snapshot; failure pauses execution. The current processed bank supplies only the
freshness clock. Account reads, simulations and execution receipts use confirmed
commitment. A known runtime compute/data-budget exhaustion suspends the profile;
the censored failed consumption is not used as a resource-demand label.
An account snapshot is evidence, not a chain lock; remaining races are handled by
the program's checks and transaction rollback.

A confirmed resource-budget exhaustion leaves payment progress unchanged and
forces subsequent estimates for that queue through simulation, within the shared
two-failed-attempt limit. Every method uses this recovery rule. The failed attempt,
its fee and the retry remain in the report; ordinary program errors still pause
execution. Recovery changes neither the resource caps nor trained model margins.

Short presentation flow:

1. Show the approved list and the funded test-token vault.
2. Execute a qualified model-derived batch; show selected count, signature,
   recipient balance deltas and matching cursor/paid-count increment.
3. Create a recipient ATA before the next decision and show the newly read state
   and resulting estimate. A count change is measured, not promised.
4. Use the benchmark's unknown account-alias shape to show bounded fallback.
   Use `bash scripts/payouts.sh upgrade-test` for the separate real dependency
   upgrade, profile suspension and fallback-payment regression described above.
5. Complete the queue and inspect zero pending obligations, exact amounts and no
   duplicates. Compare the causal ablation's actually executed first batch.

## Reports and interpretation

`artifacts/payouts/estimates.json` contains frozen holdout resource coverage,
failure/missing denominators, per-resource and joint underestimation, and mean,
median and p95 over-allocation. `candidate.json` records learned cell parameters,
policy, provenance and split identities; `baselines.json` records fit-only
competitor parameters. `last-run.json` links decisions, signatures and verified
outcomes. The benchmark exports `execution-report.json` and `execution-report.md`;
consult the dated results below and the generated report for an individual run.

Queue comparisons must include the program's overhead, all preparation and
confirmation work, simulations/controls, account/deployment reads, retries,
transactions and total RPC calls. Local Python/Node transport, signing, submission
and confirmation are measured separately where available. A modeled remote RPC
latency scenario must remain separate from measured local timings. Fees and ATA
rent deposits are separate; rent is not a consumed transaction fee. Fewer
simulation calls do not by themselves prove dollar savings.

Benchmark fallback counts exclude decisions simulated solely as sampled controls.
Control prediction errors compare measured simulation usage with the frozen model
limits, before simulation replaces those limits. The report retains selected
controls without outcomes separately: a control can be selected while considering
a candidate that the planner never executes. Such an absent outcome is neither a
successful observation nor a failed simulation. Confirmed resource-budget failures
quarantine the exact profile revision without treating censored failed usage as
successful demand evidence. Recovered timings and RPC totals remain missing when
the pre-crash process did not persist them; each metric includes its denominator.

## Measured local evidence: 2026-09-25

This run used Surfpool 1.5.0 offline, reporting Solana core 4.1.2 and feature set
3345198602. The compiled program's raw ELF SHA-256 was
`a1e04ed95beb488f8c9e28dd493c9a1e150d9439b3f835901bef4d785bebd027`;
the trained bundle digest was
`9694b75eb3ab1fc8dcc9516631740805594e791f294288fdbf776caf7471dc6b`.
Preserved reports contain historical `runtime.elf_digest`
`9e66afe2e6c109170ff4a198d948293a07e9f39bac6f5533eb82032ab60b5aa6`:
that value hashes the JSON-serialized Node Buffer, not raw ELF bytes. Future
bridge starts report the corrected raw-byte digest. Original evidence is retained;
deployment-watcher validity checks independently fingerprinted the full on-chain
code and did not rely on this display metadata.
The collection and grouped split counts are recorded above. Release manifests
record the earliest actual development slot for each cell, rather than using its
last fitting slot as the beginning of evidence.

Collection was interrupted with an actual SIGINT after 1,098 durable records and
resumed to all 3,520 unique scheduled observations, preserving earlier records.
Replaying the completed collection took 18.087 seconds and performed zero
simulations and zero submissions, with unchanged dataset SHA-256 and journal
counts. `artifacts/payouts/collection-recovery.json` records this evidence. These
are collector/application restarts against the same live isolated bank, not a
claim that the bank or ephemeral keys survive a validator/bridge restart.

Explicit local qualification initially activated 41 of 44 state profiles. Both
count-one profiles and count-seven/missing-one failed joint calibration. These
failures were retained, with no margin or policy change. Qualification uses the
declared local minimum of 12 fitting/20 calibration windows and joint Wilson
upper bound 0.15; it is not a production reliability certificate. Active status
can later change through controls, confirmed underestimates or deployment checks.

| Frozen holdout estimator | Statistically accepted / all | CU exceed / paired accepted | Data exceed / paired accepted | Fallback / all |
|---|---:|---:|---:|---:|
| State-conditioned learned quantiles | 656 / 704 (93.18%) | 6 / 656 (0.91%) | 0 / 656 | 48 / 704 (6.82%) |
| Marginal per-pattern p99 | 704 / 704 (100%) | 2 / 704 (0.28%) | 0 / 704 | 0 / 704 |

Joint exceedance equals CU exceedance here because no loaded-data limit was
underestimated. CU over-allocation mean/median/p95 was 56,903/55,834/97,559 for the
conditional model and 113,526/106,770/205,306 for marginal p99. The respective
loaded-byte figures were 11,271/11,336/11,700 and 11,749/11,700/12,525. These
averages use each method's own accepted subset, so they are not matched-sample
claims of improvement. Lower reservation came with more observed underestimates
and less coverage. Statistical acceptance does not mean that a prediction fits
the planner's fixed 100,000-CU cap or that its release remains active.

Only 303/704 measured holdout pairs fit the 100,000-CU scheduling cap; all 704/704
fit 1,400,000 CU with the same 1 MiB loaded-data cap. All 144 count-eight holdout
pairs fit the larger cap; maximum observed count-eight consumption was 310,051
CU and 107,579 loaded bytes. This is descriptive simulation evidence. It does
not measure completion time or fees for an alternative budget. It also shows
why gains under the chosen scheduling cap cannot establish general economic
advantage over ordinary fixed batches of eight.

The causal experiment executed the following first batches on fresh equivalent
eight-payment queues with existing ATAs and a 36,000-unit test-token total:

| Estimate source | Chosen and executed | CU limit / actual CU | Cursor / paid count | Total paid / vault remainder |
|---|---:|---:|---:|---:|
| Qualified learned profile | 4 | 80,600 / 59,694 | 4 / 4 | 10,000 / 26,000 |
| Fixed-estimate intervention | 2 | 70,000 / 32,989 | 2 / 2 | 3,000 / 33,000 |

Both paths verified the exact approved recipient balance changes and zero
duplicates. The learned path used an accepted prediction without estimation or
control simulation replacing it. The signatures were
`2Gcr7xToNVSZvLwTTSkupLT2SpPNEVwDoMNwwTGY6phXJiZx5f4mfcd1kvJzrDYYCaQJTHzRiRjxNzvbtVVDuvb3`
and
`4Zg9Hgnkzbhpm6pgCZKVvdwfNPgZhVCQGowJfinvxffaSgXwv9UvLSKBrKnEvmGAqR5hyJoitk1ELMBzRyRPWvjc`,
respectively. These identify transactions in this isolated runtime, not public
explorer transactions. The model changed actual payment execution; this result
alone does not establish superiority over a tuned simpler planner.

The completed benchmark reports `all_local_execution_gates_passed`: six methods
each completed three fresh 16-payment queues (all ATAs present, all absent, and
eight present), with one repetition per scenario. Across these 18 queues, all 288 obligations were paid exactly,
with zero pending obligations and zero duplicates. Every queue ended at
cursor/paid-count 16/16, total paid 136,000 and vault balance zero.

| Method | Successful / failed transactions | Sizing / control simulations | RPC / state-deployment reads | Active-step mean seconds (n=3) | Uninterrupted completion seconds (n) | Fees, lamports |
|---|---:|---:|---:|---:|---:|---:|
| Learned | 18 / 0 | 30 / 0 | 312 / 156 | 5.004 | 5.057 (3) | 90,000 |
| Tuned fixed batch | 48 / 0 | 0 / 0 | 642 / 402 | 7.362 | 7.495 (3) | 240,000 |
| State-aware formula | 36 / 0 | 30 / 0 | 546 / 306 | 7.041 | 7.167 (3) | 180,000 |
| Refreshed cache | 19 / 3 | 25 / 0 | 350 / 190 | 4.316 | 3.565 (1) | 110,000 |
| Marginal p99 | 48 / 0 | 0 / 0 | 648 / 408 | 9.112 | 9.278 (3) | 240,000 |
| Always simulate | 15 / 0 | 33 / 0 | 273 / 132 | 3.209 | 3.249 (3) | 75,000 |

Each method created 24 new recipient accounts and incurred 48,942,720 lamports
of ATA rent deposits; these are separate from the fee column. Setup/funding
is outside queue-completion timing. Cache suffered three confirmed CU-budget
exhaustions in 22 attempts and three bounded simulation retries, retaining all
failed fees and the exact original queues. Two cache queues were interrupted
while the shared recovery handler was completed, so their uninterrupted wall
times are unavailable. Its one-queue wall-time mean is not comparable to other
methods' three-queue means. Active-step sums use all three queues and include
failed attempts, but exclude administrative downtime and gaps after the outcome
timer. No measured RPC retries occurred.

The learned worker made 6/18 decisions directly from released predictions and
12/18 through fallback; 47/65 considered candidates passed model eligibility,
before the fixed resource-cap check. Existing ATAs produced four model-derived
batches of four. Missing ATAs produced eight simulated batches of two. The mixed
queue produced two predicted batches of four followed by four simulated batches
of two. These counts came from measured state and policy, not a scripted sequence.
Sizing simulation counts use actual RPC telemetry; the report retains a correction
of an initial learned-fallback counter that counted selected simulations twice.

| Method | Successful CU labels | CU over-allocation mean / median / p95 | Fallback / attempts | Preparation p95, ms |
|---|---:|---:|---:|---:|
| Learned | 18 | 17,255 / 6,983 / 43,391 | 12 / 18 | 659.3 |
| Tuned fixed batch | 48 | 44,069 / 45,450 / 57,099 | 0 / 48 | 181.5 |
| State-aware formula | 36 | 48,249 / 66,093 / 72,099 | 12 / 36 | 351.3 |
| Refreshed cache | 19 | 10,363 / 7,914 / 23,600 | 12 / 22 | 332.8 |
| Marginal p99 | 48 | 45,506 / 42,801 / 58,608 | 0 / 48 | 281.1 |
| Always simulate | 15 | 7,233 / 7,272 / 8,803 | 15 / 15 | 374.2 |

There were zero CU limit exceedances among each row's successful execution labels.
The three cache budget failures are censored observations, reported separately;
they are not successful demand measurements. Historical execution supplied zero
loaded-data labels, so execution loaded-data and joint error rates and
over-allocation remain unavailable, not zero. Paired simulation holdout results
above cover those resources separately. One sampled control elsewhere in the
ablation/restart evidence completed with 0/1 paired exceedances; this small sample
does not establish monitoring reliability.

Always-simulate was faster locally, used fewer transactions and made fewer total
RPC calls than the learned worker. The learned worker reduced simulation calls
only from 33 to 30 while adding inference/planning overhead. It beat the fitted
fixed and formula policies here, but those policies were conservative: the
fit-only fixed choice was one payment, and the formula retained a 61,035-CU
maximum positive fitting residual before its 10% margin. These results establish
causal model influence and working recovery, not commercial advantage or broad
superiority of learning. More representative measurements and policy choices
could change the comparison. The timing distributions are descriptive local
measurements with three queues per method, not a statistical performance guarantee.

Both deliberate application restarts—after signing and after submission—finished
their original four-payment queues with zero duplicates and empty vaults. The
unknown executor-as-recipient account-alias shape used real simulation fallback,
paid exactly 1,000 units, and ended with cursor/paid-count 1/1 and vault zero.

Reproduce with the collection/training/qualification commands above and
`bash scripts/payouts.sh benchmark`. Repeating the benchmark command resumes its
checkpoint on the same running isolated bank; it does not discard failed attempts
or resample completed queues. See `artifacts/payouts/execution-report.json` for all
signatures, exact counters, mean/median/p95 timings and per-run evidence, and
`artifacts/payouts/execution-report.md` for the readable export. Generated data
and artifacts remain ignored rather than becoming a trusted release in Git.

## Stop, reset and limits

Stop the foreground page with Ctrl+C, then:

```sh
bash scripts/payouts.sh stop
# Destructive only to this application's verified isolated state directory:
bash scripts/payouts.sh reset
```

The runtime bank and ephemeral keys live in the bridge process. Application
restart and collection restart can resume while that bridge remains running.
Stopping/restarting the bridge creates a new runtime identity and requires fresh
isolated state and qualification; it is not a durable validator restart claim.
Reset verifies the application's path and process identity before deleting its
ignored local artifacts. It does not reset other projects or a public chain.

Surfpool is real local SBF execution and RPC measurement, not a mocked program.
It is still an emulator, not proof of all Agave validator behavior, mainnet
reliability or production fees. This application supports only classic initialized
nonfrozen token accounts and ordinary on-curve recipient wallets. It deliberately
leaves completed/refunded queue tombstones and their rent allocated. External
token deposits after completion are outside the recovery interface.

Ordinary batching already exists in [Streamflow Payouts](https://docs.streamflow.finance/en/articles/12639121-payouts)
and [Solana Developer Platform payouts](https://docs.platform.solana.com/docs/payments/send-payouts).
The evaluated contribution is learned resource-aware execution within fixed
payment authority. Commercial demand and superiority to simpler planning remain
unproven. The [official recipient verification guidance](https://solana.com/docs/payments/send-payments/verify-address)
also explains why deriving an ATA does not replace checking its actual account
state and token authority.

For a Colosseum presentation, explain the reusable resource-planning core and
measured application results. For the supplied historical national AI case, show
the complete model → count → transaction → transfer/state-change chain. Historical
rules and dates do not establish current eligibility or future deadlines; this
test-token application is not automatically an RWA platform or DeFi protocol.
No forms are submitted and no acceptance is promised. Public devnet deployment,
if an event requires it, remains a separate explicit authorization after local
verification; an RPC URL alone is not permission to submit transactions.
