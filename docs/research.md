# Research and implementation decisions

Checked **2026-09-24** against official documentation and project sources. These are
implementation inputs, not a claim that the prototype has demonstrated production
safety. Protocol limits and SDK support must be rechecked before deployment.

## Transaction versions: v1 is already live

Solana Foundation reports that the `txv1` feature activated on mainnet at epoch
1035, September 15, 2026, with devnet and testnet also active. Legacy and v0 remain
supported. v1 permits 4,096-byte transactions, removes address lookup tables, and
stores compute/data limits and priority fee in the message configuration. Its
unset compute and loaded-data limits are zero. RPC operators need Agave 4.2.2 or
later to avoid a documented storage conversion bug that could misreport v1 as v0
and lose configuration. Source: [official activation and migration notice](https://solana.com/upgrades/larger-transaction-sizes).

The [SIMD-0385 proposal](https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0385-transaction-v1.md)
still displayed `Review` in its header when checked. This conflicts with the dated
deployment notice; proposal status alone is not evidence of cluster activation.

Readers should request JSON integer `maxSupportedTransactionVersion: 1`, then
validate the version they receive. The RPC `transaction.message.transactionConfig`
object is the source for v1 budget fields; legacy/v0 budgets are encoded in
ComputeBudget instructions. v0 lookup keys must be resolved before interpreting
instruction account indexes. Raw v1 serialization differs substantially from v0;
v1 has a leading `0x81` discriminator and signatures at the tail. Source:
[versioned transaction documentation](https://solana.com/docs/core/transactions/versioned-transactions).

**Decision:** accept the three current versions in normalized JSON, keep version
in the pattern identity, and fail closed on unsupported versions. Do not implement
an unverified binary transaction codec in this iteration. Parsing v1 does not by
itself qualify a pattern to skip simulation: loaded-data evidence is needed too.

## Compute budgets and separate resource budgets

The documented per-transaction maximum is 1,400,000 CUs. Legacy/v0 defaults depend
on instruction type: ordinary SBF instructions receive 200,000 each, while certain
builtins receive 3,000; the total is capped. A naive `instruction_count * 200000`
is consequently an evaluation baseline, not an exact runtime-default calculation.
The maximum loaded-accounts budget is 64 MiB. ComputeBudget instructions do not
configure v1: they execute as no-ops, still consuming 150 CUs and an instruction
slot. Source: [compute budget documentation](https://solana.com/docs/core/fees/compute-budget).

For legacy/v0, the prioritization fee is calculated from the **requested** CU
limit and price, not actual consumption. v1 instead specifies an absolute total
priority fee in lamports. Reducing a v1 CU limit alone does not proportionally
reduce an already-fixed fee. Source: [fee structure](https://solana.com/docs/core/fees/fee-structure).

**Decision:** expose CU and loaded-data recommendations as distinct quantities.
Keep fees outside the prediction target. An estimate whose headroom approaches
the hard CU limit triggers fallback; silently clamping a larger estimate would
hide uncertainty. A CU predictor must not imply that account data, heap, locking,
or program execution validity has also been certified.

## Labels, features, and simulation

Historical metadata exposes optional `meta.computeUnitsConsumed`; this is the
primary CU label. `costUnits` is a separate field and is not a replacement label.
Metadata may be absent, and reduced `accounts` block responses omit relevant
fields. `loadedAddresses` supplies resolved lookup addresses, not loaded byte
size. Source: [RPC JSON structures](https://solana.com/docs/rpc/json-structures).

`simulateTransaction` does not broadcast. It can run an unsigned serialized
transaction with signature checking disabled, and returns `unitsConsumed`,
`loadedAccountsDataSize`, and `err` in the response value when available.
`replaceRecentBlockhash` permits refreshing an ordinary blockhash during
simulation. Source: [simulateTransaction reference](https://solana.com/docs/rpc/http/simulatetransaction).

**Decisions:**

- Persist the outcome and label source separately from pre-execution features.
  Use only successful, complete observations for fitting conservative budgets.
  A failed execution can stop early; its consumed units do not measure the amount
  needed to complete the intended transaction. Keep such rows for analysis.
- Do not infer missing labels as zero, and do not substitute requested limits,
  fees, logs, inner-instruction counts, or post-execution balances for labels or
  predictive features. Preserve missing loaded-data labels explicitly.
- Accept pre-resolved v0 lookup keys from the builder. Historical `loadedAddresses`
  can reconstruct the same message inputs for training, but inference must resolve
  them before execution. Reject unresolved indexes.
- A fallback simulation uses caller-supplied serialized bytes. An estimation
  caller must prepare sufficiently high budgets first; simulation of an already
  undersized budget cannot establish the needed limit. Do not automatically
  replace durable-nonce lifetime values. Surface failed simulation instead of
  converting it into a plausible-looking numeric recommendation.
- Record cluster, collection time/slot, provider/runtime version when available,
  and application/program deployment context. State and deployed code can change
  while a structural transaction pattern remains identical.

## What maintained tools already do

| Project | Relevant behavior | Implication for CU Pilot |
| --- | --- | --- |
| [Solana Kit resource estimator](https://www.solanakit.com/api/functions/estimateResourceLimitsFactory) | Simulates with maximum CU and loaded-data budgets. Returns both limits for v1; missing loaded-data results cause an error for v1. | A strong integration/fallback target; it already handles version-sensitive preparation. |
| [Kit release history](https://github.com/anza-xyz/kit/releases) | v8.3.0, September 9, 2026, removed `estimateComputeUnitLimitFactory` in favor of `estimateResourceLimitsFactory`; the paired setting/provisory helpers were replaced too. | Documentation should use current resource helper names, not older CU-only examples. |
| [Jupiter Metis swap API](https://developers.jup.ag/docs/api-reference/swap/v1/swap) | `dynamicComputeUnitLimit: true` simulates the swap, adding one RPC call. The page now marks Metis as superseded by Swap V2. | Evidence for the simulation latency opportunity, not evidence of a learned predictor or a current API integration choice. |
| [Solana developer helpers](https://github.com/solana-developers/helpers) | `getSimulationComputeUnits` and `addComputeInstructions` build on simulation and configurable buffering. | Include simulation plus a margin as the familiar reference workflow. |
| [Helius Rust SDK source](https://github.com/helius-labs/helius-rust-sdk/blob/dev/src/optimized_transaction.rs) | Transaction optimization gets measured CU consumption and applies a configurable buffer; the inspected source defines a default multiplier of 1.25. | Margins are engineering choices; compare them empirically instead of claiming a universally correct percentage. |

This short survey found established simulation-based helpers. It did not establish
that no learned estimator exists. CU Pilot's proposed distinction is selective
abstention: learn which repeated workload shapes have enough evidence to avoid
that preparatory simulation call. Fee-price estimation and compute-consumption
estimation solve different problems.

## Patterns and evaluation

Solana's optimization guidance explicitly warns that CU usage can vary, including
from program-derived-address search, and suggests either measuring an upper
envelope over time or adding a simulation margin. Source:
[optimal compute budget guide](https://github.com/solana-foundation/developer-content/blob/main/content/guides/advanced/how-to-request-optimal-compute.md).

**Engineering choices and assumptions:**

1. Start with empirical per-pattern p95/p99/max baselines and explicit headroom.
   Prefer transparent JSON artifacts and CPU inference. Quantile regression is an
   experiment to compare, not a prerequisite or a safety claim.
2. Build a versioned deterministic pattern from ordered programs, conservative
   instruction prefixes, data lengths, account-role/alias shape, and version.
   Exclude signatures, blockhashes, slots, and ordinary account identities. This
   intentionally cannot identify all state-dependent execution branches. Unknown
   program encodings can fragment patterns; explicit program decoders can improve
   useful coverage later.
3. Gate skipping on sample count, calibration evidence, spread, data completeness,
   and recency. New patterns, incompatible versions, missing inputs, stale
   artifacts, and near-limit recommendations must abstain. Empirical confidence
   is not a guarantee that the next observation is bounded.
4. Split observations chronologically and keep repeated signatures from crossing
   splits. Choose policy thresholds using training/calibration data, then report
   held-out coverage and underestimation among accepted predictions. Also report
   fallback rate and excess CU mean/median/p95. Tiny or synthetic fixtures prove
   pipeline behavior, not production performance.
5. Report latency as a scenario unless RPC timing was actually measured. A simple
   estimate is `coverage * simulation_ms - local_prediction_ms` per transaction,
   assuming calls are serial. Parallel preparation, retries, and account-resolution
   requests can change real end-to-end savings. An actual-consumption-plus-margin
   comparator is an oracle diagnostic unless paired simulation labels exist.

## Public repository quality and next experiment

GitHub recommends a README explaining purpose, usage, setup, and support, and a
security policy explaining how to report vulnerabilities. Sources:
[README guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)
and [security policy guidance](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/add-security-policy).

For this prototype, that means reproducible installation, typed schemas, small
offline fixtures, deterministic tests, linting in CI, explicit experimental
status, and no credentials or large datasets in version control. Keep signing and
broadcasting outside the project. Python is a practical first implementation;
measure its local overhead before deciding that another language is necessary.

The next useful experiment is a small consenting application's **shadow-mode**
sample: capture pre-execution normalized messages, simulation results/timing, and
eventual transaction metadata across multiple days and program versions. Continue
the application's existing simulation behavior while scoring proposed skips.
Include failures, fresh accounts, changed routes, and periods after upgrades.
Select a small set of stable patterns only after chronological holdout results
support the desired underestimation/coverage tradeoff. Live RPC is optional for
development, and this research involved no signing or broadcasting.
