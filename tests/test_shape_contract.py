import json
from pathlib import Path

import pytest

from cu_pilot.features import extract_features
from cu_pilot.schemas import TransactionInput

FIXTURES = Path(__file__).parent / "fixtures"
CASES = json.loads((FIXTURES / "shape_contract.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_structural_mutations(case):
    raw = json.loads((FIXTURES / "kit" / f"{case['base']}.json").read_text())["transaction"]
    for mutation in case["mutations"]:
        target = raw
        for key in mutation["path"][:-1]:
            target = target[key]
        target[mutation["path"][-1]] = mutation["value"]
    if raw["transaction_config"] and isinstance(raw["transaction_config"]["priorityFee"], str):
        raw["transaction_config"]["priorityFee"] = int(raw["transaction_config"]["priorityFee"])
    features = extract_features(TransactionInput.model_validate(raw))
    assert features.pattern_id == case["pattern_id"]
    assert list(features.risk_flags) == case["risk_flags"]
