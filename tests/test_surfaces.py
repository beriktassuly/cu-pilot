import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cu_pilot.api import create_app
from cu_pilot.cli import app
from cu_pilot.data import split_by_slot
from cu_pilot.demo import demo_transaction, make_demo
from cu_pilot.estimator import PatternEstimator


def test_api_valid_unknown_context_and_bad_input(tmp_path) -> None:
    rows, _ = split_by_slot(make_demo(), 0.8)
    model = tmp_path / "model.json"
    PatternEstimator.fit(rows).save(model)
    client = TestClient(create_app(model))
    assert client.get("/health").json()["model_loaded"] is True
    payload = {
        "transaction": demo_transaction().model_dump(mode="json"),
        "current_slot": 1_002_000,
        "context": "synthetic-demo-v1",
    }
    response = client.post("/predict", json=payload)
    assert response.status_code == 200
    assert response.json()["simulation_recommended"] is False
    payload["context"] = "different-deployment"
    assert client.post("/predict", json=payload).json()["simulation_recommended"] is True
    payload["transaction"]["instructions"][0]["accounts"] = [999]
    assert client.post("/predict", json=payload).json()["simulation_recommended"] is True
    assert client.post("/predict", json={}).status_code == 422


def test_api_without_model_is_not_ready() -> None:
    client = TestClient(create_app())
    payload = {
        "transaction": demo_transaction().model_dump(mode="json"),
        "current_slot": 1,
        "context": "demo",
    }
    assert client.post("/predict", json=payload).status_code == 503


def test_cli_offline_demo_and_prediction(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["demo", "--output-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "synthetic" in result.output
    result = runner.invoke(
        app,
        [
            "predict",
            str(tmp_path / "transaction.json"),
            str(tmp_path / "model.json"),
            "--context",
            "synthetic-demo-v1",
            "--current-slot",
            "1002000",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["simulation_recommended"] is False


@pytest.mark.parametrize(
    "field,value", [("version", False), ("signature_count", True), ("signature_count", "1")]
)
def test_api_rejects_coerced_wire_facts(field, value) -> None:
    client = TestClient(create_app())
    payload = {
        "transaction": demo_transaction().model_dump(mode="json"),
        "current_slot": 1,
        "context": "demo",
    }
    payload["transaction"][field] = value
    assert client.post("/predict", json=payload).status_code == 422
