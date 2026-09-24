# CU Pilot: Integration and Reliability Builder Prompt

## Role and mission

You are an autonomous senior engineer continuing development of **CU Pilot**, a
Solana developer tool. Work in the existing repository, inspect its current state,
and extend the implementation rather than replacing working components without a
clear reason.

Turn the experimental foundation into a complete, testable integration for an
application that repeatedly builds a controlled family of transactions. Implement
the work described below. Do not stop at a plan, scaffolding, mock-only examples,
or a list of recommendations.

The product remains:

> CU Pilot learns when a separate resource-estimation simulation is unnecessary,
> and performs simulation when a resource profile cannot be trusted.

Treat this prompt as intent, constraints, and acceptance criteria. Choose practical
implementation details independently. Research current official documentation and
source when protocol behavior or SDK capabilities affect correctness. Document
decisions when the evidence requires a different approach.

## Start by inspecting the repository

Read the applicable repository instructions, `README.md`, `pyproject.toml`,
`docs/research.md`, `docs/patterns.md`, `docs/model.md`, `docs/data-flow.md`, the public
schemas, estimator, parser, RPC client, CLI, API, and tests. Check git status before
editing and preserve unrelated user changes.

The original internal version includes:

- Python schemas, compiled JSON parsing, and deterministic transaction patterns.
- Legacy, v0, and v1 input handling, including resolved v0 lookup addresses.
- Per-pattern quantiles, chronological calibration, support/freshness checks, and
  explicit simulation recommendations.
- Historical observation loading, a separate simulation helper, JSON model
  artifacts, an evaluation harness, CLI, and local inference API.
- Offline tests and synthetic examples; an initial suite had 179 tests, but inspect
  the current suite rather than treating that count as a requirement.

Important gaps to verify and close:

- Prediction and fallback execution are not yet one integrated transaction path.
- There is no complete prospective shadow collection and execution reconciliation
  workflow.
- The initial estimator predicts CU only and always falls back for v1.
- There is no local TypeScript integration/runtime for a real transaction builder.
- Deployment context and artifact lifecycle are largely operator managed.
- Mocked RPC and handcrafted fixtures do not establish correctness with real
  serialization and a local execution environment.

## Scope and priorities

Complete all six workstreams below. Deliver them in coherent milestones.

The **first milestone** should contain a unified estimation adapter, a shadow
collector, and an integration test using a real builder and local Solana execution.
This is the first delivery boundary, not the stopping point. Continue through
dual-resource estimation, TypeScript inference, lifecycle controls, evaluation,
documentation, and verification.

Choose one narrow example workload whose builder we control, such as repeated
batch operations or settlement-like instructions. A standard token transfer can
serve as a control fixture. Do not claim that a test workload is a real external
customer or evidence of demand.

## 1. Implement one resource-estimation path

Provide a practical interface, such as `estimate_resources(...)`, that:

1. Accepts a supported transaction/message or builder adapter.
2. Normalizes and validates the actual message being evaluated.
3. Resolves the necessary pre-execution inputs and extracts its features.
4. Checks artifact, deployment, freshness, shape, and policy eligibility.
5. Returns accepted resource limits or actually invokes fallback simulation.
6. Produces a structured decision and observation identifier for later auditing.

The result must distinguish an accepted prediction, a successful simulation,
and an unresolved failure. Include relevant resource limits, decision reason,
pattern/profile/model versions, context, observation slot, timings, and provenance.
A timeout or failed simulation must never become a fabricated successful estimate.

### Bind the decision to the transaction

- Do not accept an arbitrary feature object alongside unrelated serialized bytes
  and assume they describe the same transaction.
- Derive both the feature representation and simulation input from a single
  authoritative message, or verify a rigorously documented equivalence contract.
- Distinguish a grouping `pattern_id` from an identity/fingerprint that binds a
  decision to a particular message. Pattern equality alone is insufficient.
- Prepare the final instruction topology before feature extraction. In legacy/v0,
  adding a budget instruction afterward changes both the shape and execution cost.
- Define which transformations are controlled: replacing existing resource fields,
  applying explicitly allowed blockhash refreshes, and any SDK normalization.
  Rebind or re-evaluate when required. Other instruction/account/data/heap changes
  must invalidate the decision.
- Explicitly document assumptions about programs inspecting budget instructions or
  remaining compute. Restrict unsupported budget-sensitive workloads to simulation.
- Preserve fee settings and instruction ordering unless a documented adapter
  operation intentionally changes them. Respect nonce semantics.
- Return unsigned output where rebuilding is necessary. Never imply that existing
  signatures remain valid after message changes. Signing and sending belong to the
  caller, outside the production library's responsibilities.

### Execute fallback correctly

