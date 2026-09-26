# CU Pilot: Autonomous Payout Reference Application

## Mission

You are an autonomous senior engineer continuing development of CU Pilot in this repository. Implement a complete reference application for autonomously executing preapproved Solana payouts using CU Pilot's resource estimation, learned profiles and simulation fallback.

**CU Pilot remains the reusable resource estimation and planning core. Autonomous payouts are its first complete reference application.** Preserve the working foundation instead of replacing it with a payment-specific library. Deliver working code, meaningful training and evaluation, an actual local Solana program, a small interface and reproducible evidence. Do not stop at research, a plan, scaffolding, a mock dashboard or another implementation prompt.

This is the active next-phase prompt. It replaces the previous integration-and-reliability assignment and supersedes a standalone release-checking roadmap as the next milestone. `ML_AGENT_PROMPT.md` provides historical project context, not an instruction to rebuild the original foundation. The previous restrictions against an application program and a demo interface are intentionally superseded for this bounded reference application. The core library still prepares unsigned transactions; application code owns signing and sending.

Make routine engineering decisions independently. Treat the design below as intent, constraints and acceptance criteria, and document justified changes. Complete all authorized local work before asking for any genuinely missing external authorization.

## Why this application

The user supplied historical national-hackathon specifications. Their AI + Blockchain case requires a deployed Solana program, a model or agent, a demo and the actual chain:

> learned model -> batch-size decision -> Solana transaction -> token transfers and program state change

The model estimates resource demand for candidate batches. A deterministic planner uses those estimates to select how many approved obligations to execute next. The application program enforces the owner's immutable recipients and amounts. The model has no authority to invent beneficiaries or increase payments.

The demonstration must show that changing the model's estimates can change the batch actually executed. Displaying predictions or storing an inference hash alone is insufficient.

The supplied rules and deadlines are historical. Do not claim that they establish current eligibility, use their dates as future deadlines, or automatically classify this application as an RWA platform or DeFi protocol. For Colosseum, explain the reusable infrastructure and measured application results. For the supplied national AI case, make every arrow in the chain visible. Do not submit forms, contact organizers or promise acceptance.

Bulk payments already exist in products such as Streamflow and Solana Developer Platform. The contribution to evaluate is learned resource-aware execution within fixed payment authority. Commercial demand and an advantage over simpler planning are unproven; evaluate them honestly instead of claiming that ordinary batching is novel.

## Inspect and reuse before implementing

Check applicable repository instructions, Git status, README, CONTRIBUTING, package manifests, lockfiles and current source. Preserve unrelated changes and inspect existing behavior rather than treating old prompt descriptions or test counts as current requirements.

Read the relevant documentation in `docs/`, especially `integration.md`, `resource-model.md`, `lifecycle.md`, `local-runtime.md`, `verification.md` and `review-corrections.md`. Inspect:

- Python parsing, feature extraction, exact-message binding, resource estimation, evaluation, shadow collection and profile lifecycle code in `src/cu_pilot/`.
- TypeScript builders, local inference, binding and control storage in `typescript/`.
- Real local execution and deployment-invalidation fixtures in `tests/integration/` and current CI workflows.

Use existing offline tests and local execution infrastructure where practical. Research current official Solana, Anchor, SPL Token and SDK sources when their behavior affects implementation. Verify available Rust/SBF/Anchor, Python, Node and local-runtime toolchains. On Windows, use a supported local build environment or a documented reproducible CI build where necessary; a mocked program is not a substitute for an executable program. Pin compatible dependencies and document the supported environment.

## Architecture boundaries

Keep these responsibilities explicit:

- **Core:** generic transaction normalization, features, binding, conservative resource estimates, fallback, profile lifecycle, observation provenance and evaluation. Reusable state-envelope or planner interfaces may be added here if justified.
- **Reference application:** queue and payment semantics, ATA discovery, application-specific candidate generation, local executor keys, signing/sending, reconciliation, token setup and interface.
- **On-chain program:** payment authorization, vault ownership, immutable obligations, ordered execution and atomic state updates.

Preserve existing unsigned SDK/API/CLI behavior and generic transaction use cases. Do not add automatic signing or transaction submission to the reusable core.

A reasonable layout is `programs/payout_queue/` for the program, `apps/payout-demo/` for the worker and interface, `examples/payouts/` for collection and evaluation commands, and `docs/payouts.md` for the application. Choose a smaller consistent layout if it fits the repository better. Avoid duplicating the existing inference and observation stacks.

## Bounded MVP and user flow

