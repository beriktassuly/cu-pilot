import json

import pytest

from cu_pilot.shadow import ObservationStore, RecordConflict


def test_atomic_checkpoint_dedup_and_conflict(tmp_path):
    path = tmp_path / "observations.sqlite"
    with ObservationStore(path) as store:
        assert store.ingest("a", {"wire": "one"})
        assert not store.ingest("a", {"wire": "one"})
        with pytest.raises(RecordConflict):
            store.ingest("a", {"wire": "two"})
        assert store.checkpoint("input") == 0
        store.finish("a", {"status": "unresolved"}, stream="input", cursor=1)
    with ObservationStore(path) as store:
        assert store.checkpoint("input") == 1
        assert store.get("a")["result"]["status"] == "unresolved"
        assert len(list(store.export_records())) == 1
        assert store.conflict_count() == 1
        out = tmp_path / "export.jsonl"
        store.export_jsonl(out)
        assert json.loads(out.read_text())["observation_id"] == "a"


def test_pending_is_resumable_and_schema_checked(tmp_path):
    path = tmp_path / "observations.sqlite"
    with ObservationStore(path) as store:
        store.ingest("a", {"wire": "one"})
        store.freeze_plan("a", {"prediction": "before-label"})
    with ObservationStore(path) as store:
        assert store.get("a")["phase"] == "planned"
        assert store.get("a")["outcome"] == "missing"
        with pytest.raises(RecordConflict):
            store.freeze_plan("a", {"prediction": "after-label"})
