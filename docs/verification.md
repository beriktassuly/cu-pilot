# Integration verification — 2026-09-24

CU Pilot 0.2.0 has a working bound resource-estimation integration. It is not a
production-validated model. No public-network transaction or paid collection was
performed. Local tests use ephemeral keys in private offline runtime instances.

## Checks run

| Check | Result |
| --- | --- |
| `uv run pytest -q` | 334 passed; original correctness tests retained |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check .` | Passed |
| `uv run mypy src` | Passed, 19 source modules |
| `uv build` | Python 0.2.0 wheel and source distribution built |
| `npm --prefix typescript run build` | Passed |
| `npm --prefix typescript test` | 42 passed, none skipped |
| Real TypeScript integration | Legacy/v0/v1 simulation and signed local execution passed |
| Python runtime integration | 500 prospective paired observations, reconciliation, release, controls passed |
| Program upgrade integration | Signed loader-v3 upgrade, suspension, actual fallback passed |
| Known CPI dependency upgrade | ATA invokes Token/System; Token-only upgrade invalidates unchanged caller |
| Offline CLI replay | 500 synthetic records; restart deduplicates; export/train/evaluate passed |

One existing Starlette/httpx deprecation warning remains in the Python tests; it
does not fail the checks. Default tests make no network requests. The separate
runtime commands fail explicitly when native prerequisites are absent.

## Real execution evidence

Solders 0.29.0 and Solana Kit 8.3.0 agree on shared message/shape contracts.
Surfpool 1.5.0 ran with Node 24.14.1 in WSL Linux and reported core 4.1.2,
feature set 3345198602. Legacy/v0/v1 batch transfers measured respectively
600/600/300 CU and 36/124/14 loaded bytes. Final fallback limits were
700/700/400 CU and 32,768 data bytes. Actual signed execution confirmed the CU
measurements. Real v0 lookup resolution, absent-account creation, and account
closure/recreation also passed.

The Python collector froze each prediction before simulation, reopened its database,
deduplicated completed inputs, verified caller-signed bytes, and reconciled actual
finalized metadata. Historical CU came from `computeUnitsConsumed`.

The 500-observation run used 280 fitting groups, 120 calibration groups, and an
untouched 100-row holdout. Bootstrap watcher snapshots were retained without
authorizing release. Explicit local operator release enabled an accepted decision
with zero sizing RPCs. Growing recipient data to 65,536 bytes produced 65,572 loaded
bytes; a preselected control suspended the profile.

Both upgrade tests executed signed SDK loader instructions, rather than merely
changing an account fixture and calling it an upgrade. The CPI test verified that
Token's deployment fingerprint changed while ATA/System/Compute Budget remained
unchanged. Both caused suspension and one actual fallback simulation. The programs
were reinstalled with identical ELF payloads: these prove deployment invalidation,
not a change in resource demand caused by new executable logic. Their six-label
release policies are explicitly weak local state-machine tests, not qualification
under the default model policy.

## Measured policy comparison

See [local runtime details](local-runtime.md) for measurement scope and commands.
In 100 interleaved requests per method, the released policy selected five controls,
avoided 95 sizing calls, and added seven watcher reads. Median preparation was
26.02 ms versus 52.37 ms for always simulating. Its p95 was **81.57 ms versus
59.13 ms**, and p99 **133.06 ms versus 72.90 ms**. Lower call count did not imply
better tail latency. Only the five selected requests had deployment control labels.

Shadow mode made all 500 simulations and avoided zero calls. Its measured
p50/p95/p99 were 65.15/101.24/131.09 ms, including 1,023 state reads and durable
collection. Artifacts were already loaded. No preflight or necessary validation
call was counted as removable. There is no measured network-fee or landing benefit.

Static, p95/p99, cache and joint-policy holdout comparisons are available; this
simple native workload does not demonstrate a model advantage over a deterministic
rule. Configured priority fees are calculated separately from observed total fees;
v1's unchanged absolute priority fee does not decrease with a smaller resource limit.

## Supported boundary and next validation

The local v1 suite enables all features. Upgrade suites use default features because
the bundled ELF fails redeployment with all features forced on. No single feature
configuration is claimed to cover both matrices. Emulator checks do not establish
full Agave validator fidelity.

Loader-v3, immutable loader-v1/v2, and explicitly declared native runtime identities
are supported. Loader-v4 and untracked/dynamic CPI closures fall back. RPC version
strings do not fully identify active feature gates; operators must maintain context
epochs for relevant runtime changes. Snapshot revocations propagate through refresh
or expiry. The TypeScript journal supports one writer process; prefix verification
cost grows with retained history. Cancellation of an in-flight synchronous Python
request is bounded by its request timeout.

Distinct slots from one repeated local burst are not independent production
evidence. The next model-validation step is bounded prospective shadow collection
from the controlled application builder across varied account states, amounts,
deployment epochs and time periods. Freeze the holdout and operational risk target
before examining labels; compare static/cache rules at the same joint-risk budget.
Keep v1 skipping disabled until its own paired evidence and lifecycle gates qualify.
Any optional devnet submission requires separate authorization.

Setup and public interfaces: [README](../README.md), [integration](integration.md),
[resource model](resource-model.md), [profile lifecycle](lifecycle.md),
[TypeScript runtime](typescript.md), and [upgrade tests](program-upgrade.md).
