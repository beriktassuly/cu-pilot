"""Offline interruption recovery contracts for frozen comparison preparation."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from examples.payouts import compare

SOURCE = {"sha256": "a" * 64, "files": {"module.py": "b" * 64}}


def write_plan(directory, config):
    directory.mkdir(parents=True)
    plan = {
        "schema_version": compare.SCHEMA,
        "config": config.model_dump(mode="json"),
        "source": SOURCE,
        "schedule": compare.schedule(config),
    }
    (directory / "plan.json").write_text(json.dumps(plan))
    return plan


@pytest.mark.parametrize("change", ["configuration", "schema", "source", "schedule"])
def test_resume_requires_original_configuration_source_and_schedule(tmp_path, monkeypatch, change):
    directory = tmp_path / "campaign"
    config = compare.CampaignConfig(repeats=2, seed=73)
    plan = write_plan(directory, config)
    monkeypatch.setattr(compare, "source_fingerprint", lambda: SOURCE)
    assert compare.resume_plan(directory, config) == plan
    expected = "configuration changed"
    if change == "configuration":
        config = config.model_copy(update={"alpha": 0.06})
    elif change == "schema":
        plan["schema_version"] = "unknown-protocol"
    elif change == "source":
        expected = "source changed"
        monkeypatch.setattr(
            compare,
            "source_fingerprint",
            lambda: {"sha256": SOURCE["sha256"], "files": {"module.py": "c" * 64}},
        )
    else:
        expected = "schedule changed"
        plan["schedule"][0], plan["schedule"][1] = plan["schedule"][1], plan["schedule"][0]
    (directory / "plan.json").write_text(json.dumps(plan))
    with pytest.raises(ValueError, match=expected):
        compare.resume_plan(directory, config)


class OwnedProcess:
    pid = 10001

    def __init__(self, *, slow_shutdown=False):
        self.actions = []
        self.slow_shutdown = slow_shutdown

    def poll(self):
        return None

    def terminate(self):
        self.actions.append("terminate")

    def kill(self):
        self.actions.append("kill")

    def wait(self, timeout):
        self.actions.append(("wait", timeout))
        if self.slow_shutdown and timeout == 15:
            raise subprocess.TimeoutExpired("fixture-runtime", timeout)
        return 0


def mocked_startup(monkeypatch, process):
    starts = []
    monkeypatch.setattr(compare, "source_fingerprint", lambda: SOURCE)
    monkeypatch.setattr(compare.shutil, "which", lambda _name: "/fixture/node")
    monkeypatch.setattr(compare.subprocess, "check_output", lambda *_args, **_kwargs: "v24.14.1\n")

    def start(*args, **kwargs):
        starts.append((args, kwargs))
        return process

    monkeypatch.setattr(compare.subprocess, "Popen", start)
    return starts


def test_collect_failure_preserves_ready_bank_and_resume_appends_attempt(tmp_path, monkeypatch):
    directory = tmp_path / "campaign"
    process = OwnedProcess()
    starts = mocked_startup(monkeypatch, process)
    instances = []

    def ready(path, owned):
        assert owned is process
        path.write_text(json.dumps({"pid": process.pid, "instance_id": "original-bank"}))

    class InterruptedCollection:
        def __init__(self, path, *, compute_unit_cap):
            self.path = path
            self.info = {"instance_id": "original-bank"}
            self.closed = False
            self.cap = compute_unit_cap
            instances.append(self)

        def collect(self, groups):
            assert groups == 80
            evidence = self.path / "durable-marker.json"
            if evidence.exists():
                assert json.loads(evidence.read_text()) == {"completed_rows": 44}
                raise KeyboardInterrupt()
            evidence.write_text(json.dumps({"completed_rows": 44}))
            raise RuntimeError("bounded collection interruption")

        def close(self):
            self.closed = True

    monkeypatch.setattr(compare, "wait_runtime", ready)
    monkeypatch.setattr(compare, "Application", InterruptedCollection)
    config = compare.CampaignConfig(repeats=1)
    with pytest.raises(RuntimeError, match="bounded collection interruption"):
        compare.prepare(directory, config)
    first = json.loads((directory / "preparation.json").read_text())
    assert first["status"] == "incomplete"
    assert len(first["attempts"]) == 1
    assert first["attempts"][0]["status"] == "error"
    assert first["attempts"][0]["error_type"] == "RuntimeError"
    assert instances[0].closed
    assert process.actions == []
    assert not (directory / "manifest.json").exists()

    with pytest.raises(KeyboardInterrupt):
        compare.prepare(directory, config, resume=True)
    resumed = json.loads((directory / "preparation.json").read_text())
    assert resumed["status"] == "incomplete"
    assert [a["status"] for a in resumed["attempts"]] == ["error", "interrupted"]
    assert resumed["attempts"][1]["error_type"] == "KeyboardInterrupt"
    assert all(a["elapsed_ms"] >= 0 for a in resumed["attempts"])
    assert len(starts) == 1
    assert all(app.closed for app in instances)
    assert process.actions == []


@pytest.mark.parametrize("slow_shutdown", [False, True])
def test_failed_startup_stops_only_owned_child(tmp_path, monkeypatch, slow_shutdown):
    process = OwnedProcess(slow_shutdown=slow_shutdown)
    starts = mocked_startup(monkeypatch, process)

    def fail_readiness(_path, owned):
        assert owned is process
        raise RuntimeError("runtime readiness failed")

    monkeypatch.setattr(compare, "wait_runtime", fail_readiness)
    monkeypatch.setattr(
        compare,
        "Application",
        lambda *_args, **_kwargs: pytest.fail("collection must not start before readiness"),
    )
    directory = tmp_path / "campaign"
    with pytest.raises(RuntimeError, match="runtime readiness failed"):
        compare.prepare(directory, compare.CampaignConfig())
    assert len(starts) == 1
    assert starts[0][1]["start_new_session"] is True
    assert process.actions == (
        ["terminate", ("wait", 15), "kill", ("wait", 5)]
        if slow_shutdown
        else ["terminate", ("wait", 15)]
    )
    saved = json.loads((directory / "preparation.json").read_text())
    assert saved["status"] == "incomplete"
    assert saved["attempts"][0]["error_type"] == "RuntimeError"


@pytest.mark.parametrize("same_bank", [False, True])
def test_completed_preparation_resume_verifies_original_bank_and_closes_client(
    tmp_path, monkeypatch, same_bank
):
    directory = tmp_path / "campaign"
    config = compare.CampaignConfig(seed=81)
    write_plan(directory, config)
    (directory / "manifest.json").write_text("{}")
    manifest = {"bank": {"instance_id": "original-bank"}}
    monkeypatch.setattr(compare, "source_fingerprint", lambda: SOURCE)
    checks, closed = [], []

    def verify(path):
        checks.append(path)
        return manifest

    class BankConnection:
        def __init__(self, path):
            assert path == directory / "private/runtime.json"
            self.client = SimpleNamespace(close=lambda: closed.append(True))

        def call(self, action):
            assert action == "info"
            return {"instance_id": "original-bank" if same_bank else "restarted-bank"}

    monkeypatch.setattr(compare, "verify_frozen", verify)
    monkeypatch.setattr(compare, "Bridge", BankConnection)
    monkeypatch.setattr(
        compare,
        "Application",
        lambda *_args, **_kwargs: pytest.fail("completed preparation must not collect again"),
    )
    monkeypatch.setattr(
        compare.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("resumption must not launch a replacement bank"),
    )
    if same_bank:
        assert compare.prepare(directory, config, resume=True) is manifest
    else:
        with pytest.raises(ValueError, match="bank restarted"):
            compare.prepare(directory, config, resume=True)
    assert checks == [directory]
    assert closed == [True]


@pytest.mark.parametrize("change", ["runtime_config", "live_response", "missing_frozen_identity"])
def test_incomplete_resume_rejects_restarted_bank_and_preserves_original_identity(
    tmp_path, monkeypatch, change
):
    directory = tmp_path / "campaign"
    config = compare.CampaignConfig(seed=82)
    write_plan(directory, config)
    private = directory / "private"
    private.mkdir()
    runtime_id = "replacement-bank" if change == "runtime_config" else "original-bank"
    (private / "runtime.json").write_text(json.dumps({"instance_id": runtime_id, "pid": 10001}))
    saved_bank = None if change == "missing_frozen_identity" else "original-bank"
    checkpoint = {
        "status": "incomplete",
        "bank_instance_id": saved_bank,
        "attempts": [{"status": "error", "error_type": "RuntimeError", "elapsed_ms": 2}],
    }
    (directory / "preparation.json").write_text(json.dumps(checkpoint))
    monkeypatch.setattr(compare, "source_fingerprint", lambda: SOURCE)
    applications = []

    class ChangedLiveBank:
        def __init__(self, _path, **_kwargs):
            assert change == "live_response"
            self.info = {"instance_id": "replacement-bank"}
            self.closed = False
            applications.append(self)

        def collect(self, _groups):
            pytest.fail("bank mismatch must be rejected before collection")

        def close(self):
            self.closed = True

    monkeypatch.setattr(compare, "Application", ChangedLiveBank)
    monkeypatch.setattr(
        compare.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail(
            "incomplete resumption must not launch a replacement bank"
        ),
    )
    expected = "runtime identity changed" if change == "live_response" else "bank identity changed"
    with pytest.raises(ValueError, match=expected):
        compare.prepare(directory, config, resume=True)
    saved = json.loads((directory / "preparation.json").read_text())
    assert saved["status"] == "incomplete"
    assert saved["bank_instance_id"] == saved_bank
    assert saved["attempts"][0] == checkpoint["attempts"][0]
    assert len(saved["attempts"]) == 2
    assert saved["attempts"][-1]["error_type"] == "ValueError"
    assert all(app.closed for app in applications)
    assert len(applications) == (1 if change == "live_response" else 0)


def test_resume_cli_uses_frozen_nondefault_configuration(tmp_path, monkeypatch):
    directory = tmp_path / "campaign"
    config = compare.CampaignConfig(
        groups=160, repeats=31, seed=821, alpha=0.03, compute_unit_cap=900_000
    )
    write_plan(directory, config)
    captured = []
    monkeypatch.setattr(
        compare.sys,
        "argv",
        ["compare", "prepare", "--directory", str(directory), "--resume-preparation"],
    )
    monkeypatch.setattr(
        compare, "prepare", lambda path, actual, **kwargs: captured.append((path, actual, kwargs))
    )
    compare.main()
    assert captured == [(directory.resolve(), config, {"resume": True})]


def test_resume_cli_rejects_explicit_configuration_override(tmp_path, monkeypatch):
    directory = tmp_path / "campaign"
    write_plan(directory, compare.CampaignConfig())
    monkeypatch.setattr(
        compare.sys,
        "argv",
        [
            "compare",
            "prepare",
            "--directory",
            str(directory),
            "--resume-preparation",
            "--alpha=0.02",
        ],
    )
    monkeypatch.setattr(
        compare,
        "prepare",
        lambda *_args, **_kwargs: pytest.fail("an override must not reach preparation"),
    )
    with pytest.raises(SystemExit) as exc:
        compare.main()
    assert exc.value.code == 2
