import base64
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


def signed_retry_fixture(store, *, commitment="finalized", attach_retry=True):
    """Real SDK signatures for two controlled blockhash variants; RPC labels are fixtures."""
    import httpx
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.transaction import VersionedTransaction
    from test_binding import builder, context, rpc_response

    from cu_pilot.binding import decode_wire, unsigned_wire
    from cu_pilot.rpc import RpcClient
    from cu_pilot.shadow import ShadowRequest, collect_shadow

    payer = Keypair()
    request = ShadowRequest(
        observation_id="retried",
        wire_base64=unsigned_wire(builder(payer=payer.pubkey())),
        context=context(),
        evidence_origin="synthetic",
    )
    with RpcClient(
        "http://unused.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rpc_response())),
    ) as rpc:
        collect_shadow([request], store=store, rpc=rpc)
    final = decode_wire(store.get("retried")["result"]["unsigned_transaction_base64"]).message
    h = final.header
    refreshed = Message.new_with_compiled_instructions(
        h.num_required_signatures,
        h.num_readonly_signed_accounts,
        h.num_readonly_unsigned_accounts,
        final.account_keys,
        Hash.from_bytes(bytes([7]) * 32),
        final.instructions,
    )
    wires = [
        base64.b64encode(bytes(VersionedTransaction(message, [payer]))).decode()
        for message in (final, refreshed)
    ]
    signatures = [str(decode_wire(wire).signatures[0]) for wire in wires]
    for wire in wires if attach_retry else wires[:1]:
        store.attach_signature("retried", wire, commitment=commitment, allow_blockhash_refresh=True)
    assert signatures[0] != signatures[1]
    outcomes = [
        {
            "slot": 11 + index,
            "transaction": [wire, "base64"],
            "meta": {"err": None, "computeUnitsConsumed": 650, "costUnits": 999999},
        }
        for index, wire in enumerate(wires)
    ]
    return signatures, outcomes


@pytest.mark.parametrize("finalized_first", [True, False])
def test_retry_aggregate_retains_finalized_evidence_in_any_arrival_order(tmp_path, finalized_first):
    path = tmp_path / "retry.sqlite"
    with ObservationStore(path) as store:
        signatures, outcomes = signed_retry_fixture(store)
        events = [(signatures[0], outcomes[0]), (signatures[1], None)]
        if not finalized_first:
            events.reverse()
        for signature, outcome in events:
            store.reconcile(signature, outcome, commitment="finalized")
        assert store.get("retried")["outcome"] == "finalized"
        assert store.reconcile(signatures[1], None, commitment="finalized") == "finalized"
        record = next(store.export_records())
        assert record["outcome"] == "finalized"
        assert record["execution_signature_count"] == 2
        assert len(record["execution_outcomes"]) == 2
        rows = list(store.training_observations(source="historical", evidence_origin="synthetic"))
        assert len(rows) == 1
        assert rows[0].label.success is True
        assert rows[0].label.compute_units == 650
        # Existing databases may have persisted the former last-attempt summary.
        # Reads must correct that cache without changing stored outcome revisions.
        with store.db:
            store.db.execute(
                "UPDATE observations SET outcome='unavailable' WHERE id=?", ("retried",)
            )
    with ObservationStore(path) as reopened:
        assert reopened.get("retried")["outcome"] == "finalized"


def test_attaching_another_signed_retry_preserves_existing_finality(tmp_path):
    with ObservationStore(tmp_path / "attach.sqlite") as store:
        signatures, outcomes = signed_retry_fixture(store, attach_retry=False)
        assert store.reconcile(signatures[0], outcomes[0], commitment="finalized") == "finalized"
        assert (
            store.attach_signature(
                "retried", outcomes[1]["transaction"][0], allow_blockhash_refresh=True
            )
            == signatures[1]
        )
        assert store.get("retried")["outcome"] == "finalized"
        record = next(store.export_records())
        assert record["execution_signature_count"] == 2
        assert len(record["execution_outcomes"]) == 1


def test_failed_finalized_attempt_retains_finality_while_retry_is_pending(tmp_path):
    with ObservationStore(tmp_path / "failed.sqlite") as store:
        signatures, outcomes = signed_retry_fixture(store)
        outcomes[0]["meta"]["err"] = {"InstructionError": [0, "Custom"]}
        assert store.reconcile(signatures[0], outcomes[0], commitment="finalized") == "finalized"
        # Finality describes evidence, not success or completion of every retry.
        assert store.reconcile(signatures[1], outcomes[1], commitment="confirmed") == "finalized"
        assert store.get("retried")["outcome"] == "finalized"
        rows = list(store.training_observations(source="historical", evidence_origin="synthetic"))
        assert len(rows) == 1
        assert rows[0].label.success is False
        assert store.reconcile(signatures[1], outcomes[1], commitment="finalized") == "finalized"
        rows = list(store.training_observations(source="historical", evidence_origin="synthetic"))
        assert len(rows) == 1
        assert rows[0].label.success is True


@pytest.mark.parametrize(
    "commitment,observed", [("finalized", "pending_finality"), ("confirmed", "confirmed")]
)
def test_retry_aggregate_uses_latest_revision_per_signature(tmp_path, commitment, observed):
    with ObservationStore(tmp_path / "pending.sqlite") as store:
        signatures, outcomes = signed_retry_fixture(store, commitment=commitment)
        assert store.get("retried")["outcome"] == "pending"
        # An unavailable retry cannot erase another attempt's pending state.
        assert store.reconcile(signatures[0], None, commitment=commitment) == "pending"
        assert store.reconcile(signatures[1], outcomes[1], commitment="confirmed") == observed
        assert store.reconcile(signatures[0], None, commitment=commitment) == observed
        # The same attempt may disappear before finality. Old revisions are audit
        # events, not current evidence; duplicate old results must not resurrect it.
        assert store.reconcile(signatures[1], None, commitment=commitment) == "unavailable"
        assert store.reconcile(signatures[1], outcomes[1], commitment="confirmed") == "unavailable"
        record = next(store.export_records())
        assert len(record["execution_outcomes"]) == 3
        assert not list(
            store.training_observations(source="historical", evidence_origin="synthetic")
        )


def test_training_exports_preserve_original_collection_provenance_only_when_recorded(tmp_path):
    with ObservationStore(tmp_path / "provenance.sqlite") as store:
        signatures, outcomes = signed_retry_fixture(store)
        store.reconcile(signatures[0], outcomes[0], commitment="finalized")
        for source in ("simulation", "historical"):
            row = next(store.training_observations(source=source, evidence_origin="synthetic"))
            assert row.collection_method == "prospective"
            assert row.collection_mode == "shadow"
        # An old/manual record without preparation telemetry carries no evidence
        # that its original execution was shadow collection or deployment.
        with store.db:
            store.db.execute("DELETE FROM preparation_traces WHERE observation_id=?", ("retried",))
        for source in ("simulation", "historical"):
            row = next(store.training_observations(source=source, evidence_origin="synthetic"))
            assert row.collection_method is None
            assert row.collection_mode is None