Use a caller-supplied RPC/client or controlled configured endpoint. Prepare adequate
simulation budgets using the supported SDK and the transaction version's rules.
Do not simulate a known insufficient budget and call truncated usage an estimate.

Handle RPC errors, missing required measurements, timeouts, cancellation, rate
limits, bounded retries, commitment, and minimum context slots explicitly. Reuse
the current validated RPC behavior where appropriate. Retry only retryable
failures; do not retry deterministic transaction errors indefinitely.

Do not silently clamp an unsafe required limit to a protocol cap. Explain the
failure and leave the transaction unresolved. Do not expose credential-bearing
endpoints or secrets in errors or logs.

Resource estimation does not replace security, payment, slippage, account
resolution, wallet preview, or other required validation. Do not change preflight
policy implicitly or count an indispensable validation simulation as removable.

## 2. Add prospective shadow collection and reconciliation

Implement a CLI/library workflow, such as `cu-pilot shadow`, that observes the
prediction first and still simulates every input. It must collect data without
enabling prediction-based skipping by default.

Persist enough information to reconstruct each decision:

- Unique request/observation ID and message identity.
- Pre-execution features, pattern, allowed workload context, and deployment identity.
- Artifact/profile/policy versions and the prediction made before seeing its label.
- The corresponding simulation context, CU, loaded-data bytes, error status, and
  elapsed times.
- Actual execution outcome when a caller later provides a signature or result.
  Missing outcomes must remain explicitly missing, pending, or unavailable.
- The relationship between original, simulation-prepared, and final messages.
  Do not claim exact equality when budgets or blockhashes differ.

Historical CU labels must come from `meta.computeUnitsConsumed`, not `costUnits`.
Simulation labels use `unitsConsumed` and `loadedAccountsDataSize`. Preserve the
distinction between prediction-versus-simulation error and prediction-versus-real
execution error. Do not silently mix those sources during training or reporting.

Choose simple durable storage: SQLite is reasonable for events, checkpoints, and
joins; JSONL export should remain easy to inspect and compatible with experiments.
Avoid adding a distributed data platform.

Implement resumable collection, atomic checkpoints, idempotent ingestion,
deduplication, conflicting-record handling, bounded queues/concurrency, backpressure,
and configurable RPC rate limits. Retain failed and missing-label observations for
auditing without using partial failed execution as successful resource demand.

Reconciliation must verify message/signature correspondence where available,
handle configured commitment/finality and possible outcome changes, and avoid
double counting retries. Do not replay a historical transaction against today's
state and label that result as its historical execution.

Live external collection stays optional. Supply an offline replay workflow and a
local integration example requiring no paid provider or private credentials.

## 3. Estimate both resources and calibrate their joint risk

Extend the data/model/decision contract to support:

- Compute units.
- Loaded-account data size.

Parse and apply resource requests according to legacy/v0/v1 semantics. Verify
current official behavior rather than copying older SDK examples. Respect v1's
message configuration, fee units, resource defaults, and wire format.

Keep a transparent quantile baseline first. Missing loaded-data labels must not be
imputed from CU, account counts, or an assumed constant. Keep unsupported resource
profiles on simulation. Apply justified margins and documented rounding rules;
evaluate the rounded final limits that the caller will actually use.

The acceptance target concerns **either resource limit being exceeded**. Two
independent marginal acceptance tests do not automatically bound this joint event.
Implement and explain a joint calibration rule, or a conservative risk allocation
with explicit assumptions. Use paired observations for the joint event, and make
missing-resource eligibility and denominators visible.

Preserve these statistical safeguards:

- Freeze chronological fit/calibration/test boundaries before filtering records.
- Keep a slot and duplicate observations out of multiple partitions.
- Count only calibration examples that would pass the same non-label eligibility
  checks used by inference; ineligible rows cannot dilute the measured risk.
- Learn limits and feature ranges from fitting data only. Calibration checks them;
  test data does not tune margins, profiles, or thresholds.
- Handle temporal correlation and support counts honestly. Many rows from one
  execution burst are not independent evidence.
- Report uncertainty, abstention, and unknown patterns. Do not call an empirical
  upper bound a guaranteed probability of future transaction success.
- Treat the current experimental risk threshold as a research default, not an
  approved production SLO. Make risk targets configurable and documented.

Do not enable v1 skipping merely because its parser accepts v1. Enable it only for
profiles meeting the full dual-resource policy, while keeping default examples
and release behavior appropriately conservative.

Version schemas and artifacts when required. Reject incompatible artifacts clearly
or provide a tested migration; never silently reinterpret old CU-only artifacts
as evidence for loaded-account limits.

## 4. Provide a real TypeScript builder integration and local runtime

Keep Python for data processing, fitting, calibration, and artifact production.
Add a small typed TypeScript package, preferably integrating with current Solana
Kit APIs, for application-side decisions and controlled message preparation.

