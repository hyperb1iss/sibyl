"""Genuine empty rollback receipts fence stores whose first apply never committed."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from surrealdb import AsyncSurreal
from surrealdb.errors import SurrealError, parse_query_error

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.schema import render_surreal_compatible_sql
from sibyl_core.backends.surreal.schema_archive_phases import ARCHIVE_PHASE_DEFINITIONS
from sibyl_core.backends.surreal.schema_source_states import (
    SOURCE_STATE_DEFINITIONS,
    source_state_event,
)
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive_phase_receipts import ArchiveCreatedIdentity, ArchivePhaseKey
from sibyl_core.services.archive_phase_store import (
    prepare_archive_phase_transaction,
    prepare_archive_unused_store_close,
    read_archive_phase_receipt,
)
from tests.test_archive_phase_receipts import binding

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def isolated_store():
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace="archive_unused_store_close_author_" + uuid4().hex,
    )
    try:
        await client.execute_query("""
            DEFINE TABLE raw_captures SCHEMALESS;
            DEFINE INDEX raw_uuid ON raw_captures FIELDS uuid UNIQUE;
            DEFINE TABLE entity SCHEMALESS;
            DEFINE INDEX entity_uuid ON entity FIELDS uuid UNIQUE;
            DEFINE TABLE memory_derivations SCHEMALESS;
            DEFINE TABLE relates_to SCHEMALESS TYPE RELATION IN entity OUT entity ENFORCED;
        """)
        await client.execute_query(SOURCE_STATE_DEFINITIONS)
        await client.execute_query("DEFINE FIELD incarnation ON source_states TYPE option<string>;")
        await client.execute_query(source_state_event(SourceKind.RAW_CAPTURE, integrity=True))
        await client.execute_query(source_state_event(SourceKind.GRAPH_ENTITY, integrity=True))
        await client.execute_query(
            render_surreal_compatible_sql(ARCHIVE_PHASE_DEFINITIONS, url=client._url)
        )
        yield client
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {client.namespace};")
        await client.close()


def keys(model, store):
    return (
        ArchivePhaseKey(binding=model, store=store, action="apply", batch_sequence=0),
        ArchivePhaseKey(binding=model, store=store, action="rollback", batch_sequence=0),
    )


def first_apply(client, key, token):
    identity = str(uuid4())
    kind = "raw_capture" if key.store == "content" else "graph_entity"
    table = "raw_captures" if key.store == "content" else "entity"
    record = {
        "uuid": identity,
        "revision": 1,
        "metadata": {},
        "memory_scope": "private",
        "principal_id": key.binding.actor_id,
        "scope_key": key.binding.actor_id,
    }
    if key.store == "content":
        record.update(organization_id=key.binding.organization_id, raw_content="Captured")
    else:
        record.update(group_id=key.binding.organization_id, entity_type="topic", name="Captured")
    return prepare_archive_phase_transaction(
        key=key,
        url=client._url,
        expected_revision=0,
        expected_token=token,
        writer_statements=f"CREATE {table} CONTENT $record; "
        "LET $sibyl_archive_phase_outcomes = [{kind:$kind, disposition:'created', "
        "destination_id:$record.uuid}];",
        writer_parameters={"record": record, "kind": kind},
        planned_creates=(ArchiveCreatedIdentity(kind=kind, destination_id=identity),),
    )


@pytest.mark.parametrize("store", ["content", "graph"])
async def test_unused_store_close_is_terminal_replayable_and_blocks_delayed_apply(store):
    model, token, closed = binding(), str(uuid4()), str(uuid4())
    apply_key, close_key = keys(model, store)
    async with isolated_store() as client:
        delayed = first_apply(client, apply_key, token)
        tx = prepare_archive_unused_store_close(
            key=close_key, url=client._url, expected_token=token, rollback_token=closed
        )
        await client.execute_query(tx.query, **tx.parameters)
        first = await read_archive_phase_receipt(client.execute_query, key=close_key, token=closed)
        assert first is not None and first.terminal
        assert first.previous_revision == 0 and first.committed_revision == 1
        assert first.previous_token == token and first.token == closed
        assert first.counts == first.introduced == first.retired == ()
        assert (
            await read_archive_phase_receipt(client.execute_query, key=close_key, token=closed)
            == first
        )
        with pytest.raises(SurrealError, match="admission changed"):
            await client.execute_query(delayed.query, **delayed.parameters)
        with pytest.raises(SurrealError, match="token is closed"):
            await read_archive_phase_receipt(client.execute_query, key=apply_key, token=token)
        for table in ("entity", "raw_captures", "source_states"):
            assert await client.execute_query(f"SELECT * FROM {table};") == []
        control = (await client.execute_query("SELECT * FROM archive_phase_controls;"))[0]
        assert control["revision"] == 1 and control["state"] == "rolled_back"
        assert len(await client.execute_query("SELECT * FROM archive_phase_receipts;")) == 1


async def test_unused_store_close_preserves_committed_other_store_after_interruption():
    model, token, closed = binding(), str(uuid4()), str(uuid4())
    content_apply, _ = keys(model, "content")
    _, graph_close = keys(model, "graph")
    async with isolated_store() as content, isolated_store() as graph:
        tx = first_apply(content, content_apply, token)
        await content.execute_query(tx.query, **tx.parameters)
        before = await read_archive_phase_receipt(
            content.execute_query, key=content_apply, token=token
        )
        closure = prepare_archive_unused_store_close(
            key=graph_close, url=graph._url, expected_token=token, rollback_token=closed
        )
        await graph.execute_query(closure.query, **closure.parameters)
        assert (
            await read_archive_phase_receipt(content.execute_query, key=content_apply, token=token)
            == before
        )
        assert len(await content.execute_query("SELECT * FROM raw_captures;")) == 1
        assert await graph.execute_query("SELECT * FROM entity;") == []


@pytest.mark.parametrize("store", ["content", "graph"])
async def test_unused_store_close_rejects_committed_apply_without_changing_evidence(store):
    model, token = binding(), str(uuid4())
    apply_key, close_key = keys(model, store)
    async with isolated_store() as client:
        tx = first_apply(client, apply_key, token)
        await client.execute_query(tx.query, **tx.parameters)
        before = await client.execute_query("SELECT * FROM archive_phase_receipts;")
        closure = prepare_archive_unused_store_close(
            key=close_key, url=client._url, expected_token=token, rollback_token=str(uuid4())
        )
        with pytest.raises(SurrealError, match="already has committed phase evidence"):
            await client.execute_query(closure.query, **closure.parameters)
        assert await client.execute_query("SELECT * FROM archive_phase_receipts;") == before
        assert (await client.execute_query("SELECT * FROM archive_phase_controls;"))[0][
            "state"
        ] == "open"


@pytest.mark.parametrize("bad", ["apply", "later-batch", "unchanged-token"])
async def test_unused_store_close_rejects_nonclosure_shapes(bad):
    model, token = binding(), str(uuid4())
    key = ArchivePhaseKey(
        binding=model,
        store="graph",
        action="apply" if bad == "apply" else "rollback",
        batch_sequence=1 if bad == "later-batch" else 0,
    )
    expected = "only rollback" if bad == "apply" else "fixed empty terminal phase"
    with pytest.raises(ValueError, match=expected):
        prepare_archive_unused_store_close(
            key=key,
            url="memory://",
            expected_token=token,
            rollback_token=token if bad == "unchanged-token" else str(uuid4()),
        )


async def test_unused_store_close_does_not_relax_ordinary_rollback_admission():
    _, key = keys(binding(), "graph")
    async with isolated_store() as client:
        tx = prepare_archive_phase_transaction(
            key=key,
            url=client._url,
            expected_revision=0,
            expected_token=str(uuid4()),
            rollback_token=str(uuid4()),
            terminal=True,
            writer_statements="LET $sibyl_archive_phase_outcomes = [];",
            writer_parameters={},
        )
        with pytest.raises(SurrealError, match="control is missing"):
            await client.execute_query(tx.query, **tx.parameters)
        assert await client.execute_query("SELECT * FROM archive_phase_controls;") == []
        assert await client.execute_query("SELECT * FROM archive_phase_receipts;") == []


async def raw_query(db, query, parameters, transaction):
    response = await db.query_raw(query, parameters, txn_id=transaction)
    db.check_response_for_error(response, "unused-store closure race")
    for entry in response["result"]:
        if entry["status"] != "OK":
            raise parse_query_error(entry)
    return response["result"][-1]["result"]


@pytest.mark.parametrize("store", ["content", "graph"])
@pytest.mark.parametrize("winner", ["close", "apply"])
async def test_unused_store_close_native_actual_first_apply_race(store, winner):
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://")
    if not url.startswith(("ws://", "wss://", "http://", "https://")):
        pytest.skip("actual write conflicts require independent native server sockets")
    model, token, closed = binding(), str(uuid4()), str(uuid4())
    apply_key, close_key = keys(model, store)
    async with isolated_store() as client:
        sockets = [AsyncSurreal(url), AsyncSurreal(url)]
        transactions = []
        settled = set()
        try:
            for db in sockets:
                await db.connect()
                await db.signin(
                    {
                        "username": os.environ["SIBYL_ARCHIVE_TEST_SURREAL_USERNAME"],
                        "password": os.environ["SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD"],
                    }
                )
                await db.use(client.namespace, "content")
                transactions.append(await db.begin())
            cuts = await asyncio.gather(
                *(
                    raw_query(db, "RETURN (SELECT * FROM archive_phase_controls);", {}, transaction)
                    for db, transaction in zip(sockets, transactions, strict=True)
                )
            )
            assert cuts == [[], []]
            apply = first_apply(client, apply_key, token)
            close = prepare_archive_unused_store_close(
                key=close_key, url=client._url, expected_token=token, rollback_token=closed
            )
            for db, transaction, tx in zip(sockets, transactions, (close, apply), strict=True):
                body = tx.query.removeprefix("BEGIN TRANSACTION;").removesuffix(
                    "COMMIT TRANSACTION;\n"
                )
                await raw_query(db, body, tx.parameters, transaction)
            order = (0, 1) if winner == "close" else (1, 0)
            await sockets[order[0]].commit(transactions[order[0]])
            settled.add(order[0])
            with pytest.raises(SurrealError):
                await sockets[order[1]].commit(transactions[order[1]])
            settled.add(order[1])
            receipts = await client.execute_query("SELECT * FROM archive_phase_receipts;")
            assert len(receipts) == 1 and receipts[0]["action"] == (
                "rollback" if winner == "close" else "apply"
            )
            controls = await client.execute_query("SELECT * FROM archive_phase_controls;")
            assert len(controls) == 1 and controls[0]["state"] == (
                "rolled_back" if winner == "close" else "open"
            )
            table = "raw_captures" if store == "content" else "entity"
            assert len(await client.execute_query(f"SELECT * FROM {table};")) == (
                0 if winner == "close" else 1
            )
        finally:
            for index, (db, transaction) in enumerate(zip(sockets, transactions, strict=False)):
                if index not in settled:
                    await db.cancel(transaction)
            for db in sockets:
                await db.close()
