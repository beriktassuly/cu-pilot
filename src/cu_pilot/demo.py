"""Deterministic synthetic pipeline exercise, never evidence of real-world accuracy."""

import random

from cu_pilot.features import extract_features
from cu_pilot.schemas import Account, Instruction, Observation, ResourceLabel, TransactionInput

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
PAYER = "So11111111111111111111111111111111111111112"
BUDGET = "ComputeBudget111111111111111111111111111111"


def demo_transaction(family: int = 0) -> TransactionInput:
    programs = [SYSTEM, TOKEN, MEMO]
    program = programs[family % 3]
    prefix = [bytes.fromhex("02000000"), bytes([3]), b"demoix01"][family % 3]
    # This is a shape fixture; accounts/data are not a transaction to submit.
    return TransactionInput(
        version="legacy",
        signature_count=1,
        accounts=(
            Account(pubkey=PAYER, signer=True, writable=True),
            Account(pubkey=program, signer=False, writable=False),
            Account(pubkey=BUDGET, signer=False, writable=False),
        ),
        instructions=(
            Instruction(
                program_id=BUDGET,
                accounts=(),
                data_hex=(bytes([2]) + (1_400_000).to_bytes(4, "little")).hex(),
            ),
            Instruction(program_id=program, accounts=(0,), data_hex=(prefix + bytes(8)).hex()),
        ),
    )


def make_demo(count: int = 2400, seed: int = 7) -> list[Observation]:
    if count < 30:
        raise ValueError("Demo requires at least 30 rows")
    rng = random.Random(seed)
    features = [extract_features(demo_transaction(i)) for i in range(3)]
    rows = []
    for index in range(count):
        family = index % 3
        center = [450, 6_000, 30_000][family]
        # Drift after the fitting period: calibration must reject this family.
        if family == 2 and index >= int(count * 0.57):
            center *= 3
        units = int(center * (1 + rng.uniform(-0.02, 0.02)))
        rows.append(
            Observation(
                record_id=f"synthetic-{index:06d}",
                slot=1_000_000 + index,
                context="synthetic-demo-v1",
                source="synthetic",
                features=features[family],
                label=ResourceLabel(compute_units=units, success=True),
            )
        )
    return rows
