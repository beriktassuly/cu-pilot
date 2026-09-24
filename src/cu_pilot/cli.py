"""Local-first command line workflows. All network operations are explicit."""

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import typer

from cu_pilot.data import load_observations, read_jsonl, write_observations
from cu_pilot.estimator import PatternEstimator, Policy
from cu_pilot.evaluation import evaluate, markdown_report
from cu_pilot.features import extract_features
from cu_pilot.integration_cli import app as integration_app
from cu_pilot.lifecycle_cli import app as lifecycle_app
from cu_pilot.parsing import normalize_observation, parse_transaction
from cu_pilot.rpc import RpcClient, RpcError
from cu_pilot.schemas import Prediction, PredictRequest, TransactionInput

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Bound Solana resource estimates with explicit simulation fallback.",
)
app.add_typer(integration_app)
app.add_typer(lifecycle_app, name="profiles")


def _json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, allow_nan=False))


def _rpc() -> RpcClient:
    endpoint = os.environ.get("CU_PILOT_RPC_URL")
    if not endpoint:
        raise typer.BadParameter("Set CU_PILOT_RPC_URL for this explicit network operation")
    return RpcClient(endpoint)


@app.command()
def normalize(raw: Path, output: Path, context: Annotated[str, typer.Option()]) -> None:
    """Convert compiled getTransaction JSONL into observations."""
    rows = [normalize_observation(row, context=context) for row in read_jsonl(raw)]
    write_observations(output, rows)
    _json({"observations": len(rows), "output": str(output)})


@app.command()
def collect(signatures: Path, output: Path, context: Annotated[str, typer.Option()]) -> None:
    """Read at most 100 explicit public signatures; never submits transactions."""
    identifiers = list(dict.fromkeys(signatures.read_text(encoding="utf-8").split()))
    if not 1 <= len(identifiers) <= 100:
        raise typer.BadParameter("Supply between 1 and 100 distinct signatures")
    try:
        with _rpc() as rpc:
            rows = [
                normalize_observation(rpc.get_transaction(sig), context=context)
                for sig in identifiers
            ]
    except RpcError as exc:
        raise typer.BadParameter(str(exc)) from None
    write_observations(output, rows)
    _json({"observations": len(rows), "output": str(output)})


@app.command()
def train(dataset: Path, model: Path, quantile: float = 0.99) -> None:
    """Fit per-pattern quantiles and a separate later calibration window."""
    estimator = PatternEstimator.fit(load_observations(dataset), policy=Policy(quantile=quantile))
    model.parent.mkdir(parents=True, exist_ok=True)
    estimator.save(model)
    _json(
        {
            "model": str(model),
            "patterns": len(estimator.model.patterns),
            "context": estimator.model.context,
        }
    )


@app.command()
def predict(
    transaction: Path,
    model: Path,
    context: Annotated[str, typer.Option()],
    current_slot: Annotated[int, typer.Option(min=0)],
) -> None:
    """Recommend a CU limit or simulation for a normalized or RPC JSON transaction."""
    estimator = PatternEstimator.load(model)
    try:
        raw = json.loads(transaction.read_text(encoding="utf-8-sig"))
        tx = TransactionInput.model_validate(raw) if "accounts" in raw else parse_transaction(raw)
        prediction = estimator.predict(
            extract_features(tx), context=context, current_slot=current_slot
        )
    except (ValueError, TypeError, KeyError):
        prediction = Prediction(
            pattern_id=None,
            simulation_recommended=True,
            reason="invalid_transaction",
            explanation="Unable to extract features.",
        )
    _json(prediction.model_dump())


@app.command("evaluate")
def evaluate_command(
    dataset: Path,
    output: Path = Path("artifacts/evaluation.json"),
    simulation_ms: float = 100,
    test_fraction: float = 0.2,
) -> None:
    """Compare baselines on an untouched chronological test set."""
    report = evaluate(
        load_observations(dataset), simulation_ms=simulation_ms, test_fraction=test_fraction
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
    typer.echo(markdown_report(report))


@app.command()
def simulate(
    transaction_base64: Path,
    version: Annotated[str, typer.Option()] = "auto",
    margin: float = 0.1,
    replace_recent_blockhash: bool = True,
) -> None:
    """Simulate caller-prepared high-budget wire bytes, without signing or sending."""
    if version not in {"auto", "legacy", "0", "1"}:
        raise typer.BadParameter("Version must be auto, legacy, 0, or 1")
    parsed_version: Literal["legacy", 0, 1] | None = None
    if version == "legacy":
        parsed_version = "legacy"
    if version == "0":
        parsed_version = 0
    elif version == "1":
        parsed_version = 1
    try:
        with _rpc() as rpc:
            result = rpc.simulate(
                transaction_base64.read_text().strip(),
                version=parsed_version,
                margin=margin,
                replace_recent_blockhash=replace_recent_blockhash,
            )
    except (RpcError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None
    _json(result.model_dump())


@app.command()
def demo(output_dir: Path = Path("artifacts/demo")) -> None:
    """Exercise training/evaluation entirely offline on labeled synthetic data."""
    from cu_pilot.data import split_by_slot
    from cu_pilot.demo import demo_transaction, make_demo

    output_dir.mkdir(parents=True, exist_ok=True)
    rows = make_demo()
    write_observations(output_dir / "synthetic.jsonl", rows)
    development, _ = split_by_slot(rows, 0.8)
    PatternEstimator.fit(development).save(output_dir / "model.json")
    (output_dir / "transaction.json").write_text(
        demo_transaction().model_dump_json(indent=2), encoding="utf-8"
    )
    request = PredictRequest(
        transaction=demo_transaction(), context="synthetic-demo-v1", current_slot=1_002_400
    )
    (output_dir / "request.json").write_text(request.model_dump_json(indent=2), encoding="utf-8")
    report = evaluate(rows)
    (output_dir / "evaluation.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "evaluation.md").write_text(markdown_report(report), encoding="utf-8")
    typer.echo(markdown_report(report))


@app.command()
def serve(model: Path, port: Annotated[int, typer.Option(min=1, max=65535)] = 8000) -> None:
    """Start an inference-only API on localhost, with /docs and /health."""
    import uvicorn

    from cu_pilot.api import create_app

    uvicorn.run(create_app(model), host="127.0.0.1", port=port)


if __name__ == "__main__":
    app()
