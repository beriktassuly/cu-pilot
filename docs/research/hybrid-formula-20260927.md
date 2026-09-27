# Decision: an experimental PDA-aware hybrid

Research date: 2026-09-27. This concerns the local payout reference application,
not universal Solana resource prediction.

The earlier experiment made the five-term formula worth testing, but its
maximum fitting residual plus 10% margin was a heuristic. The new method keeps
the public pre-execution features, fits actual nonnegative least squares,
calibrates a joint upper envelope on separate whole cohorts, and requires
another partition for release qualification. Unsupported cases use simulation.

## Formula

For candidate payment count $n$, missing recipient ATAs $m$, and canonical
bumps $b_i$, define

$$
x=(1,n,m,a,a_m),\quad a=\sum_i(256-b_i),\quad
a_m=\sum_{i:\mathrm{missing}}(256-b_i).
$$

For each resource $r$, compute units and loaded account bytes:

$$
\beta_r=\arg\min_{\beta\ge0}\sum_{\mathrm{fit}}(y_r-\beta^\top x)^2,
\qquad f_r(x)=\beta_r^\top x.
$$

Clipping an unconstrained least-squares solution is not NNLS. For
$X=((1,0),(1,1)), y=(1,0)$, clipped OLS gives $(1,0)$ and squared error 1;
NNLS gives $(0.5,0)$ and error 0.5. The implementation enumerates at most
32 active coefficient sets, solving each feasible face with scaled,
reorthogonalized QR. Inference needs no heavy dependency.
[NNLS definition](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.nnls.html).

For each complete calibration cohort $g$:

$$
S_g=\max_{j\in g,\ r\in\{\mathrm{CU},\mathrm{bytes}\}}
\left[0,\frac{y_{gjr}-f_r(x_{gj})}{\max(f_r(x_{gj}),1)}\right].
$$

For $G$ calibration cohorts:

$$
k=\lceil(G+1)(1-\alpha)\rceil,\quad q=S_{(k)},\quad
U_r(x)=\operatorname{roundUp}_r(f_r(x)+q\max(f_r(x),1)).
$$

If $k>G$, prediction must abstain. Exact rational rank arithmetic and upward
resource rounding avoid silently weakening the requested confidence.
The cohort maximum handles dependent candidate rows inside a sampled cohort;
the resource maximum calibrates the event “either resource exceeded.”
Two separate 95% bounds do not imply 95% joint coverage. This is our application
of general split conformal calibration, not a new cryptographic theorem.
[Angelopoulos and Bates, 2021/2022](https://arxiv.org/html/2107.07511v6).

## Assumptions and sample size

The rank argument requires exchangeable complete calibration and future cohort
draws. It gives marginal predictive coverage, averaged over calibration and
future draws. It is not a high-confidence failure-rate bound for one frozen
artifact, nor a conditional guarantee among only accepted planner decisions.

| Nominal joint error | Minimum calibration cohorts for a finite bound |
|---|---:|
| 5% | 19 |
| 1% | 99 |
| 0.1% | 999 |

Eighty collection cohorts produce 40 fitting, 24 calibration and 16 qualification
cohorts. The last partition is explicitly **qualification**, consumed by the
release decision. Final comparison queues are fresh after model and policy
choices are frozen. Correlated rows do not increase independent sample size.

The permissive local-development Wilson gate is not a production SLA. With
zero failures in 16 independent validation cohorts, the exact one-sided 95%
binomial upper limit is about 17.1%. A zero-failure upper bound no greater than
1% would require at least 299 independent validation cohorts, before considering
drift and selection.
[NIST exact binomial methods](https://www.itl.nist.gov/div898/software/dataplot/refman1/auxillar/propconf.htm).

Relevant recent research:

- **Calibration after Adaptive Pick, JMLR 2025** studies selection before
  reporting conformal intervals. Evaluate the accepted subset and fallback
  policy together. CAP is not implemented here.
  [Publication](https://jmlr.org/papers/v26/24-0452.html).
- **Generalized Hierarchical Conformal Prediction, August 2026 preprint**
  considers grouped data with partial observations from a new group. It does
  not justify treating correlated prefix rows as independent. GHCP is not
  implemented here.
  [Preprint](https://arxiv.org/abs/2608.15500).
- **Conformal prediction beyond exchangeability** requires extra machinery
  for drift. A program upgrade cannot be solved by reusing an empirical
  quantile. Keep version binding, freshness, controls and suspension.
  [Paper](https://arxiv.org/abs/2202.13415).
- **Conformalized Quantile Regression** is a later comparator if residual
  heteroscedasticity justifies a more complex model.
  [Paper](https://arxiv.org/abs/1905.03222).

## Crypto and protocol consequences

Canonical PDA search is deterministic from public inputs but has variable
execution effort. Its population average is not a safe per-transaction bound:
counterparties can select expensive seeds. A separate high-attempt stress
phase tests this boundary. The feature describes particular canonical searches,
not every syscall or cryptographic operation inside a transaction.

Coefficients must be fitted for the actual program/runtime. Scope includes
dependency deployment fingerprints, not just program IDs. See the companion
[Solana source review](hybrid-solana-20260927.md).

No directly applicable primary-source CU-prediction paper found in this review
superseded the measured simple baselines. ZK proofs, consensus changes and other
cryptographic primitives do not improve this small estimator merely because
they are mathematical cryptography.

## Decision and promotion criteria

The opt-in method is named adaptive_derivation. Formula artifacts and profiles
are separate from quantile releases. The exact message, public derivation,
sealed application state, runtime, deployments, support ranges, freshness and
active profile must agree. The application owns approved payments, account-state
evidence, signing and submission.

Only immutable address/bump calculations are cached. Account existence,
balances, ownership, eligibility and deployment evidence are not cached by that
optimization. The on-chain program and transaction version remain unchanged.

Compare complete policy behavior against adaptive, always_simulate and
scoped_fixed_batch. A three-term NNLS/conformal ablation is fitted on the same
collection for follow-up analysis; the old heuristic source remains intact.

Promote only if fresh comparisons improve over the strongest simple competitor
without hiding failures or changing the evaluation set after seeing results.
Lower CU reservation alone is insufficient: at zero CU price it saves no
priority fees, and a local bank does not measure remote RPC latency.
