# Transaction features and pattern identity

`shape-v1` groups pre-execution message structure. A matching pattern means the
messages look alike; account state and program behavior may still change their
compute needs. Calibrated policy checks and simulation fallback remain necessary.

## Accepted input

`parse_transaction` accepts compiled JSON from `getTransaction`, with or without
its JSON-RPC envelope, or an unsigned `{version, message}` wrapper. Request
`encoding: "json"` and `maxSupportedTransactionVersion: 1`. `jsonParsed` and binary
transaction encodings are rejected rather than reconstructed with missing bytes.
Missing versions are accepted only for apparently legacy messages. Missing v1
configuration is rejected because it may indicate an incompatible RPC projection.

For v0, static keys are followed by writable lookup keys, then readonly lookup
keys. Historical `meta.loadedAddresses` resolves the message's lookup references;
this is address resolution, not a prediction label. Exact counts must match
`addressTableLookups`. In a deployment, callers resolve tables before prediction
and pass the resulting `TransactionInput` account list and lookup counts.
Missing resolution is an error. v1 has no lookup tables.

Keys, signatures when present, headers, indices, instruction encoding and v1
configuration widths are validated. A signature array may be omitted from an
unsigned wrapper; its required count comes from the message header. The code
does not validate cryptographic signatures or sign transactions.

## Hash definition

The ID is `shape-v1:` followed by SHA-256 of a canonical, key-sorted JSON object.
Inputs are:

- Transaction version, signature count and lookup-table/count shape.
- Ordered top-level program IDs, instruction discriminators and **exact** data
  lengths. Exact lengths avoid merging potentially different workloads into buckets.
- Account signer/writable/static-or-lookup roles and account-reference topology.
- Effective requested heap size, with an unset request represented as 32,768 bytes.

Discriminators use four bytes for the System Program, one byte for both SPL Token
programs, one byte for Compute Budget, and the first eight available bytes for
other programs. The generic rule is a heuristic, not a universal Anchor decoder:
it can include payload bytes, fragment patterns, or merge distinct suboperations.
Program-specific extractors are the next extension when real datasets justify them.

Account indices are relabeled in first-reference order, with the fee payer anchored
at index zero. Both program and argument references use this mapping. Repeated
references therefore preserve aliases within and across instructions. Unreferenced
accounts contribute a sorted role multiset. Swapping peer account positions while
updating references leaves the pattern stable; changing reference topology does not.

Excluded values are account public keys except program IDs, signatures, blockhashes,
slots, block times, compute-unit request amounts, loaded-data request amounts and
priority fees. Budget instructions still contribute their position, tag and length.
Heap requests remain part of the hash because they can affect execution costs.
Adding/removing a budget instruction changes the pattern; merely changing its CU
limit or price does not. Persisted models must be retrained if this hash contract changes.

## Resource requests and labels

Legacy/v0 budget instructions supply requested limits and micro-lamports per CU;
duplicates, malformed instructions, zero resource limits and invalid heap sizes
produce risk flags. Requested CU/data values are retained without clamping so
callers can inspect the original request. The runtime may cap their effective
values. No blanket per-instruction default compute limit is inferred.

v1 reads `message.transactionConfig`: `computeUnitLimit`, `heapSize`,
`loadedAccountsDataSizeLimit`, and `priorityFee`. The fee is a total in lamports.
Unset CU/data limits become zero and are flagged. Compute Budget instructions in
v1 do not set these values; their presence is flagged. See the official
[versioned transaction documentation](https://solana.com/docs/core/transactions/versioned-transactions)
and [compute budget rules](https://solana.com/docs/core/fees/compute-budget).

Historical labels come only from `meta.computeUnitsConsumed`, never `costUnits`,
logs, requested limits or an invented replacement for missing values. Explicit
`meta.loadedAccountsDataSize` is accepted when supplied by a collection adapter;
ordinary historical metadata commonly lacks this field. It remains `null` then.
Simulation adapters obtain the distinct `unitsConsumed` and
`loadedAccountsDataSize` fields from their simulation response and join those
labels with the original message. Metadata errors determine success; absent
`meta.err` is rejected. Failed executions are retained as failed observations.

`serialized_size` is optional caller-supplied wire size, never JSON string length.
RPC compiled JSON does not report it, so parsing leaves it unset. Post-execution
balances, fees, logs, CPI lists and labels never enter feature extraction.

## Limitations

A shape cannot detect new branches caused by balances, amounts, oracle values,
account growth, Token-2022 extensions, program upgrades or unseen CPIs. Context
epochs and recent held-out calibration reduce risk without proving safety.
Broad patterns require stricter admission or a future state-aware extractor;
the current extractor does not claim semantic program analysis. All committed
fixtures are handcrafted synthetic examples, not captured mainnet evidence.
