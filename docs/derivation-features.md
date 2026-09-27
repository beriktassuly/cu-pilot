# Offline derivation feature experiment

`examples/payouts/derivation_features.py` is a separate research module. The application does not import it. Its versioned formula artifact carries `release_authority: false` and cannot activate a profile or authorize a payout. The existing model and bridge contracts are unchanged.

The public inputs are each recipient address, mint address, and account presence from the pre-execution snapshot. For each recipient the module derives the associated token account using the token program seed and associated token program. It checks the address against the snapshot. The public bump determines the number of attempted addresses in one descending search: `256 - bump`. This is a feature describing one search per recipient, not a measurement of every operation the program performs.

The pinned implementation is `@solana/addresses` 8.3.0 in `typescript/node_modules/@solana/addresses/src/program-derived-address.ts`, lines 160–170. It starts at 255 and decrements on an on-curve rejection. The program uses the same seeds in `programs/payout_queue/src/lib.rs`, in `ata_address`. Solders 0.29.0 exposes the resulting public bump. An offline test checks its result against an explicit descending sequence of `create_program_address` attempts. The CLI records the pinned TypeScript source hash.

Feature contract `cu-pilot-ata-derivation-effort-v1` contains candidate count, missing account count, total ATA search attempts, missing ATA search attempts, and public bumps. The experiment compares a three-term count/missing formula against a five-term formula that adds total and missing search attempts. Both use ordinary least squares, nonnegative coefficient clipping, and the maximum positive fitting residual as an upper envelope. Both then apply the existing 10% margin and rounding. Loaded-data limits use the same fitting-only per-count 99th percentile maximum. No coefficients, residuals, feature ranges or margins use calibration or holdout labels.

The planned dataset is 3,520 new local simulation observations in 80 separate queue cohorts, collected after freezing the plan and module hash. Queue identity, snapshot identity and overlapping slot intervals join related observations before the chronological 50%/30%/20% split. Recipients must also be disjoint across partitions. A single fresh bank remains a local experiment and does not establish production independence.

The module preserves `LOCAL_POLICY`: 12 fitting cohorts, 20 calibration cohorts, joint CU/data Wilson upper bound at most 0.15, 216,000-slot fitting freshness and all existing structural and resource limits. Calibration is separate for each exact state/pattern cell. Failed or incomplete calibration labels do not count as successful demand evidence. New feature values outside fitting ranges cause abstention. Holdout rows include predictions, abstentions, failures, missing labels and resource exceedances. Reports compare both overall coverage and the matched set accepted by both formulas.

Run once on the new dataset after collection:

```sh
.venv/bin/python -m examples.payouts.derivation_features /absolute/path/observations.jsonl \
  --output /absolute/path/new-feature-evaluation \
  --evidence-status fresh-prospective \
  --plan /absolute/path/feature-plan.json
```

The prospective mode verifies the frozen module hash, 3,520 unique records, 80 independent cohorts, and local simulation provenance. The operator must still establish that collection followed the plan; filenames alone do not prove that. Existing output directories are rejected. The output contains `formula-artifact.json`, `evaluation.json`, all derived features, and holdout predictions. It reports feature extraction time separately, but does not measure payout time, simulations avoided, RPC savings or fees. A lower requested CU limit with zero CU price does not establish fee savings.
