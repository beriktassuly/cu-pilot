# Real local integration

The separate integration suite uses actual Solana Kit 8.3.0 serialization and
Surfpool 1.5.0 simulation/execution. Surfpool is configured offline; it does not
fork mainnet or need provider credentials. Native bindings support Linux x64 glibc
and macOS. On Windows use WSL and a Linux Node 24 installation. Missing prerequisites
cause an explicit failure, not an all-skipped success.

From a Linux/macOS checkout:

```sh
npm --prefix typescript ci
npm --prefix tests/integration/runtime ci
npm --prefix typescript run integration
uv sync --dev
uv run python tests/integration/python_runtime.py
uv run python tests/integration/program_upgrade.py
uv run python tests/integration/cpi_program_upgrade.py
# Optional unsigned builder/fallback example:
npm --prefix typescript run example
```

The runtime package has a separate lockfile because Surfpool's optional plugin
declares a Kit 7 peer. The suite calls Surfpool's native API and HTTP RPC directly,
while transaction construction uses Kit 8. No unsupported plugin peer override is
needed. Signing keys exist only in the test process. Production modules neither
sign nor submit transactions.

Windows may run TypeScript build/tests natively, then run compiled integration files
with Linux Node:

```powershell
npm --prefix typescript run build
wsl -d Ubuntu --cd /mnt/c/Users/berek/cu-pilot/typescript --exec node --test dist/test/integration/local.test.js
$env:CU_PILOT_LOCAL_NODE='["wsl","-d","Ubuntu","--cd","/mnt/c/Users/berek/cu-pilot/tests/integration/runtime","--exec","node","serve.mjs"]'
uv run python tests/integration/python_runtime.py
```

Use your checkout path. `CU_PILOT_LOCAL_NODE` is a JSON array containing the entire
local launcher command. Linux Node must be on WSL's PATH; Windows Node cannot load
the Linux native module. The test launcher exposes only a temporary loopback RPC
endpoint and authorized local-test execution; it never outputs secret keys.

Verified on September 24, 2026: Node 24.14.1 in WSL Linux, Surfpool 1.5.0 reporting
`solana-core: 4.1.2`, feature set 3345198602. Actual v1 simulation and signed execution
succeeded with `allFeatures: true`; the reported core version alone is therefore
not the capability test. The controlled two-transfer workload consumed 600 CU for
legacy/v0 and 300 CU for v1. Loaded-data measurements were 36, 124 and 14 bytes
respectively. These are local-runtime measurements, not universal program costs.
The ignored `typescript/.integration-report.json` records current measurements and
full preparation times. Three samples cannot support latency percentiles or a
production improvement claim.

The TypeScript suite checks real legacy/v0/v1 fallback and execution, v0 table
account resolution through SDK decoding and RPC, insufficient initial budgets,
bound message mismatch, stale lookup evidence, deterministic failure and cancellation.
The Python suite checks prospective shadow simulation, committed prediction before
label, database reopen/deduplication, signed-message correspondence, actual execution
metadata and idempotent finalized reconciliation. Simulation and historical labels
remain separate; no historical transaction is re-executed to invent a past label.

It additionally collects 500 actual paired-resource simulations in distinct local
bank slots. Local signed tick transactions advance the bank between inputs; these
are fixture setup, not production library behavior. The first 400 rows provide 280
fit and 120 calibration groups; the final 100 remain untouched holdout rows. An
explicit test operator releases the fitted profile against actual deployment reads.
A subsequent accepted prediction performs zero sizing RPC calls. Growing the
recipient to 65,536 bytes then produces 65,572 loaded bytes in a selected control
simulation and suspends the profile. No synthetic labels or fabricated slot counts
are used. All these local observations still come from one correlated workload
burst; distinct slots do not establish independent production evidence.

The final measured WSL/Windows run of those 500 shadow inputs had preparation p50 65.15 ms,
p95 101.24 ms, and p99 131.09 ms. These sum nonoverlapping measured SDK slot/blockhash
reads and builder work with Python preparation, actual simulation, and durable
collection. They include 1,023 state reads (including 23 deployment refreshes),
500 resource simulations, and durable unreleased deployment snapshots.
The local test bank-advancement transactions are excluded. Avoided calls in shadow
are zero.
The local environment, fixed workload and process boundaries limit generalization.

After explicit release, the test interleaves 100 always-simulate preparations and
100 complete-policy preparations with a preselected 5% control probability. This
run selected five controls and avoided 95 resource-estimation calls. The 95 unselected
requests have no resource label; they are not counted as successful audited examples.
The full measured preparation distributions were:

| Method | Mean ms | p50 ms | p95 ms | p99 ms | Sizing/control calls | State reads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Always simulate | 53.10 | 52.37 | 59.13 | 72.90 | 100 / 0 | 200 |
| Released policy | 35.11 | 26.02 | 81.57 | 133.06 | 0 / 5 | 207 |

The released policy had lower median but **higher p95/p99** in this local run.
No production latency benefit is established. Cached deployment checks, durable
control logging, and seven watcher reads are included. The watcher refreshes by
observed slot/time age rather than assuming a fixed number of requests equals a
fixed number of slots. Artifacts were preloaded (zero refreshes); preflight and
business validation were outside preparation and none were counted as avoided.
Telemetry's own final write and local fixture bank advancement remain excluded.
Random control counts and measured times vary between runs.

The holdout comparison scored 100 paired rows for static limits, p95/p99 and the
joint statistical policy, with no exceedances in this controlled sample. The cache
accepted 97 and refreshed three times. Complete-policy replay falls back for all
100 because no released-profile snapshot callback was supplied. Bootstrap
deployment observations do not authorize skipping. The separately executed
release/control comparison supplies actual lifecycle evidence. These results do
not show an advantage over a deterministic System-transfer rule or establish
customer demand, landing improvements, fee reductions, or production risk.

Local native account setup is emulator state control. The separate
[program-upgrade.md](program-upgrade.md) suite executes an actual signed loader-v3
Upgrade instruction, verifies the changed deployment slot through RPC, and checks
suspension plus subsequent fallback. It reloads the same bundled Memo ELF; it does
not demonstrate a behavioral change in program code. Upgrade tests use Surfpool's
default feature configuration because its bundled Memo ELF fails redeployment with
all features forced on, while the v1 suite uses all features. Arbitrary CPI graphs
and full validator fidelity still need targeted verification before qualifying.
The default controlled workload has only known native System/Compute Budget programs.
The additional ATA fixture verifies real Token/System CPIs, then upgrades Token
while the caller remains unchanged and checks dependency-based suspension.
Surfpool behavior is valuable integration evidence, not an assertion of full Agave
validator fidelity or production-calibrated risk.

References: [Solana versioned transactions](https://solana.com/docs/core/transactions/versioned-transactions),
[Kit source](https://github.com/anza-xyz/kit), and
[Surfpool source](https://github.com/solana-foundation/surfpool).
