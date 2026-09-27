# Solana research behind the hybrid payout estimator

Research checked on **2026-09-27**. This note distinguishes the pinned local
experiment from the current network and from proposals. It does not establish
mainnet eligibility for a locally trained model.

The measured native bank is Surfpool/LiteSVM embedding Agave crates, not a full
Agave validator. Surfpool 1.5.0's [lockfile](https://github.com/solana-foundation/surfpool/blob/v1.5.0/Cargo.lock)
pins LiteSVM 0.14.0 and the relevant Solana runtime, syscall, SVM and version crates
to 4.1.2. The Agave 3.1.10 toolchain used to build the unchanged ELF is distinct
from the runtime executing it.

## Decision

Keep the PDA-aware formula as an explicit experimental method in the existing
adaptive planner. Its additional inputs describe public, pre-execution work
performed by this payout program. A prediction can bypass simulation only after
the usual state, deployment, runtime, qualification and message-binding checks.
A more accurate central estimate alone is insufficient.

Do not change the on-chain payout program or transaction version in the same
comparison. Such changes would alter the workload whose formula is being tested.

## The cryptographic mechanism

For each recipient, derive the canonical associated token account (ATA) from the
wallet, token-program ID and mint under the ATA program ID. A bump search starts
at 255 and tries decreasing bumps until the SHA-256-derived address is off the
Ed25519 curve. The public bump therefore exposes the number of attempted
derivations; it is not a private key, an execution label or evidence of account
existence. The official [PDA documentation](https://solana.com/docs/core/pda) and
[ATA seed implementation](https://github.com/solana-program/associated-token-account/blob/main/interface/src/address.rs)
describe that contract.

The pinned Agave **v4.1.2** implementation charges 1,500 CU per address-creation
attempt. Its search charges the initial attempt and another 1,500 CU after each
failure. For a successful supported bump, the number of attempts is 256 minus
the bump. This is a cost of this syscall, not a formula for the entire
transaction. See the pinned
[execution-cost constants](https://github.com/anza-xyz/agave/blob/v4.1.2/program-runtime/src/execution_budget.rs#L196-L205)
and [search loop](https://github.com/anza-xyz/agave/blob/v4.1.2/syscalls/src/lib.rs#L815-L861).

The current payout program verifies the canonical ATA for every recipient.
When that account is missing, it additionally invokes the ATA program, which
performs its own canonical derivation before creating the account. The
[ATA processor](https://github.com/solana-program/associated-token-account/blob/main/program/src/processor.rs#L69-L120)
also verifies the derived address before its idempotent existing-account return.

That gives a causal motivation for five terms:

    estimated_CU = beta_0
                 + beta_n * payment_count
                 + beta_m * missing_ATA_count
                 + beta_a * total_ATA_search_attempts
                 + beta_am * missing_ATA_search_attempts

The coefficients are fitted, not declared to equal runtime constants. SBF
instructions, token transfers, CPI overhead, signer derivation and state checks
also consume compute. Residual protection, rounding, empirical qualification and
fallback remain necessary. This is a workload-specific resource estimator, not
new cryptography or a universal Solana cost formula.

## Safe off-chain reuse

The implementation memoizes only the immutable mapping of the complete
(wallet, mint, token program, ATA program) tuple to (canonical address, bump).
The cache contains at most 4,096 entries and is shared by recipient-address
validation and derivation-feature extraction.

Account existence, ownership, initialization, frozen status, balances, resource
estimates and qualification are never cached by this helper. Every state
snapshot still undergoes normal validation. The helper exposes cache_clear()
and cache_info(); comparison runners can clear it at a declared boundary and
record cold/warm statistics. Cached derivation saves local preparation work,
not on-chain CU. Its effect on end-to-end time must be measured.

## Current network changes and model portability

| Change | Verified evidence | Implication for this experiment |
| --- | --- | --- |
| P-token replaces the Token Program implementation at the existing Tokenkeg address | The [official upgrade page](https://solana.com/upgrades/p-token) reports completed mainnet/devnet activation; the [feature explorer](https://explorer.solana.com/address/ptokFjwyJtrwCa9Kgo9xoDS59V4QccBGEaRFnRPnSdP) records mainnet activation at slot 419,472,000. | Program ID alone cannot identify execution cost. Preserve actual runtime and deployment evidence; refit and requalify for a different implementation. |
| Transaction v1 is deployed on mainnet | The [September 19 publication of the September 18 changelog](https://solana.com/news/solana-changelog-september-18-2026) explicitly reports this; [current format documentation](https://solana.com/developers/cookbook/transactions/versions) describes direct resource configuration, no lookup tables and a 4,096-byte size limit. | A local Surfpool 1.5.0 / Agave 4.1.2 result does not certify current-network behavior. Changing format is a separate benchmark. |
| CPI metering can change under feature gates | [SIMD-0339](https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0339-increase-cpi-account-info-limit.md) specifies account-info and metadata charges together with a reduced base CPI cost. | Bind coefficients to runtime/feature evidence, even when business instructions are unchanged. |
| Mainnet block capacity rose to 100 million CU | The [official upgrade page](https://solana.com/upgrades/100m-cu-blocks) records activation on July 29, 2026. | This is a block limit, not permission to exceed the transaction's 1.4-million-CU ceiling. |

Some SIMD source headers still say Review while later official deployment
information reports activation, notably SIMD-0266 and SIMD-0385. Proposal status
alone is not a reliable runtime-capability check. Likewise, support in a client
SDK does not prove activation in the bank being measured.

## Simulation and economics

The [RPC specification](https://solana.com/docs/rpc/http/simulatetransaction)
documents unitsConsumed and loadedAccountsDataSize and supports unsigned
simulation when signature verification is disabled. Missing labels must remain
missing rather than becoming zero. Simulation is still conditional on the
state and commitment against which it ran.

Solana Kit's
[estimateResourceLimitsFactory](https://www.solanakit.com/api/functions/estimateResourceLimitsFactory)
simulates with maximum CU and loaded-data limits. It returns both resources for
v1 and rejects a missing loaded-data measurement there. Its legacy/v0 path
returns CU only; the application's explicit paired-resource fallback must not
assume this helper supplies both values for every version.

For legacy/v0, the documented priority fee is:

    priority_fee_lamports = ceil(CU_price_micro_lamports * requested_CU / 1_000_000)

At zero CU price, reducing the requested limit alone does not reduce this fee.
In v1 the priority fee is an explicit absolute lamport total, so a lower CU
limit does not automatically lower that total. Record actual charged fees and
transaction version; do not substitute reservation savings for monetary
savings. See the [official fee structure](https://solana.com/docs/core/fees/fee-structure).

[SIMD-0553](https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0553-resource-fee-burn.md)
proposes a resource-based fee tied to requested scheduler resources. Its inspected
source is a draft with feature-gated rates. Those prospective rates are not used
as measured fees or as an assumption of this branch.

## Optimizations deferred to separate experiments

P-token's batch instruction can perform multiple token operations in one CPI,
reducing repeated invocation overhead. It is relevant to a payout application,
but using it would change the on-chain program, instruction shape and training
distribution. Measure it separately after the formula comparison.
[SIMD-0266](https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0266-efficient-token-program.md)
describes the instruction and its rationale.

The current SDK offers hash-only address derivation for seeds and bumps already
known to be valid. Its documentation explicitly says that path omits off-curve
validation. Replacing canonical validation with it is not an equivalent safe
optimization of this estimator. Any program change would need a correctness
review and fresh evidence.
[SDK derivation source](https://github.com/anza-xyz/solana-sdk/blob/master/address/src/derive.rs).

No directly applicable verified recent Solana CU-prediction paper found in the
targeted search superseded the measured baselines. Consensus cryptography,
signature batching and zero-knowledge primitives do not improve this workload
merely because they are recent. The useful cryptographic information here is
the deterministic public derivation work already performed by the program.

## Deployment snapshot correction found during native validation

Surfpool 1.5.0 uses a 1 ms default slot interval. Even in transaction production
mode, its blockhash-expiry event produces a block after 75 intervals. Consequently,
two consecutive account reads can observe different slots without an intervening
workload transaction. See the [SDK defaults](https://github.com/solana-foundation/surfpool/blob/v1.5.0/crates/sdk/src/surfnet.rs#L55-L67),
[expiry-triggered production](https://github.com/solana-foundation/surfpool/blob/v1.5.0/crates/core/src/runloops/mod.rs#L350-L375)
and [timer](https://github.com/solana-foundation/surfpool/blob/v1.5.0/crates/core/src/runloops/mod.rs#L589-L598).

A fresh local diagnostic saw nine context changes in 100 sequential
Program/ProgramData read pairs. Requiring both RPC contexts to be identical
rejected valid refreshes. Simply relaxing equality would combine deployment
accounts from inconsistent snapshots.

The watcher now discovers ProgramData pointers, then fetches Programs and their
ProgramData together in one atomic response. Only that final response supplies
deployment evidence. Pointer changes, backward contexts, invalid loaders,
visibility delays and oversized batches still reject and invalidate cached
evidence. A separate 100-refresh native check completed without errors, including
nine advances between discovery and final snapshot. These checks diagnose
snapshot correctness; they are not performance or production reliability results.
