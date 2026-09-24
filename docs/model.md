# Estimation and simulation fallback

This page describes the preserved **CU-only compatibility estimator**. New builder integrations
use the separately versioned [dual-resource model](resource-model.md), [bound estimation
path](integration.md) and [profile lifecycle](lifecycle.md). CU-only artifacts are never
reinterpreted as loaded-account-data evidence.

The first predictor is an empirical per-pattern quantile. Its default is p99 with
a 10% margin, rounded upward. Set `Policy(quantile=0.95)` for the p95 variant.
This choice keeps the strongest simple baseline transparent and dependency-free.
The model estimates compute units only. It never invents a loaded-account byte
limit from historical compute labels.

## Fit and calibration

Fit accepts observations from exactly one `context` and one source (`historical`,
`simulation`, or `synthetic`). Context should encode the cluster and a user-managed
program/deployment epoch. A program upgrade or execution-environment change needs
a new context and new observations. The tool cannot automatically detect all
upgrades or changes to account state.

Duplicate record IDs are counted once; conflicting duplicates fail. Failed
transactions, missing/invalid compute labels, and unsafe feature records are
excluded, with counts recorded in the artifact. Compute used before a transaction
failed does not represent the cost of a successful execution.

The last 30% of distinct input slots after deduplication form a chronological
calibration partition. Freeze this time boundary before label/feature filtering;
otherwise excluding risky late examples could pull easy earlier examples into
calibration. All patterns share the same boundary. No slot occurs in both partitions. A pattern
seen only during calibration stays unknown. An external evaluation set must come
strictly after the complete fitted artifact; the evaluator is responsible for its
own chronological test boundary.

Within each pattern and slot, use the maximum observed successful compute usage.
Consequently, sample counts represent distinct slots, not transactions. This avoids
claiming extra statistical support from hundreds of closely related transactions
in one slot, and guards against a cheap sample hiding an expensive same-slot one.
It does **not** prove that observations in different slots are independent.

Training alone determines the nearest-rank quantile, proposed limit, and numerical
feature ranges. Calibration rows must pass the same feature eligibility guards as
predictions, including numerical support and an existing positive compute-limit
instruction. Filter rows **before** taking per-slot maxima. Otherwise many easy
out-of-distribution examples could dilute failures among the few transactions
actually eligible for prediction. Excluded rows are counted in
`ineligible_calibration_count` and do not refresh pattern freshness. Calibration
labels never raise the proposed limit or expand feature support. Defaults require
at least 30 training slots and 100 eligible calibration
slots for the pattern. At least roughly 334 well-distributed slots per pattern are
therefore needed with a 30% calibration fraction; more may be necessary when the
global split leaves uneven per-pattern support.

For calibration slot maxima, count values strictly above the proposed limit.
Compute the one-sided 95% Wilson upper bound on this underestimation rate, using
`z = 1.6448536269514722`. Require that bound to be at most 5% by default. With zero
underestimates among 100 calibration slots, the bound is about 2.63%; zero observed
failures does not mean zero risk. This is an approximate interval for historical
slot samples under sampling assumptions, **not** a calibrated probability of the
next transaction succeeding, a guarantee, or a multiple-pattern risk certificate.
Distribution shift and correlated slots can invalidate its interpretation.

## Abstention policy

Prediction returns a limit only after every guard passes. Otherwise it returns
`simulation_recommended=true`, `compute_unit_limit=null`, and a specific reason:

- Context mismatch, backwards prediction slot, or stale pattern observations.
- Version 1 transactions, extraction risk flags, invalid or incomplete features.
- No existing positive compute-limit instruction (`missing_compute_limit`).
- An explicit nondefault loaded-account cap (including a cap below 64 MiB).
- Unknown pattern, too few training/calibration slots, or failed calibration bound.
- Program/version mismatch or a numerical feature outside its training range.
- An estimated limit near the 1,400,000 CU cap (90% by default) or reaching the cap.

Numerical support includes account/signature/instruction counts, instruction byte
lengths, lookup counts, serialized size when available, and requested heap size.
Missing optional values are accepted only if missing was observed during training.
These min/max checks catch simple extrapolation, but cannot detect every unusual
combination or account-state change. Budget prices and requested CU limits are not
learned resource labels and are not used as predictors; valid compute limits may
be replaced by the returned estimate by the caller. For legacy/v0, insert a
compute-limit instruction **before** feature extraction, then replace its value
after prediction. Adding that instruction only afterward changes the transaction
shape and itself costs compute. Historical rows without this instruction can
contribute to training/baseline analysis, but cannot calibrate or receive accepted
predictions. The compute-limit value is deliberately excluded from numerical
support so replacing it does not create a circular input. Loaded-account limits
remain unchanged. The estimator never silently clamps a required CU estimate to
the cap. The Python prediction interface requires `current_slot` to be a
nonnegative integer and rejects floating-point, NaN, boolean, and string values.

Unknown account-state effects remain a key limitation of shape-only prediction.
Use audited pattern allowlists, conservative policy settings, and real chronological
holdout data before relying on estimates. A successful simulation is also not a
future execution guarantee because chain state can change.

## Artifacts and optional ML path

`PatternEstimator.save` writes a versioned JSON artifact containing policy, context,
source provenance, split slots, diagnostics, and per-pattern statistics.
`PatternEstimator.load` rejects unknown versions, unknown fields, invalid ranges,
overlapping split slots, inconsistent calibration bounds, and limits inconsistent
with the saved margin. Artifacts contain no pickle or executable estimator state.
Validation catches malformed artifacts; it does not authenticate their origin.
Load trusted model files and rebuild when data or policy changes.

No fitted machine-learning model is needed for this first version. A future
CPU-only quantile regressor (for example scikit-learn gradient boosting) should
train on pre-execution features, use a separate chronological calibration set,
retain unknown-pattern/context/freshness/resource gates, and compete with p95/p99
on an untouched chronological test set. It must improve coverage and excess CU at
the same underestimation tolerance to justify the added dependency and complexity.
Do not use the test set to choose quantiles, margins, or fallback thresholds.
