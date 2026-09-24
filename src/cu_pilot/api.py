"""Local inference API; raw transaction inputs prevent client-supplied pattern IDs."""

from pathlib import Path

from fastapi import FastAPI, HTTPException

from cu_pilot import __version__
from cu_pilot.estimator import PatternEstimator
from cu_pilot.features import extract_features
from cu_pilot.schemas import Prediction, PredictRequest


def create_app(model_path: Path | None = None) -> FastAPI:
    estimator = PatternEstimator.load(model_path) if model_path else None
    application = FastAPI(title="CU Pilot", version=__version__)

    @application.get("/health")
    def health() -> dict[str, str | bool]:
        return {"status": "ok", "model_loaded": estimator is not None, "version": __version__}

    @application.post("/predict", response_model=Prediction)
    def predict(request: PredictRequest) -> Prediction:
        if estimator is None:
            raise HTTPException(status_code=503, detail="Load a model before predicting")
        try:
            features = extract_features(request.transaction)
        except ValueError:
            return Prediction(
                pattern_id=None,
                simulation_recommended=True,
                reason="invalid_transaction",
                explanation="Transaction fields are incomplete or unsupported.",
            )
        return estimator.predict(
            features, context=request.context, current_slot=request.current_slot
        )

    return application
