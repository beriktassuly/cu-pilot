"""Build real SDK messages with explicitly synthetic replay labels, without keys/network.

uv run python examples/shadow_replay.py
uv run cu-pilot shadow artifacts/shadow/requests.jsonl artifacts/shadow/events.sqlite --replay
"""

import json
from pathlib import Path

from solders.hash import Hash
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from cu_pilot.binding import unsigned_wire
from cu_pilot.integration import EstimationContext
from cu_pilot.shadow import ShadowRequest


def main() -> None:
    output = Path("artifacts/shadow")
    output.mkdir(parents=True, exist_ok=True)
    payer = Pubkey.from_bytes(bytes([7]) * 32)
    recipient = Pubkey.from_bytes(bytes([8]) * 32)
    with (output / "requests.jsonl").open("w", encoding="utf-8") as stream:
        for index in range(500):
            message = Message.new_with_blockhash(
                [
                    transfer(
                        TransferParams(from_pubkey=payer, to_pubkey=recipient, lamports=100 + index)
                    ),
                    transfer(
                        TransferParams(from_pubkey=payer, to_pubkey=recipient, lamports=200 + index)
                    ),
                ],
                payer,
                Hash.default(),
            )
            request = ShadowRequest(
                observation_id=f"synthetic-{index}",
                wire_base64=unsigned_wire(message),
                context=EstimationContext(
                    context="synthetic-batch-v1",
                    current_slot=index + 100,
                    cluster_identity="synthetic",
                    runtime_identity="synthetic",
                    workload="batch-transfer",
                    budget_independent=True,
                ),
                evidence_origin="synthetic",
                replay_response={
                    "context": {"slot": index + 100},
                    "value": {
                        "err": None,
                        "unitsConsumed": 600 + index % 20,
                        "loadedAccountsDataSize": 256 + index % 30,
                    },
                },
            )
            stream.write(request.model_dump_json() + "\n")
    print(
        json.dumps(
            {"requests": 500, "evidence": "synthetic", "path": str(output / "requests.jsonl")}
        )
    )


if __name__ == "__main__":
    main()
