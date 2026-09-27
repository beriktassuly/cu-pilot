"""Mathematical contracts for dependency-light conservative formulas."""

from __future__ import annotations

import random

import pytest

from cu_pilot.formula import (
    conformal_quantile,
    conformal_rank,
    formula_limit,
    joint_score,
    nonnegative_least_squares,
    point_prediction,
)


def test_nnls_refits_free_coefficients_instead_of_clipping_ols():
    coefficients = nonnegative_least_squares([(1, 0), (1, 1)], [1, 0])
    assert coefficients == pytest.approx((0.5, 0))
    assert sum(
        (point_prediction(coefficients, x) - y) ** 2
        for x, y in zip([(1, 0), (1, 1)], [1, 0], strict=True)
    ) == pytest.approx(0.5)


def test_nnls_recovers_five_term_additive_cost():
    rng = random.Random(92)
    vectors = [(1, *(rng.randint(0, 50) for _ in range(4))) for _ in range(100)]
    coefficients = (3000, 4000, 5000, 1500, 2500)
    targets = [int(point_prediction(coefficients, x)) for x in vectors]
    assert nonnegative_least_squares(vectors, targets) == pytest.approx(coefficients, rel=1e-10)


def test_nnls_convex_optimality_on_noisy_correlated_design():
    rng = random.Random(718)
    vectors = [(1, *(rng.randint(0, 20) for _ in range(4))) for _ in range(80)]
    targets = [max(0, 100 + 2 * x[1] - 12 * x[2] + rng.randint(-10, 10)) for x in vectors]
    fitted = nonnegative_least_squares(vectors, targets)
    residuals = [point_prediction(fitted, x) - y for x, y in zip(vectors, targets, strict=True)]
    for j, coefficient in enumerate(fitted):
        gradient = sum(x[j] * residual for x, residual in zip(vectors, residuals, strict=True))
        assert gradient >= -1e-6
        if coefficient > 1e-8:
            assert abs(gradient) < 1e-6


def test_nnls_handles_rank_deficiency_and_zero_columns():
    fitted = nonnegative_least_squares([(1, 1, 0), (2, 2, 0)], [3, 6])
    assert all(v >= 0 for v in fitted)
    assert point_prediction(fitted, (1, 1, 0)) == pytest.approx(3)
    assert fitted[2] == 0


@pytest.mark.parametrize(
    ("groups", "alpha", "rank"),
    [
        (19, 0.05, 19),
        (24, 0.05, 24),
        (99, 0.01, 99),
        (999, 0.001, 999),
        (18, 0.05, 19),
        (98, 0.01, 99),
    ],
)
def test_exact_finite_sample_rank(groups, alpha, rank):
    assert conformal_rank(groups, alpha) == rank
    result = conformal_quantile(tuple(float(i) for i in range(groups)), alpha)
    assert result == (float(rank - 1) if rank <= groups else None)


def test_joint_score_covers_either_resource_and_rounds_only_upward():
    assert joint_score(110, 50, 100, 100) == pytest.approx(0.1)
    assert joint_score(90, 120, 100, 100) == pytest.approx(0.2)
    assert joint_score(90, 50, 100, 100) == 0
    assert formula_limit(100, 0.10, 1) == 110
    assert formula_limit(101, 0.10, 100) == 200
    assert formula_limit(0, 0.2, 1024) == 1024


@pytest.mark.parametrize("scores", [(), (float("nan"),), (-1.0,), (float("inf"),)])
def test_invalid_calibration_scores_fail_closed(scores):
    with pytest.raises(ValueError):
        conformal_quantile(scores, 0.05)
