import json

from test_binding import builder, context
from typer.testing import CliRunner

from cu_pilot.binding import unsigned_wire
from cu_pilot.cli import app
from cu_pilot.shadow import ShadowRequest


def test_shadow_cli_replay_resume_and_export(tmp_path):
    request = ShadowRequest(
        observation_id="cli-observation",
        wire_base64=unsigned_wire(builder()),
        context=context(),
        evidence_origin="synthetic",
        replay_response={
            "context": {"slot": 10},
            "value": {"err": None, "unitsConsumed": 600, "loadedAccountsDataSize": 128},
        },
    )
    path = tmp_path / "requests.jsonl"
    path.write_text(request.model_dump_json() + "\n")
    db = tmp_path / "events.sqlite"
    runner = CliRunner()
    result = runner.invoke(app, ["shadow", str(path), str(db), "--replay"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["completed"] == 1
    result = runner.invoke(app, ["shadow", str(path), str(db), "--replay"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["deduplicated"] == 1
    exported = tmp_path / "observations.jsonl"
    result = runner.invoke(
        app,
        [
            "export-shadow",
            str(db),
            str(exported),
            "--training-source",
            "simulation",
            "--evidence-origin",
            "synthetic",
        ],
    )
    assert result.exit_code == 0, result.output
    row = json.loads(exported.read_text())
    assert row["source"] == "synthetic"
    assert row["label_source"] == "simulation"
    assert row["evidence_origin"] == "synthetic"
    result = runner.invoke(app, ["preparation-report", str(db)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["synthetic/shadow/offline-replay"]
    assert report["measured_count"] == 1
    assert report["p99_ms"] >= 0
    assert report["resource_estimation_calls"] == 1


def test_new_network_commands_require_explicit_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("CU_PILOT_RPC_URL", raising=False)
    runner = CliRunner()
    path = tmp_path / "transaction.base64"
    path.write_text(unsigned_wire(builder()))
    ctx = tmp_path / "context.json"
    ctx.write_text(context().model_dump_json())
    result = runner.invoke(app, ["estimate-resources", str(path), "--context", str(ctx)])
    assert result.exit_code != 0
    assert "CU_PILOT_RPC_URL" in result.output