Support a clear flow:

> builder -> bound message/features -> local profile decision -> predicted limits
> or SDK simulation -> unsigned prepared result -> caller-controlled signing/sending

The accepted-prediction path should not require a remote prediction service.
Account for any additional state reads, artifact refresh, logging, and deployment
checks when measuring the total cost of the integration.

Add cross-language contract fixtures and tests covering:

- Identical canonicalization and `pattern_id` values.
- Program/account-role and alias topology, instruction discriminators and lengths.
- Resource and fee interpretation for all supported versions.
- Equivalent decisions, numerical boundaries, rounding, null/missing values,
  freshness, and unsupported artifacts.
- JavaScript numeric precision: use exact integer representations where values can
  exceed the safe integer range, especially fees and identifiers.
- Real v0 lookup resolution and v1 serialization from the selected SDK.

Avoid duplicating the training stack in TypeScript. Keep the artifact format
portable, validated, and non-executable. Expose useful types and a runnable example.
Retain the Python CLI/API where they help local inspection and compatibility.

## 5. Implement resource-profile lifecycle and operational controls

Add an explicit profile state model, such as candidate, shadow, active, suspended,
and retired. Define which transitions are automatic and which require an explicit
operator release action.

Profiles need stable identities, revisions, provenance, training/calibration ranges,
risk/support summaries, workload allowlists, cluster identity, compatible runtime
assumptions, and a binding to program deployments.

Implement program-change detection for supported program loaders using verified
deployment identity, deployment slot, or code fingerprint as appropriate. Cover
known invoked dependencies when available; top-level program identity alone does
not prove that CPI dependencies are unchanged. If a relevant dependency cannot be
tracked, document the limitation and require a restrictive policy or fallback.

Use cached checks or a bounded watcher where practical. Define freshness and
failure behavior for the watcher itself; stale or unavailable change-detection
evidence must not silently count as a successful check. Avoid introducing an
extra uncontrolled RPC round trip on every accepted prediction.

Implement:

- Validated, atomic artifact activation and safe concurrent reads.
- Explicit compatibility checks and model/profile version visibility.
- Retention of prior artifacts and an auditable rollback operation.
- Rollback that rechecks deployment/freshness; an old artifact is not safe merely
  because it previously worked. Preserve suspension/quarantine evidence; rollback
  must not manufacture fresh observations or silently re-enable a rejected profile.
- Configurable sampled control simulations, with recorded selection and outcomes.
- Suspension when observed resource excess, incompatible program changes, stale
  evidence, or configured deterioration rules invalidate the profile.
- An emergency option to force simulation and a clear recovery/requalification path.

Keep control observations separate from the decision they audit. Sample before
seeing outcomes, retain the decision version, audit eligibility and sampling
probability, and account for control-simulation overhead in reported savings.
Distinguish fully observed shadow data from selectively audited deployment data;
do not ignore selection bias. Do not auto-promote a profile based on synthetic tests.

## 6. Validate with real serialization and local execution

Keep fast deterministic offline tests as the default suite. Add a separately
invocable integration suite using a supported local Solana environment, such as
Surfpool, LiteSVM, or a local validator. Select the tool based on the capabilities
needed, including actual v1 support and program upgrade tests.

Use real SDK-generated messages and actual simulation/execution responses. Include
at least one builder-to-decision-to-fallback example. Test-only local fixtures may
use ephemeral in-memory signing keys when execution is necessary; never commit,
print, or use real user keys. Production code still does not sign or submit.

Cover the scenarios that can cause false acceptance:

- Known eligible shape and unknown shape.
- Exact message/serialized input mismatch.
- Controlled resource replacement versus other mutations after prediction.
- Invalid, duplicate, insufficient, and near-cap budgets.
- Account creation, changing data size, closed/recreated accounts where relevant,
  and altered batch composition.
- Changes to supported programs and known invoked dependencies.
- Stale observations, stale lookup/deployment evidence, and invalid current slots.
- Missing loaded-data measurements and joint CU/data exceedance.
- Deterministic transaction failures, transport failures, rate limits, bounded
  retries, cancellation, and interrupted collection.
- Collection restart, conflicting duplicates, reconciliation, and artifact rollback.
- v0 lookup tables, v1 configuration, and local SDK serialization compatibility.
- The specific eligibility, chronology, and coercion regressions already covered
  by the existing tests.

Default tests must remain network-independent. Provide explicit integration
commands, prerequisites, environment/version reporting, and a suitable CI job.
An explicitly requested integration run must report missing prerequisites as an
incomplete check, not silently skip everything and claim success.

If one emulator cannot cover a required behavior, use another appropriate local
tool or document the remaining limitation precisely. Do not substitute mocked
behavior and describe it as a real runtime test.

