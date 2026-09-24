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


def test_interrupted_collection_reuses_prelabel_plan(tmp_path, monkeypatch):
    import httpx
    from test_binding import builder, context, rpc_response

    from cu_pilot.binding import unsigned_wire
    from cu_pilot.rpc import RpcClient
    from cu_pilot.shadow import ShadowRequest, collect_shadow

    path = tmp_path / "interrupted.sqlite"
    request = ShadowRequest(
        observation_id="interrupt",
        wire_base64=unsigned_wire(builder()),
        context=context(),
        evidence_origin="synthetic",
    )

    def interrupt(_request):
        raise KeyboardInterrupt()

    with (
        ObservationStore(path) as store,
        RpcClient("http://unused.invalid", transport=httpx.MockTransport(interrupt)) as rpc,
    ):
        with pytest.raises(KeyboardInterrupt):
            collect_shadow([request], store=store, rpc=rpc)
        assert store.get("interrupt")["phase"] == "planned"
        assert store.checkpoint("default") == 0
        frozen = store.get("interrupt")["plan"]

    def forbid_replanning(*args, **kwargs):
        raise AssertionError("resumption must reuse the original pre-label prediction")

    monkeypatch.setattr("cu_pilot.shadow.prepare_decision", forbid_replanning)
    with (
        ObservationStore(path) as store,
        RpcClient(
            "http://unused.invalid",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rpc_response())),
        ) as rpc,
    ):
        assert collect_shadow([request], store=store, rpc=rpc)["completed"] == 1
        assert store.get("interrupt")["plan"] == frozen
        assert collect_shadow([request], store=store, rpc=rpc, stream="second")["checkpoint"] == 1
        assert rpc.call_count == 1


def test_identifiers_are_parameterized(tmp_path):
    identifier = "x'); DROP TABLE observations;--"
    with ObservationStore(tmp_path / "events.sqlite") as store:
        store.ingest(identifier, {"input": 1})
        store.finish(identifier, {"status": "unresolved"}, stream=identifier, cursor=1)
        assert store.get(identifier)["result"]["status"] == "unresolved"
        assert store.checkpoint(identifier) == 1
