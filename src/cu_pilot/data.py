"""Small, stream-readable JSONL datasets; no execution labels enter features."""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cu_pilot.schemas import Observation


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("expected an object")
            except ValueError as exc:
                raise ValueError(f"Invalid JSON object at line {line_number}") from exc
            yield value


def load_observations(path: Path) -> list[Observation]:
    return [Observation.model_validate(row) for row in read_jsonl(path)]


def write_observations(path: Path, rows: Iterable[Observation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(row.model_dump_json() + "\n")


def unique_observations(rows: list[Observation]) -> list[Observation]:
    seen: dict[str, Observation] = {}
    for row in rows:
        if row.record_id in seen and row != seen[row.record_id]:
            raise ValueError("Conflicting observations share a record_id")
        seen[row.record_id] = row
    return sorted(seen.values(), key=lambda row: (row.slot, row.record_id))


def split_by_slot(
    rows: list[Observation], fraction: float
) -> tuple[list[Observation], list[Observation]]:
    if not 0 < fraction < 1:
        raise ValueError("Split fraction must be between zero and one")
    ordered = unique_observations(rows)
    slots = sorted({row.slot for row in ordered})
    if len(slots) < 2:
        raise ValueError("Need at least two distinct slots for a chronological split")
    boundary = slots[min(len(slots) - 1, max(1, int(len(slots) * fraction)))]
    return [r for r in ordered if r.slot < boundary], [r for r in ordered if r.slot >= boundary]