Use one classic SPL Token test mint, one owner, one designated executor, and an immutable ordered queue of at most 16 payments. Label the token as a test asset, not real USDC or proof of a real-world asset. Start with a finite candidate menu such as 1, 2, 4 and 8 payments, with explicitly supported tails.

1. The owner approves recipients and integer token amounts, creates a queue and funds its vault.
2. The worker reads the queue and relevant pre-execution account state.
3. It constructs eligible candidate transactions for contiguous prefixes of pending obligations.
4. The learned estimator predicts resources; the policy chooses a feasible next count or falls back.
5. The application verifies the final bound transaction, signs with its local test executor and submits it to the isolated local runtime.
6. The program transfers exactly the approved amounts and atomically advances queue progress.
7. The application verifies actual balances and queue state and continues until completion, pause or a clearly reported failure.

Existing versus absent recipient associated token accounts (ATAs) provides real state-dependent work. Do not add artificial compute work to make learning appear necessary.

Exclude swaps, lending, yield strategies, Token-2022 transfer hooks, arbitrary CPI targets, cross-chain transfers, real assets, a general workflow engine and a hosted multitenant service. A small interface is required; a broad dashboard is not.

## Program and payment authority

Use Anchor/Rust if it is the smallest maintainable choice. Build and deploy an actual program to the local runtime. An example interface, adjustable after implementation review:

- `create_queue`: owner-signed; validates a unique queue identity, fixed mint, executor, expiry, nonzero payments and checked total; stores immutable terms and transfers the approved total into a queue-PDA-controlled vault.
- `execute_batch(expected_cursor, count, decision_id, model_digest)`: validates executor authority, queue identity/status, pause/expiry, cursor, allowed count and remaining range. Transfers the exact next stored obligations, then atomically updates `cursor`, `paid_count` and `total_paid`.
- Owner-controlled pause/resume without rewriting funded payment terms.
- Owner-only refund of unspent funds after expiry, with a terminal state preventing further execution. Preserve a tombstone or equivalent durable identity so a closed queue cannot be ambiguously recreated and replayed.

Validate remaining accounts explicitly: expected recipient ATA derivation, mint, token-account owner and authority, vault derivation and authority, program identities, signer/writable roles and account order. Pin the classic Token, ATA and System programs. Reject arbitrary replacement accounts and programs. Use checked arithmetic. Either reject duplicate recipients at creation or support their semantics explicitly with tests.

The worker supplies the count and audit identifiers, not replacement payment terms. A prediction or model digest is never authority to debit funds. The digest records provenance; it does not prove that inference was correct.

The executor pays any recipient-account creation costs from a separate bounded local SOL allowance. Document that token-vault authorization does not itself cap every network fee or rent payment made by an executor. Enforce application retry/spend bounds separately.

Prove that stale concurrent decisions, unauthorized execution, recipient substitution and repeated submissions cannot duplicate payments. A failed batch must roll back transfers and queue progress atomically. An uncertain send outcome requires signature and queue reconciliation before choosing another action; do not treat a timeout as proof that nothing executed.

## Data collection and actual learning

Existing models and native-transfer observations do not qualify this program. Changing batch size changes transaction shape; never grant an unseen size confidence just because a nearby size worked.

Collect bounded, reproducible observations from actual execution/simulation of this program. Vary supported batch counts, queue tails, recipient identities and existing/missing ATAs. Include supported and unsupported state cases. Record program/dependency versions, runtime feature configuration, provenance and failures. Keep simulation and execution labels distinct, and never double-count them as independent samples from the same decision.

Use historical execution `computeUnitsConsumed` where available, and simulation `unitsConsumed` and `loadedAccountsDataSize` where supplied. Preserve missing values rather than inventing labels. Pair both resources for any policy claiming dual-resource qualification. Supporting legacy or v0 in the application is acceptable; keep existing core v1 support intact and do not claim application v1 compatibility without actual testing.

Add a versioned, freshness-bounded pre-execution state envelope where necessary, including candidate count, queue cursor, existing/missing ATA count, relevant account sizes and initialization/frozen-state evidence, deployment identities, observation slot and snapshot digest. Inputs must be available before execution. Post-execution consumption, logs, outcomes and balances are labels or audit evidence, not predictive features. Preserve exact identity for binding without treating recipient addresses as a substitute for generalization.

Use the simplest genuinely learned estimator adequate to the workload. Retain per-pattern p99 as a baseline. A small CPU quantile model is acceptable if useful; no deep learning, language model or GPU is required. Fit parameters from observations, export a versioned artifact and calibrate conservative limits. Hardcoded thresholds relabeled as a trained model do not satisfy this task.