## Evaluation and product evidence

Extend the harness to compare:

- Always simulate.
- The caller's existing resource-sizing policy where available.
- Fixed per-operation limits or justified deterministic program-specific rules.
- A simple last-successful/profile cache with explicit refresh behavior.
- Existing per-pattern p95/p99 baselines.
- The complete new policy, including lifecycle checks and control simulations.

Measure coverage/fallback, each resource's underestimation, joint underestimation,
excess resource limits, failure reasons, and support/uncertainty per profile.

Measure full preparation latency and its p50/p95/p99, not only model inference.
Report avoided resource-estimation calls separately from preflight, business
validation, and control simulations. Include added state reads and other overhead.
In shadow mode, avoided calls and savings are counterfactual estimates, since all
inputs are still simulated. Label synthetic, local-runtime, live-simulation, and
historical-execution evidence accurately.

Do not infer a p95 latency improvement by multiplying a mean RPC time by coverage.
Do not promise improved landing, lower network fees, or production reliability from
resource-only metrics. Evaluate fees according to transaction version and actual
configuration; in particular, an unchanged absolute priority fee is not reduced
simply by lowering a resource limit.

Only add a CPU quantile regressor if real evidence shows a useful advantage over
these simple baselines at the same risk budget. A stronger ML model is optional;
the integration, lifecycle, collection, and tests above are not.

## Engineering constraints and autonomy

- Keep dependencies small, public interfaces typed, and the project runnable without
  paid services. Use a lockfile for the new TypeScript package.
- Do not build a dashboard, hosted multitenant service, new on-chain application,
  distributed training platform, or custom RPC infrastructure. A minimal local test
  program is acceptable only if needed to exercise an otherwise untestable case.
- Never sign or send real mainnet transactions. Any public-network reads or
  simulations must be optional, bounded, and documented. Devnet submission needs
  explicit authorization beyond merely having an RPC URL.
- Do not commit secrets, private keys, credential-bearing provider URLs, large
  datasets, local databases, or generated model artifacts.
- Keep historical collection opt-in; do not launch a large backfill or paid run.
- Preserve established correctness tests and useful compatibility. Explain schema
  changes, migrations, unsupported cases, and changed behavior.
- Make routine implementation decisions independently and continue through all
  authorized milestones. If optional live access is unavailable, complete the
  offline/local integration and state what real-data validation remains.
- External interviews, partner outreach, public messages, package publication, and
  production deployment are outside this implementation task.

## Git and GitHub

Use the existing local git identity, authenticated GitHub setup, and repository
remote. Preserve unrelated changes. Commit after meaningful milestones and push
coherent tested batches when the configured remote is available. Do not rewrite
published history.

Use normal project-focused branch names and commit messages. Do not include
assistant/tool attribution, "AI", "Codex", "ChatGPT", "LLM", or "generated by" in
commit messages, branch names, or new public-facing attribution.

Example milestone commits:

- Add bound resource estimation with simulation fallback
- Add resumable shadow observations and outcome reconciliation
- Calibrate joint compute and account data limits
- Add local TypeScript resource policy integration
- Add profile activation, suspension and rollback
- Validate resource policies with local runtime integration tests

## Definition of done

Do not mark this task complete until:

1. A real supported builder can call a single interface and receive an accepted
   prediction, an actual fallback-simulation result, or an explicit unresolved error.
2. The decision is bound to the transaction; unapproved mutations are detected.
3. Shadow collection resumes reliably, preserves provenance, and can reconcile
   caller-provided execution outcomes without contaminating features or labels.
4. Dual-resource profiles and joint calibration exist; unsupported or insufficient
   v1 evidence still produces fallback.
5. A local TypeScript runtime/adapter agrees with Python on shared contract fixtures.
6. Deployment changes, stale evidence, observed excess, activation, suspension,
   recovery, and compatible rollback have implemented and tested behavior.
7. Offline checks pass and the actual local integration suite has run successfully
   for the supported matrix, with every material gap stated explicitly.
8. Evaluation includes simple static/cache competitors and accurate latency/resource
   denominators. Synthetic results are not presented as production evidence.
9. Setup, examples, architecture, artifact compatibility, operator actions, tests,
   and limitations are documented; the repository is clean except for explicitly
   preserved pre-existing user changes.

If an external prerequisite genuinely prevents one item, complete independent work
and report the exact blocked capability and evidence. Do not describe the whole
objective as complete or quietly remove the requirement.

## Final report

Report what was implemented, the complete runnable workflow, public interfaces,
files/directories added, schema/artifact changes, commands run, test and integration
results, measured versus assumed benchmark results, commits and push status,
remaining limitations, and the next real-data validation step.

Clearly distinguish a completed integration from a production-validated model.
