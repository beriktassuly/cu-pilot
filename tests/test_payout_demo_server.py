"""Real loopback HTTP tests; payout execution is a synthetic application fixture."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from examples.payouts import server as demo


@pytest.fixture
def service(tmp_path, monkeypatch):
    calls = []
    opened = []

    class FixtureApplication:
        def __init__(self, directory, *, compute_unit_cap):
            opened.append((directory, compute_unit_cap))
            self.compute_unit_cap = compute_unit_cap
            self.loaded_data_cap = 1_048_576
            self.info = {"program": "offline-fixture"}
            self.bundle = None
            self.store = SimpleNamespace(db=SimpleNamespace(execute=lambda _sql: []))

        def setting(self, _key):
            return None

        def step(self, queue, *, method):
            calls.append((queue, method))
            return {
                "queue": queue,
                "method": method,
                "chosen_count": 8,
                "limit_source": "fresh_simulation",
                "estimation_simulations": 1,
                "control_simulations": 0,
                "rpc_calls": 7,
                "verification": {"queue": {"status": 1}},
            }

        def close(self):
            pass

    monkeypatch.setattr(demo, "Application", FixtureApplication)
    server = demo.create_server(tmp_path, 0, compute_unit_cap=1_400_000)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            state = client.get("/api/state").json()
            client.headers["X-Payout-Token"] = state["csrf_token"]
            yield SimpleNamespace(client=client, calls=calls, opened=opened, state=state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("route", ["/api/step", "/api/start"])
@pytest.mark.parametrize("method", demo.DEMO_METHODS)
def test_selected_strategy_reaches_execution_with_explicit_ceiling(service, route, method):
    response = service.client.post(route, json={"queue": "approved-local-queue", "method": method})
    assert response.status_code == 200
    deadline = time.monotonic() + 3
    while not service.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.calls == [("approved-local-queue", method)]
    assert all(cap == 1_400_000 for _directory, cap in service.opened)
    state = service.client.get("/api/state").json()
    assert state["worker"]["method"] == method
    assert state["last"]["method"] == method
    assert state["last"]["limit_source"] == "fresh_simulation"
    assert state["last"]["estimation_simulations"] == 1
    assert state["last"]["rpc_calls"] == 7


@pytest.mark.parametrize("route", ["/api/step", "/api/start"])
@pytest.mark.parametrize("method", ["fixed_estimate_ablation", "unknown", None, [], True])
def test_unsupported_strategy_rejected_before_opening_application(service, route, method):
    opened = len(service.opened)
    response = service.client.post(route, json={"queue": "q", "method": method})
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported demo strategy"
    assert service.calls == []
    assert len(service.opened) == opened
    assert service.client.get("/api/state").json()["worker"]["running"] is False


def test_demo_default_is_simulation_and_exposes_equal_resource_policy(service):
    assert service.state["default_method"] == "always_simulate"
    assert service.state["methods"] == list(demo.DEMO_METHODS)
    assert service.state["resource_policy"] == {
        "compute_unit_cap": 1_400_000,
        "loaded_data_cap": 1_048_576,
    }
    response = service.client.post("/api/step", json={"queue": "q"})
    assert response.status_code == 200
    assert service.calls == [("q", "always_simulate")]


@pytest.mark.parametrize("cap", [0, -1, 1_400_001, True, 100_000.0, "1400000"])
def test_invalid_application_ceiling_fails_before_binding_socket(tmp_path, monkeypatch, cap):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("invalid configuration must not open a server or application")

    monkeypatch.setattr(demo, "ThreadingHTTPServer", unexpected)
    monkeypatch.setattr(demo, "Application", unexpected)
    with pytest.raises(ValueError, match="compute unit cap"):
        demo.create_server(tmp_path, 0, compute_unit_cap=cap)


def test_local_control_boundaries_are_preserved(service):
    for headers in (
        {"X-Payout-Token": "wrong"},
        {"Host": "example.com"},
        {"Origin": "https://example.com"},
    ):
        response = service.client.post(
            "/api/step", json={"queue": "q", "method": "adaptive"}, headers=headers
        )
        assert response.status_code == 403
    assert service.calls == []


def test_non_object_request_is_rejected(service):
    response = service.client.post("/api/start", json=["adaptive"])
    assert response.status_code == 400
    assert response.json()["error"] == "JSON object required"
    assert service.calls == []