Group related queues and state snapshots before chronological fit/calibration/holdout splitting. Keep the final evaluation scenarios out of fitting and tuning. Record sample counts, seeds, versions and reproducible data-generation commands. Local measurements demonstrate local behavior, not a production rare-failure guarantee.

Python may host the planner through a local API while TypeScript builds and executes transactions. Include that overhead in measurements. A portable TypeScript model is optional; do not redesign the project solely to avoid a localhost call. Preserve the existing artifact lifecycle: a newly trained candidate is not automatically a trusted release. Scope any qualified demo release to its local program/runtime.

## Planning, binding and fallback

Generate eligible prefix lengths from the same observed queue state. Check actual serialized size, account/version support and deterministic limits first. Select a count whose conservative resource estimates fit declared, fixed policy limits.

Document why the configured resource/cost constraints are relevant. Use the same constraints for all methods. Do not manufacture an advantage by tuning the budget after observing holdout results, silently relaxing limits or making the program unnecessarily expensive. If all realistic candidates fit, report that limitation honestly.

Unknown shapes, missing required resource labels, stale observations, unsupported state, failed deployment checks or insufficient support must trigger bounded simulation, a smaller verified candidate or a pause. Failed simulation is not a usable estimate. Preserve sampled controls, quarantine and recovery behavior. Include the real CPI dependency closure in profile validity checks.

Freeze candidate inputs, model/version, estimates, chosen action and reasoning in the observation store before execution labels arrive. Bind the final transaction, including queue/count/audit fields, to its decision. Changed instructions, recipients, amounts, cursor or unapproved message transformations require replanning. Preserve the established rules for budget replacement and permitted blockhash refresh.

A recorded state snapshot does not freeze chain state. Bound freshness, revalidate relevant assumptions before sending and document remaining races. On-chain checks and atomic rollback must protect payment correctness when resource assumptions become stale. Reconcile actual execution and suspend a profile after a confirmed underestimate as required by its policy.

## Prove model influence and measure usefulness

These are separate requirements:

1. **Causal influence:** on a held-out supported queue/state, compare the learned estimator with an ablation using a fixed estimate under the same policy. Execute both from equivalent isolated starting states. Show that the selected and actually executed batch counts differ, and verify the corresponding real balance changes and cursor/paid-count increments. At least one demonstrated path must execute a qualified model-derived choice; an always-simulate worker does not prove this chain.
2. **Comparative value:** compare full queue completion against an adequately tuned fixed batch, a state-aware deterministic formula/packing rule, a refreshed estimate cache and per-pattern p99. Also report an always-simulate resource-estimation policy. Use equivalent states, constraints and confirmation semantics, and include the new program's overhead.

Show normal execution, a changed ATA-state scenario, and an unknown/deployment-change fallback. Do not hardcode a demo sequence such as 8 -> 4 -> 2. If simulation overwrites every model-derived decision or all actual actions remain identical, the causal requirement is incomplete. Continue reasonable local investigation; do not falsely declare completion or invent a favorable scenario.

A simple baseline may perform as well as or better than a more complex model. That is a valid finding; do not hide it or attribute ordinary batching gains to learning.

Report at least:

- Correct recipients/amounts, completed/pending obligations, duplicate count and actual queue/vault state.
- CU and loaded-data underestimation rates, joint exceedance, eligibility/coverage and fallback rate, with explicit denominators and sample counts.
- Mean, median and p95 resource over-allocation for comparable executed decisions.
- Transaction count, failed attempts, estimation simulations, control simulations, state/deployment reads, retries and total RPC calls.
- Complete preparation and queue-completion timings, including inference, local transport, signing, submission and confirmation where applicable. Separate measured local timings from any modeled remote-latency scenarios.
- Observed fees and rent separately. Do not equate rent deposits with consumed fees or translate fewer simulation calls into an unsupported dollar saving.

Persist the decision-to-signature-to-outcome link so an individual run can be reproduced and audited. Collection and execution must resume after interruption without duplicating observations or payments.

## Demo interface and runnable workflow

Build one small, usable page backed by actual local application state. It must allow creating/funding a test queue, starting or pausing the worker, and inspecting progress. Display approved recipients/amounts, candidate counts, chosen count/reason, prediction versus fallback, transaction status and verified balances/cursor. Distinguish stopping the worker from pausing the program on chain.

Bind the local control service to loopback by default. Never expose signing keys in browser responses, logs or committed files. Recorded playback and synthetic fixtures must be labeled; normal demonstration uses the real program and runtime. A prediction chart alone is not the demo.

Provide documented commands that actually exist in the delivered repository to:

- Bootstrap the supported local toolchain and dependencies.
- Build/deploy the program locally and initialize isolated test identities/token accounts.
- Collect observations, train/calibrate and qualify a local artifact.
- Run a headless end-to-end queue and the interactive demo.
- Run baselines/ablation and export machine-readable plus readable reports.
- Reset only this application's isolated local state and cleanly stop its processes.

Provide a short demo script showing owner approval, a model-driven batch, a state change, a fallback and completion. Include an architecture diagram and known limits. Document Linux/CI and Windows setup realistically where supported.

## Testing and completion gates

Use actual serialization and local Solana execution for integration coverage. Keep default unit tests offline, deterministic and credential-free. Do not silently skip a required runtime test and report a complete end-to-end result.

- **Program:** valid execution; unauthorized owner/executor; wrong queue/mint/program/vault; malformed remaining accounts; recipient substitution; amount inflation; duplicate semantics; overflow; stale cursor; replay; expiry/pause; refund authority/terminal state; atomic rollback after partial attempted work.
- **Planner/core:** supported and unseen counts; no label leakage; stale state; real size/account/resource limits; failed simulation; profile invalidation; held-out evaluation; final-message mismatch; preservation of existing generic unsigned SDK behavior.
- **Application:** real transfers and queue changes; interrupted collection; restart after confirmed or uncertain sends; concurrent stale decisions; bounded retries; no duplicate payments; actual model-to-executed-count ablation.
- **Regression/build:** existing required Python and TypeScript lint/type/test/build checks, the new program build/tests and meaningful local end-to-end checks. Extend CI using pinned tooling and no public-network credentials.

Complete the following milestones in order, adjusting implementation details when evidence requires it:

1. Establish the architecture boundary and implement/test the local program plus deterministic application builder.
2. Collect actual workload data, fit/calibrate an artifact and evaluate candidate resource estimates.
3. Connect planning, binding, execution and resumable observation/reconciliation; demonstrate model causality and fallback.
4. Deliver the page, runnable commands, baseline report, CI and presentation-ready documentation.

The milestone is complete only when a clean supported checkout builds/deploys the local program, reproduces training/evaluation, runs the model-influenced reference flow, proves correct token/state changes, rejects unauthorized/replayed actions, and preserves core compatibility. A fallback-only worker, training script without a trained artifact path, mocked program or presentation alone is partial work. Generated datasets/model artifacts may remain ignored; reproducible commands must recreate them.

## Execution scope and repository hygiene

Install required dependencies, research primary sources and run bounded CPU experiments autonomously. Local deployment and ephemeral test signing/sending are part of this assignment. Do not sign or send mainnet transactions, use real user keys/assets, start large backfills or depend on paid services.

Preserve the existing boundary for public devnet: an RPC URL alone is not authorization to submit transactions. Complete and verify the local implementation and prepare concrete deployment commands/configuration first. If public deployment is still needed and has not already been explicitly authorized, request that authorization only as the final external step; continue independent local work. Do not claim that a local deployment is a publicly accessible devnet deployment.

Keep keys, credential-bearing RPC URLs, local databases, large datasets and generated model artifacts out of Git. Use existing Git identity and authentication. Commit meaningful implementation milestones and push coherent batches to the working branch without rewriting published history. Use ordinary project-focused branch names and commit messages; do not add assistant/tool attribution or phrases such as "AI-generated", "Codex", "ChatGPT", "LLM" or "generated by" to them. Describing the actual learned component in technical documentation is appropriate.

Do not contact third parties, submit hackathon forms, publish packages or deploy production services as part of this task. Do not ask the user to choose routine frameworks or redo the product research. Report concrete blockers candidly and finish all independent authorized work.

## Final report

Report what changed in the reusable core versus the reference application; the program's authority boundaries; exactly how the trained estimator changes executed actions; data provenance and fit/calibration/holdout counts; baseline and ablation results; actual commands and test/build outcomes; commits and push status; files and runnable demo entry points; supported runtime/version combinations; and any remaining public-deployment authorization or event-specific requirement.

Distinguish demonstrated behavior from plans and local evidence from production validation. Never mark incomplete causal influence, program execution or payment correctness as complete merely because the interface is present.

Primary starting points to verify during implementation:

- [Anchor account constraints](https://www.anchor-lang.com/docs/references/account-constraints)
- [Anchor token transfers](https://www.anchor-lang.com/docs/tokens/basics/transfer-tokens)
- [Solana recipient account verification](https://solana.com/docs/payments/send-payments/verify-address)
- [Streamflow Payouts](https://docs.streamflow.finance/en/articles/12639121-payouts)
- [Solana Developer Platform payouts](https://docs.platform.solana.com/docs/payments/send-payouts)
