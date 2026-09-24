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

Local native account setup is emulator state control, not a deployed upgrade
transaction. Program-loader upgrade transaction fidelity and arbitrary CPI graphs
still require additional targeted tests before those workloads qualify for skipping.
The default controlled workload has only known native System/Compute Budget programs.
Surfpool behavior is valuable integration evidence, not an assertion of full Agave
validator fidelity or production-calibrated risk.

References: [Solana versioned transactions](https://solana.com/docs/core/transactions/versioned-transactions),
[Kit source](https://github.com/anza-xyz/kit), and
[Surfpool source](https://github.com/solana-foundation/surfpool).
