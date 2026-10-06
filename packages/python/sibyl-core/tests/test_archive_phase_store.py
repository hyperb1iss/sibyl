"""Synthetic native data/receipt composition, without an archive apply writer."""

import asyncio
import os
from uuid import uuid4

import pytest
from surrealdb.errors import SurrealError

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    _content_schema_migrations,
)
from sibyl_core.backends.surreal.schema import (
    GRAPH_SCHEMA_MIGRATIONS,
    _graph_schema_migrations,
    render_surreal_compatible_sql,
)
from sibyl_core.backends.surreal.schema_archive_phases import ARCHIVE_PHASE_DEFINITIONS
from sibyl_core.backends.surreal.schema_source_states import (
    SOURCE_STATE_DEFINITIONS,
    source_state_event,
)
from sibyl_core.backends.surreal.schema_version import (
    GRAPH_SCHEMA_CURRENT_VERSION,
    apply_schema_migrations,
    ensure_schema_version_table,
    get_schema_version,
    record_schema_version,
)
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive_phase_receipts import ArchiveCreatedIdentity, ArchivePhaseKey
from sibyl_core.services.archive_phase_store import (
    prepare_archive_phase_transaction,
    read_archive_phase_receipt,
)
from tests.test_archive_phase_receipts import binding

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def phase_client():
    namespace = "archive_phase_foundation_author_" + uuid4().hex
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace=namespace,
    )
    try:
        await client.execute_query("""
            DEFINE TABLE raw_captures SCHEMALESS;
            DEFINE INDEX phase_synthetic_raw_uuid ON raw_captures FIELDS uuid UNIQUE;
            DEFINE TABLE entity SCHEMALESS;
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
        await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        await client.close()


def create_tx(model, token, *, revision=0, batch=0, record_id=None, body_suffix=""):
    key = ArchivePhaseKey(binding=model, store="content", action="apply", batch_sequence=batch)
    record = {
        "uuid": record_id or str(uuid4()),
        "organization_id": model.organization_id,
        "principal_id": model.actor_id,
        "memory_scope": "private",
        "scope_key": model.actor_id,
        "revision": 1,
        "title": "Synthetic",
        "raw_content": "Receipt composition fixture",
        "metadata": {},
    }
    tx = prepare_archive_phase_transaction(
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        key=key,
        expected_revision=revision,
        expected_token=token,
        writer_statements="""CREATE raw_captures CONTENT $record;
        LET $sibyl_archive_phase_outcomes = [{kind: 'raw_capture', disposition: 'created', destination_id: $record.uuid}];
        """
        + body_suffix,
        writer_parameters={"record": record},
        planned_creates=(
            ArchiveCreatedIdentity(kind="raw_capture", destination_id=record["uuid"]),
        ),
    )
    return key, tx, record


async def commit(client, tx):
    return await client.execute_query(tx.query, **tx.parameters)


async def test_archive_phase_actual_source_events_and_lost_reply_exact_replay(phase_client):
    model, token = binding(), str(uuid4())
    key, tx, record = create_tx(model, token)
    await commit(phase_client, tx)
    before = await phase_client.execute_query("SELECT * FROM archive_phase_controls;")
    first = await read_archive_phase_receipt(phase_client.execute_query, key=key, token=token)
    second = await read_archive_phase_receipt(phase_client.execute_query, key=key, token=token)
    assert first == second
    assert first.counts[0].created == 1
    row = first.introduced[0]
    state = (
        await phase_client.execute_query(
            "SELECT * FROM source_states WHERE source_id=$id;", id=record["uuid"]
        )
    )[0]
    assert row.revision == row.source_state_revision == state["revision"] == 1
    assert row.source_generation == state["generation"] == 1
    assert row.source_incarnation == state["incarnation"]
    assert before == await phase_client.execute_query("SELECT * FROM archive_phase_controls;")
    assert len(await phase_client.execute_query("SELECT * FROM raw_captures;")) == 1
    with pytest.raises(SurrealError, match=r"admission changed|already exists|index"):
        await commit(phase_client, tx)
    assert len(await phase_client.execute_query("SELECT * FROM raw_captures;")) == 1


@pytest.mark.parametrize(
    "suffix",
    [
        "CREATE raw_captures CONTENT $record;",
        "THROW 'synthetic second write failed';",
        "DELETE raw_captures WHERE uuid=$record.uuid;",
        "UPDATE source_states SET incarnation=NONE WHERE source_id=$record.uuid;",
        "LET $sibyl_archive_phase_outcomes=[{kind:'source_state', disposition:'created', destination_id:$record.uuid}];",
        "LET $sibyl_archive_phase_outcomes=[{kind:'raw_capture', disposition:'created', destination_id:$record.uuid}, {kind:'raw_capture', disposition:'created', destination_id:$record.uuid}];",
    ],
)
async def test_archive_phase_failure_rolls_back_data_control_and_receipt(phase_client, suffix):
    _, tx, _ = create_tx(binding(), str(uuid4()), body_suffix=suffix)
    with pytest.raises(SurrealError):
        await commit(phase_client, tx)
    for table in (
        "raw_captures",
        "source_states",
        "archive_phase_controls",
        "archive_phase_receipts",
    ):
        assert await phase_client.execute_query(f"SELECT * FROM {table};") == []


async def test_archive_phase_native_same_run_race_and_unrelated_same_org(phase_client):
    if not os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL"):
        pytest.skip("native concurrent conflicts require the server")
    model, token = binding(), str(uuid4())
    first_key, first, _ = create_tx(model, token)
    _, second, _ = create_tx(model, token)
    results = await asyncio.gather(
        commit(phase_client, first), commit(phase_client, second), return_exceptions=True
    )
    assert sum(isinstance(result, Exception) for result in results) == 1
    assert len(await phase_client.execute_query("SELECT * FROM raw_captures;")) == 1
    assert len(await phase_client.execute_query("SELECT * FROM archive_phase_controls;")) == 1
    assert len(await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")) == 1
    assert await read_archive_phase_receipt(phase_client.execute_query, key=first_key, token=token)
    unrelated = model.model_copy(update={"run_id": str(uuid4()), "artifact_id": str(uuid4())})
    _, third, _ = create_tx(unrelated, str(uuid4()))
    next_key, fourth, _ = create_tx(model, token, revision=1, batch=1)
    await asyncio.gather(commit(phase_client, third), commit(phase_client, fourth))
    assert len(await phase_client.execute_query("SELECT * FROM raw_captures;")) == 3
    assert await read_archive_phase_receipt(phase_client.execute_query, key=next_key, token=token)


async def test_archive_phase_terminal_rollback_closes_old_apply_even_existing_receipt(phase_client):
    model, token, rollback_token = binding(), str(uuid4()), str(uuid4())
    key, tx, record = create_tx(model, token)
    await commit(phase_client, tx)
    introduced = (
        await read_archive_phase_receipt(phase_client.execute_query, key=key, token=token)
    ).introduced
    rollback_key = ArchivePhaseKey(
        binding=model, store="content", action="rollback", batch_sequence=0
    )
    rollback = prepare_archive_phase_transaction(
        url=phase_client._url,
        key=rollback_key,
        expected_revision=1,
        expected_token=token,
        rollback_token=rollback_token,
        terminal=True,
        retirement_candidates=introduced,
        writer_statements="""DELETE raw_captures WHERE organization_id=$org AND uuid=$id;
        LET $sibyl_archive_phase_outcomes=[{kind:'raw_capture', disposition:'retired', destination_id:$id}];""",
        writer_parameters={"org": model.organization_id, "id": record["uuid"]},
    )
    await commit(phase_client, rollback)
    result = await read_archive_phase_receipt(
        phase_client.execute_query, key=rollback_key, token=rollback_token
    )
    assert result.terminal and result.counts[0].retired == 1
    assert result.retired[0].source_incarnation == introduced[0].source_incarnation
    assert result.retired[0].source_generation > introduced[0].source_generation
    for replay_token in (token, rollback_token):
        with pytest.raises(SurrealError, match="token is closed"):
            await read_archive_phase_receipt(
                phase_client.execute_query, key=key, token=replay_token
            )
    _, stale, _ = create_tx(model, token, revision=2, batch=1)
    with pytest.raises(SurrealError, match="admission changed"):
        await commit(phase_client, stale)
    assert await phase_client.execute_query("SELECT * FROM raw_captures;") == []
    assert len(await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")) == 2
    for table in ("archive_phase_controls", "archive_phase_receipts"):
        with pytest.raises(SurrealError, match=r"cannot be deleted|immutable"):
            await phase_client.execute_query(f"DELETE {table};")


async def test_archive_phase_edited_introduced_row_is_preserved(phase_client):
    model, token = binding(), str(uuid4())
    key, tx, record = create_tx(model, token)
    await commit(phase_client, tx)
    introduced = (
        await read_archive_phase_receipt(phase_client.execute_query, key=key, token=token)
    ).introduced
    await phase_client.execute_query(
        "UPDATE raw_captures SET raw_content='Concurrent edit', revision=2 WHERE uuid=$id;",
        id=record["uuid"],
    )
    rollback = prepare_archive_phase_transaction(
        url=phase_client._url,
        key=ArchivePhaseKey(binding=model, store="content", action="rollback", batch_sequence=0),
        expected_revision=1,
        expected_token=token,
        rollback_token=str(uuid4()),
        terminal=True,
        retirement_candidates=introduced,
        writer_statements="DELETE raw_captures WHERE uuid=$id; LET $sibyl_archive_phase_outcomes=[];",
        writer_parameters={"id": record["uuid"]},
    )
    with pytest.raises(SurrealError, match="candidate changed"):
        await commit(phase_client, rollback)
    assert (await phase_client.execute_query("SELECT * FROM raw_captures;"))[0][
        "raw_content"
    ] == "Concurrent edit"
    assert (await phase_client.execute_query("SELECT * FROM archive_phase_controls;"))[0][
        "state"
    ] == "open"
    assert len(await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")) == 1


async def test_archive_phase_actor_and_org_reads_and_native_binding_change(phase_client):
    model, token = binding(), str(uuid4())
    key, tx, _ = create_tx(model, token)
    await commit(phase_client, tx)
    for field in ("organization_id", "actor_id"):
        other = key.model_copy(update={"binding": model.model_copy(update={field: str(uuid4())})})
        assert (
            await read_archive_phase_receipt(phase_client.execute_query, key=other, token=token)
            is None
        )
    with pytest.raises(SurrealError, match=r"binding|revision"):
        await phase_client.execute_query(
            "UPDATE archive_phase_controls SET actor_id=$actor, revision=revision+1;",
            actor=str(uuid4()),
        )
    with pytest.raises(SurrealError, match="requires its committed receipt"):
        await phase_client.execute_query("UPDATE archive_phase_controls SET revision=revision+1;")
    with pytest.raises(SurrealError, match="immutable"):
        await phase_client.execute_query("UPDATE archive_phase_receipts SET terminal=true;")


async def test_archive_phase_registration_uses_next_store_versions():
    content_migrations = _content_schema_migrations(url="memory://")
    content = next(
        migration
        for migration in content_migrations
        if migration.name == "content_archive_phase_receipts"
    )
    graph = next(migration for migration in GRAPH_SCHEMA_MIGRATIONS if migration.version == 32)
    assert content.version == 51
    assert graph.version == 32
    assert content_migrations[-1].version == CONTENT_SCHEMA_CURRENT_VERSION
    assert GRAPH_SCHEMA_MIGRATIONS[-1].version == GRAPH_SCHEMA_CURRENT_VERSION
    assert graph.name == "graph_archive_phase_receipts"
    assert content.statements == graph.statements


async def test_archive_phase_existing_row_outcomes_only_cannot_attribute_introduction(phase_client):
    model, token = binding(), str(uuid4())
    key, _, record = create_tx(model, token)
    await phase_client.execute_query("CREATE raw_captures CONTENT $record;", record=record)
    before_rows = await phase_client.execute_query("SELECT * FROM raw_captures;")
    before_states = await phase_client.execute_query("SELECT * FROM source_states;")
    spoof = prepare_archive_phase_transaction(
        url=phase_client._url,
        key=key,
        expected_revision=0,
        expected_token=token,
        planned_creates=(
            ArchiveCreatedIdentity(kind="raw_capture", destination_id=record["uuid"]),
        ),
        writer_statements="LET $sibyl_archive_phase_outcomes=[{kind:'raw_capture', disposition:'created', destination_id:$id}];",
        writer_parameters={"id": record["uuid"]},
    )
    with pytest.raises(SurrealError, match="was not absent"):
        await commit(phase_client, spoof)
    assert before_rows == await phase_client.execute_query("SELECT * FROM raw_captures;")
    assert before_states == await phase_client.execute_query("SELECT * FROM source_states;")
    assert await phase_client.execute_query("SELECT * FROM archive_phase_controls;") == []
    assert await phase_client.execute_query("SELECT * FROM archive_phase_receipts;") == []


@pytest.mark.parametrize(
    "outcome",
    [
        "{kind:'unsupported', disposition:'skipped'}",
        "{kind:'raw_capture', disposition:'created', destination_id:$other}",
        "{kind:'graph_entity', disposition:'created', destination_id:$record.uuid}",
        "{kind:'source_state', disposition:'quarantined', destination_id:$record.uuid}",
    ],
)
async def test_archive_phase_wrong_outcome_rejects_before_native_commit(phase_client, outcome):
    model, token = binding(), str(uuid4())
    _, original, _ = create_tx(model, token)
    body = (
        "CREATE raw_captures CONTENT $record; LET $sibyl_archive_phase_outcomes=[" + outcome + "];"
    )
    parameters = original.parameters
    writer = {"record": parameters["record"], "other": str(uuid4())}
    tx = prepare_archive_phase_transaction(
        url=phase_client._url,
        key=ArchivePhaseKey(binding=model, store="content", action="apply", batch_sequence=0),
        expected_revision=0,
        expected_token=token,
        writer_statements=body,
        writer_parameters=writer,
        planned_creates=(
            ArchiveCreatedIdentity(kind="raw_capture", destination_id=writer["record"]["uuid"]),
        ),
    )
    with pytest.raises(SurrealError, match=r"unsupported|declared cut|active identity"):
        await commit(phase_client, tx)
    for table in (
        "raw_captures",
        "source_states",
        "archive_phase_controls",
        "archive_phase_receipts",
    ):
        assert await phase_client.execute_query(f"SELECT * FROM {table};") == []


async def test_archive_phase_retained_tombstone_cannot_be_reintroduced(phase_client):
    model, token = binding(), str(uuid4())
    _, tx, record = create_tx(model, token)
    await phase_client.execute_query("CREATE raw_captures CONTENT $record;", record=record)
    await phase_client.execute_query("DELETE raw_captures WHERE uuid=$id;", id=record["uuid"])
    before = await phase_client.execute_query("SELECT * FROM source_states;")
    with pytest.raises(SurrealError, match="was not absent"):
        await commit(phase_client, tx)
    assert before == await phase_client.execute_query("SELECT * FROM source_states;")
    assert before[0]["deleted"] is True
    assert await phase_client.execute_query("SELECT * FROM raw_captures;") == []
    assert await phase_client.execute_query("SELECT * FROM archive_phase_receipts;") == []


@pytest.mark.parametrize(
    "endpoint_changed", [False, True, "missing_incarnation", "bookkeeping", "overlap"]
)
async def test_archive_phase_graph_edge_uses_actual_endpoints_and_retirement_absence(
    phase_client, endpoint_changed
):
    if endpoint_changed == "overlap" and not os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL"):
        pytest.skip("native endpoint write conflicts require the server")
    model, token = binding(), str(uuid4())
    source, target, edge = str(uuid4()), str(uuid4()), str(uuid4())
    await phase_client.execute_query(
        """FOR $id IN $ids {
        CREATE entity CONTENT {
            uuid:$id, group_id:$org, revision:1, name:'Synthetic endpoint', entity_type:'topic',
            attributes:{memory_scope:'private', scope_key:$actor, principal_id:$actor}
        };
    };""",
        ids=[source, target],
        org=model.organization_id,
        actor=model.actor_id,
    )
    key = ArchivePhaseKey(binding=model, store="graph", action="apply", batch_sequence=0)
    tx = prepare_archive_phase_transaction(
        url=phase_client._url,
        key=key,
        expected_revision=0,
        expected_token=token,
        planned_creates=(ArchiveCreatedIdentity(kind="graph_relationship", destination_id=edge),),
        writer_statements="""LET $source_record=(SELECT VALUE id FROM entity WHERE uuid=$source AND group_id=$org)[0];
        LET $target_record=(SELECT VALUE id FROM entity WHERE uuid=$target AND group_id=$org)[0];
        RELATE $source_record->relates_to->$target_record CONTENT {
            uuid:$edge, group_id:$org, name:'related_to', fact:'Synthetic edge', source_id:$source, target_id:$target,
            attributes:{memory_scope:'private', scope_key:$actor, principal_id:$actor}, operational_derivation_required:false
        };
        LET $sibyl_archive_phase_outcomes=[{kind:'graph_relationship', disposition:'created', destination_id:$edge}];""",
        writer_parameters={
            "source": source,
            "target": target,
            "edge": edge,
            "org": model.organization_id,
            "actor": model.actor_id,
        },
    )
    if endpoint_changed == "missing_incarnation":
        await phase_client.execute_query(
            "UPDATE source_states SET incarnation=NONE WHERE source_id=$id;", id=source
        )
        before_states = await phase_client.execute_query("SELECT * FROM source_states;")
        with pytest.raises(SurrealError, match="endpoint is unavailable"):
            await commit(phase_client, tx)
        assert before_states == await phase_client.execute_query("SELECT * FROM source_states;")
        assert await phase_client.execute_query("SELECT * FROM relates_to;") == []
        assert await phase_client.execute_query("SELECT * FROM archive_phase_controls;") == []
        assert await phase_client.execute_query("SELECT * FROM archive_phase_receipts;") == []
        return
    await commit(phase_client, tx)
    proof = await read_archive_phase_receipt(phase_client.execute_query, key=key, token=token)
    introduced = proof.introduced[0]
    assert (
        introduced.revision is introduced.source_incarnation is introduced.source_generation is None
    )
    assert introduced.endpoint_ids == (source, target)
    assert len(introduced.endpoint_state_sha256) == 2
    state_before = await phase_client.execute_query(
        "SELECT * OMIT validation_write_witness FROM source_states;"
    )
    rollback_token = str(uuid4())
    rollback_key = ArchivePhaseKey(
        binding=model, store="graph", action="rollback", batch_sequence=0
    )
    rollback = prepare_archive_phase_transaction(
        url=phase_client._url,
        key=rollback_key,
        expected_revision=1,
        expected_token=token,
        rollback_token=rollback_token,
        retirement_candidates=proof.introduced,
        writer_statements=("SLEEP 2s; " if endpoint_changed == "overlap" else "")
        + "DELETE type::record($physical); LET $sibyl_archive_phase_outcomes=[{kind:'graph_relationship', disposition:'retired', destination_id:$edge}];",
        writer_parameters={"physical": introduced.physical_id, "edge": edge},
    )
    if endpoint_changed == "overlap":
        before_control = await phase_client.execute_query("SELECT * FROM archive_phase_controls;")
        task = asyncio.create_task(commit(phase_client, rollback))
        await asyncio.sleep(0.25)
        assert not task.done()
        await phase_client.execute_query(
            "UPDATE entity SET name='Overlapping endpoint edit', revision=2 WHERE uuid=$id;",
            id=source,
        )
        assert not task.done()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, SurrealError)
        assert before_control == await phase_client.execute_query(
            "SELECT * FROM archive_phase_controls;"
        )
        assert len(await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")) == 1
        assert (await phase_client.execute_query("SELECT * FROM relates_to;"))[0]["uuid"] == edge
        assert (
            await phase_client.execute_query(
                "SELECT * FROM source_states WHERE source_id=$id;", id=source
            )
        )[0]["revision"] == 2
        return
    if endpoint_changed == "bookkeeping":
        await phase_client.execute_query(
            "UPDATE source_states SET validation_write_witness=type::string(rand::uuid()) WHERE source_id=$id;",
            id=source,
        )
    if endpoint_changed is True:
        await phase_client.execute_query(
            "UPDATE entity SET name='Edited endpoint', revision=2 WHERE uuid=$id;", id=source
        )
        before_edge = await phase_client.execute_query("SELECT * FROM relates_to;")
        before_control = await phase_client.execute_query("SELECT * FROM archive_phase_controls;")
        with pytest.raises(SurrealError, match="edge endpoint or binding changed"):
            await commit(phase_client, rollback)
        assert before_edge == await phase_client.execute_query("SELECT * FROM relates_to;")
        assert before_control == await phase_client.execute_query(
            "SELECT * FROM archive_phase_controls;"
        )
        assert len(await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")) == 1
        return
    await commit(phase_client, rollback)
    rolled = await read_archive_phase_receipt(
        phase_client.execute_query, key=rollback_key, token=rollback_token
    )
    assert rolled.retired[0].absent
    assert rolled.retired[0].source_generation is None
    states = await phase_client.execute_query("SELECT * FROM source_states;")
    assert all(state["validation_write_witness"] for state in states)
    assert state_before == await phase_client.execute_query(
        "SELECT * OMIT validation_write_witness FROM source_states;"
    )
    with pytest.raises(SurrealError, match="token is closed"):
        await read_archive_phase_receipt(phase_client.execute_query, key=key, token=rollback_token)
    final_key = ArchivePhaseKey(binding=model, store="graph", action="rollback", batch_sequence=1)
    final = prepare_archive_phase_transaction(
        url=phase_client._url,
        key=final_key,
        expected_revision=2,
        expected_token=rollback_token,
        terminal=True,
        writer_statements="LET $sibyl_archive_phase_outcomes=[];",
        writer_parameters={},
    )
    await commit(phase_client, final)
    assert (
        await read_archive_phase_receipt(
            phase_client.execute_query, key=final_key, token=rollback_token
        )
    ).terminal
    assert len(await phase_client.execute_query("SELECT * FROM entity;")) == 2
    assert await phase_client.execute_query("SELECT * FROM relates_to;") == []


async def test_archive_phase_schema_reapplication_hardens_permissions_and_retains_history(
    phase_client,
):
    _, tx, _ = create_tx(binding(), str(uuid4()))
    await commit(phase_client, tx)
    before_receipts = await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")
    before_controls = await phase_client.execute_query("SELECT * FROM archive_phase_controls;")
    for table in ("archive_phase_controls", "archive_phase_receipts"):
        await phase_client.execute_query(f"ALTER TABLE {table} PERMISSIONS FULL;")
    await phase_client.execute_query(
        render_surreal_compatible_sql(ARCHIVE_PHASE_DEFINITIONS, url=phase_client._url)
    )
    info = await phase_client.execute_query("INFO FOR DB;")
    for table in ("archive_phase_controls", "archive_phase_receipts"):
        assert "SCHEMAFULL" in info["tables"][table]
        assert "PERMISSIONS NONE" in info["tables"][table]
    assert before_receipts == await phase_client.execute_query(
        "SELECT * FROM archive_phase_receipts;"
    )
    assert before_controls == await phase_client.execute_query(
        "SELECT * FROM archive_phase_controls;"
    )
    with pytest.raises(SurrealError, match="immutable"):
        await phase_client.execute_query("DELETE archive_phase_receipts;")


@pytest.mark.parametrize("corruption", ["body_sha256", "source_generation", "boolean_count"])
async def test_archive_phase_malformed_native_receipt_cannot_advance_control(
    phase_client, corruption
):
    _, tx, _ = create_tx(binding(), str(uuid4()))
    await commit(phase_client, tx)
    before_controls = await phase_client.execute_query("SELECT * FROM archive_phase_controls;")
    before_receipts = await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")
    forged = dict(before_receipts[0])
    forged.pop("id")
    forged.update(batch_sequence=1, previous_revision=1, committed_revision=2)
    if corruption == "boolean_count":
        forged["counts"][0]["created"] = True
    else:
        forged["introduced"][0][corruption] = "0" * 64 if corruption == "body_sha256" else None
    with pytest.raises(SurrealError, match=r"evidence|integers"):
        await phase_client.execute_query(
            "CREATE archive_phase_receipts CONTENT $forged;", forged=forged
        )
    assert before_controls == await phase_client.execute_query(
        "SELECT * FROM archive_phase_controls;"
    )
    assert len(await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")) == 1
    assert len(await phase_client.execute_query("SELECT * FROM raw_captures;")) == 1


@pytest.mark.parametrize("store,old_version,new_version", [("content", 50, 51), ("graph", 31, 32)])
async def test_archive_phase_registered_upgrade_preserves_prior_native_rows(
    phase_client, store, old_version, new_version
):
    await phase_client.execute_query(
        "REMOVE TABLE archive_phase_controls; REMOVE TABLE archive_phase_receipts;"
    )
    model = binding()
    _, _, record = create_tx(model, str(uuid4()))
    await phase_client.execute_query("CREATE raw_captures CONTENT $record;", record=record)
    before_rows = await phase_client.execute_query("SELECT * FROM raw_captures;")
    before_states = await phase_client.execute_query("SELECT * FROM source_states;")
    migrations = (
        _content_schema_migrations(url=phase_client._url)
        if store == "content"
        else _graph_schema_migrations(url=phase_client._url)
    )
    migrations = tuple(migration for migration in migrations if migration.version <= new_version)
    await ensure_schema_version_table(phase_client.execute_query)
    await record_schema_version(
        phase_client.execute_query, name=store, version=old_version, migrations=migrations[:-1]
    )
    applied = await apply_schema_migrations(phase_client.execute_query, migrations, name=store)
    assert len(applied) == 1 and applied[0].version == new_version
    assert await get_schema_version(phase_client.execute_query, name=store) == new_version
    assert await apply_schema_migrations(phase_client.execute_query, migrations, name=store) == []
    info = await phase_client.execute_query("INFO FOR DB;")
    for table in ("archive_phase_controls", "archive_phase_receipts"):
        assert "SCHEMAFULL" in info["tables"][table] and "PERMISSIONS NONE" in info["tables"][table]
    assert before_rows == await phase_client.execute_query("SELECT * FROM raw_captures;")
    assert before_states == await phase_client.execute_query("SELECT * FROM source_states;")


@pytest.mark.parametrize("store", ["content", "graph"])
async def test_archive_phase_uses_only_canonical_store_inventory(phase_client, store):
    model, token = binding(), str(uuid4())
    if store == "content":
        absent = "relates_to"
        await phase_client.execute_query("REMOVE TABLE relates_to;")
        key, tx, _ = create_tx(model, token)
    else:
        absent = "raw_captures"
        await phase_client.execute_query("REMOVE TABLE raw_captures;")
        identifier = str(uuid4())
        key = ArchivePhaseKey(binding=model, store="graph", action="apply", batch_sequence=0)
        tx = prepare_archive_phase_transaction(
            url=phase_client._url,
            key=key,
            expected_revision=0,
            expected_token=token,
            planned_creates=(
                ArchiveCreatedIdentity(kind="graph_entity", destination_id=identifier),
            ),
            writer_statements="""CREATE entity CONTENT {uuid:$id,group_id:$org,revision:1,name:'Synthetic graph',entity_type:'topic',attributes:{memory_scope:'private',scope_key:$actor,principal_id:$actor}};
            LET $sibyl_archive_phase_outcomes=[{kind:'graph_entity',disposition:'created',destination_id:$id}];""",
            writer_parameters={
                "id": identifier,
                "org": model.organization_id,
                "actor": model.actor_id,
            },
        )
    assert absent not in (await phase_client.execute_query("INFO FOR DB;"))["tables"]
    await commit(phase_client, tx)
    proof = await read_archive_phase_receipt(phase_client.execute_query, key=key, token=token)
    assert len(proof.introduced) == proof.counts[0].created == 1
    assert absent not in (await phase_client.execute_query("INFO FOR DB;"))["tables"]


async def test_archive_phase_registered_fresh_content_bootstrap():
    namespace = "archive_phase_foundation_author_" + uuid4().hex
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace=namespace,
    )
    try:
        assert (await client.execute_query("INFO FOR DB;"))["tables"] == {}
        bootstrap = _content_schema_migrations(url=client._url)[:1]
        applied = await apply_schema_migrations(client.execute_query, bootstrap, name="content")
        assert [migration.version for migration in applied] == [1]
        assert await get_schema_version(client.execute_query, name="content") == 1
        assert await apply_schema_migrations(client.execute_query, bootstrap, name="content") == []
        info = await client.execute_query("INFO FOR DB;")
        for table, expected_events in (
            ("archive_phase_controls", {"archive_phase_control_fence"}),
            (
                "archive_phase_receipts",
                {"archive_phase_receipt_immutable", "archive_phase_receipt_commit"},
            ),
        ):
            assert "SCHEMAFULL" in info["tables"][table]
            assert "PERMISSIONS NONE" in info["tables"][table]
            events = (await client.execute_query(f"INFO FOR TABLE {table};"))["events"]
            assert set(events) == expected_events
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        await client.close()
