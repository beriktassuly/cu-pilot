"""Offline application policy tests with synthetic local execution responses."""

import json
from pathlib import Path

import pytest

from examples.payouts import app as application
from tests.test_payout_adaptive import PlannerFixture


@pytest.mark.parametrize("cap", [0, -1, 1_400_001, True, 100_000.5, "100000"])
def test_invalid_compute_ceiling_fails_before_runtime_or_filesystem_access(tmp_path, cap):
    directory = tmp_path / "unused"
    with pytest.raises(ValueError, match="compute unit cap"):
        application.Application(directory, compute_unit_cap=cap)
    assert not directory.exists()


def test_configured_ceiling_is_per_application_and_recorded(tmp_path: Path, monkeypatch):
    bounded = PlannerFixture(tmp_path / "bounded", monkeypatch, compute_unit_cap=50_000)
    ordinary = PlannerFixture(tmp_path / "ordinary", monkeypatch)
    try:
        bounded.simulation_limits[8] = (90_000, 20_000)
        first = bounded.app.step("bounded-queue", "adaptive", interrupt_after_sign=True)
        second = ordinary.app.step("ordinary-queue", "adaptive", interrupt_after_sign=True)
        first = json.loads(
            bounded.app.store.db.execute(
                "SELECT body FROM payout_steps WHERE id=?", (first["id"],)
            ).fetchone()[0]
        )
        second = json.loads(
            ordinary.app.store.db.execute(
                "SELECT body FROM payout_steps WHERE id=?", (second["id"],)
            ).fetchone()[0]
        )
        assert first["chosen_count"] == 4
        assert second["chosen_count"] == 8
        assert first["compute_unit_cap"] == 50_000
        assert second["compute_unit_cap"] == 100_000
        assert first["adaptive_planning"]["compute_unit_cap"] == 50_000
        assert first["decision"]["compute_unit_limit"] <= 50_000
        assert second["decision"]["compute_unit_limit"] <= 100_000
    finally:
        bounded.app.close()
        ordinary.app.close()
