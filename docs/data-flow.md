# Data flow and integration contract

## Inputs

`normalize` consumes newline-delimited objects returned by `getTransaction` with
`encoding: json` and `maxSupportedTransactionVersion: 1`. Both a result object and its
JSON-RPC envelope are accepted. `jsonParsed` is rejected because it can omit raw instruction
bytes and does not provide a stable feature contract. RPC collection asks for finalized data.

The JSON representation of `TransactionInput` is the pre-execution API contract: resolved
accounts with signer/writable roles, ordered compiled instructions with hex bytes and account
indices, signature count, version, lookup counts, and v1 config if applicable. Construct it
from your transaction builder before signing. Do not populate it from execution logs.

For historical v0 transactions, `meta.loadedAddresses` resolves address-table references.
Those addresses and permissions are message inputs, not outcome labels. At prediction time
the caller must resolve the same lookup tables from a compatible bank state. Incomplete
lookup data cannot qualify. No automatic account-state or program-binary snapshot is collected.

## Stored observations

Each JSONL row contains `record_id`, `slot`, `context`, `source`, `features`, and `label`.
Signatures identify historical records; slot determines chronology and age. Sources are
`historical`, `simulation`, or `synthetic` and must not be mixed in one fitted artifact.
Use contexts such as `cluster-genesis/deployment-id/runtime-policy`; changing just a label
does not verify a cluster. The operator owns truthful provenance and context selection.

Historical CU comes only from `meta.computeUnitsConsumed`. `costUnits` measures a different
cost. Errors and missing measurements are preserved rather than replaced with zero. Loaded
account size is a separate optional label; no account-balance or log-derived proxy is used.
Failed execution can report truncated consumption and cannot train a successful CU budget.

The collector intentionally does not discover signatures, backfill blocks, or store raw RPC
URLs. Store local datasets under `data/` and artifacts under `artifacts/`; both are ignored.
Keep any raw evidence privately and record provider software/version separately. Older nodes
may return incomplete v1 configuration; see the dated research notes.

## Artifact and inference

Models are validated JSON, never executable pickle files. They include an artifact version,
policy, source, context, latest slot, per-pattern support and calibration statistics, observed
numeric ranges, and fit diagnostics. Treat artifacts and the Python `Features` interface as
trusted inputs; the public API accepts transactions and derives features itself.

`POST /predict` takes `transaction`, `context`, and `current_slot`. Obtain the latter from
your current RPC view; supplying an old slot defeats freshness checks. `/health` reports
whether an artifact is loaded. Invalid structured inputs receive 422; malformed transaction
semantics receive `invalid_transaction` with simulation recommended. No HTTP endpoint accepts
RPC URLs, signs, sends, or retrains a model.

The recommendation is a resource policy decision, not transaction validation. The caller must
also handle missing accounts, balances, ownership, slippage, account state, nonce semantics,
and all other execution conditions. Accepted legacy/v0 predictions leave loaded-data and heap
configuration intact. Changing the transaction requires extracting features and predicting again.

## Evaluation

Exact duplicate observations are collapsed before splitting; conflicting record IDs are
errors. All observations from one slot remain together. Chronology is development then test;
the estimator splits development again into fit then calibration. Support aggregates the
worst successful consumption per pattern per slot to limit bursts inflating sample counts.

Coverage counts accepted decisions over all test rows. CU underestimation is `actual > limit`
among accepted successful rows with CU labels. It is `null` when no prediction can be scored.
Excess is `max(limit - actual, 0)`, including a zero for underestimates; evaluate it alongside
underestimation rather than in isolation. The report also counts failed/unlabeled test rows.
Historical replay cannot measure the actual result or accuracy of a fallback simulation.

Latency scenario: `local inference ms + fallback rate * assumed simulation ms` for CU Pilot.
Always-simulate uses the supplied RPC assumption. Other baselines omit local overhead.
Network contention, state-fetch costs, retries, and tail latency require real shadow measurements.
