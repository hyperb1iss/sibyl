"""Synthetic native data/receipt composition, without an archive apply writer."""

import asyncio
import json
import os
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
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
from sibyl_core.migrate.archive_phase_receipts import (
    ArchiveCreatedIdentity,
    ArchivePhaseKey,
    phase_binding_json,
)
from sibyl_core.migrate.personal_archive_candidates import normalize_archive_candidates
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    CheckedArchivePlan,
    canonical_json,
    checked_plan_bytes,
    checked_plan_digest,
    preview_counts,
)
from sibyl_core.migrate.personal_archive_prepared import (
    PreparedArchiveRow,
    destination_semantic_digest,
)
from sibyl_core.services.archive_phase_store import (
    prepare_archive_phase_transaction,
    read_archive_apply_progress,
    read_archive_phase_receipt,
)
from tests import test_archive_graph_compiler as graph_fixtures
from tests import test_archive_raw_compiler as raw_fixtures
from tests.test_archive_phase_receipts import binding

pytestmark = pytest.mark.asyncio


def phase_namespace():
    registry = os.environ.get("SIBYL_ARCHIVE_PHASE_NS_REGISTRY")
    prefix = "archive_apply_progress_author_" if registry else "archive_phase_foundation_author_"
    namespace = prefix + uuid4().hex
    if registry:
        # Register before client preparation can create the native namespace.
        with Path(registry).open("a") as output:
            output.write(json.dumps({"namespace": namespace}) + "\n")
    return namespace


@pytest.fixture
async def phase_client():
    namespace = phase_namespace()
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
    content = content_migrations[-2]
    graph = next(migration for migration in GRAPH_SCHEMA_MIGRATIONS if migration.version == 32)
    assert content.version == 51
    assert graph.version == 32
    assert content_migrations[-1].version == CONTENT_SCHEMA_CURRENT_VERSION == 52
    assert GRAPH_SCHEMA_MIGRATIONS[-1].version == GRAPH_SCHEMA_CURRENT_VERSION == 35
    assert content.name == "content_archive_phase_receipts"
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
    namespace = phase_namespace()
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


def progress_inputs(tmp_path, *, store="content", count=1):
    parsed, plan = raw_fixtures.inputs(tmp_path, count=count)
    value = raw_fixtures.prepared(parsed, plan)
    return value, ArchivePhaseKey(
        binding=value.binding, store=store, action="apply", batch_sequence=0
    )


def seed_control(key, token):
    return {
        "organization_id": key.binding.organization_id,
        "actor_id": key.binding.actor_id,
        "run_id": key.binding.run_id,
        "store": key.store,
        "binding_json": phase_binding_json(key.binding),
        "binding_sha256": key.binding.sha256,
        "checked_plan_sha256": key.binding.checked_plan_sha256,
        "revision": 0,
        "token": token,
        "state": "open",
    }


async def progress_payload(client, key):
    return await client.execute_query(
        """RETURN {
            controls: (SELECT * FROM archive_phase_controls WHERE organization_id=$org AND run_id=$run),
            receipts: (SELECT * FROM archive_phase_receipts WHERE organization_id=$org AND run_id=$run)
        };""",
        org=key.binding.organization_id,
        run=key.binding.run_id,
    )


async def test_archive_apply_progress_native_seed_commit_lost_ack_and_edited_target(
    phase_client, tmp_path
):
    value, key = progress_inputs(tmp_path)
    missing = await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
    assert (missing.state, missing.expected_revision, missing.token, missing.receipt) == (
        "missing",
        0,
        None,
        None,
    )
    token = str(uuid4())
    await phase_client.execute_query(
        "CREATE archive_phase_controls CONTENT $row;", row=seed_control(key, token)
    )
    initialized = await read_archive_apply_progress(
        phase_client.execute_query, key=key, prepared=value
    )
    assert (initialized.state, initialized.expected_revision, initialized.token) == (
        "missing_initialized",
        0,
        token,
    )
    _, _, tx = raw_fixtures.phase(value, key=key, token=token, url=phase_client._url)
    await commit(phase_client, tx)
    first = await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
    assert first.state == "committed" and first.expected_revision == 1 and first.token == token
    assert first.receipt.counts[0].created == 1
    # Declarations include duplicate archive carriers; phase counts cover the
    # normalized selection once rather than claiming coalesced writes.
    assert raw_fixtures.raw_rows(value)[0].row.declarations > 1
    before = await progress_payload(phase_client, key)
    await phase_client.execute_query(
        "UPDATE raw_captures SET raw_content='Later canonical edit', revision=2;"
    )
    assert (
        await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
        == first
    )
    assert await progress_payload(phase_client, key) == before
    assert len(await phase_client.execute_query("SELECT * FROM raw_captures;")) == 1
    with pytest.raises(FrozenInstanceError):
        first.token = str(uuid4())


@pytest.mark.parametrize(
    "field", ["actor_id", "store", "checked_plan_sha256", "binding_sha256", "binding_json"]
)
async def test_archive_apply_progress_native_foreign_seed_not_hidden(phase_client, tmp_path, field):
    value, key = progress_inputs(tmp_path)
    row = seed_control(key, str(uuid4()))
    row[field] = {
        "actor_id": str(uuid4()),
        "store": "graph",
        "checked_plan_sha256": "0" * 64,
        "binding_sha256": "1" * 64,
        "binding_json": "{}",
    }[field]
    await phase_client.execute_query("CREATE archive_phase_controls CONTENT $row;", row=row)
    progress = await read_archive_apply_progress(
        phase_client.execute_query, key=key, prepared=value
    )
    assert (progress.state, progress.token, progress.receipt) == ("unavailable", None, None)


async def test_archive_apply_progress_native_exact_counts_wrong_introduction(
    phase_client, tmp_path
):
    value, key = progress_inputs(tmp_path)
    _, tx, record = create_tx(value.binding, str(uuid4()))
    assert record["uuid"] != raw_fixtures.raw_rows(value)[0].row.destination_id
    await commit(phase_client, tx)
    actual = await phase_client.execute_query("SELECT * FROM archive_phase_receipts;")
    assert actual[0]["counts"][0]["created"] == 1
    assert actual[0]["introduced"][0]["destination_id"] == record["uuid"]
    progress = await read_archive_apply_progress(
        phase_client.execute_query, key=key, prepared=value
    )
    assert progress.state == "unavailable" and progress.token is None


async def test_archive_apply_progress_native_extra_apply_and_closed_history(phase_client, tmp_path):
    value, key = progress_inputs(tmp_path)
    token = str(uuid4())
    _, _, first = raw_fixtures.phase(value, key=key, token=token, url=phase_client._url)
    await commit(phase_client, first)
    original = await read_archive_apply_progress(
        phase_client.execute_query, key=key, prepared=value
    )
    assert original.state == "committed"
    rollback = prepare_archive_phase_transaction(
        url=phase_client._url,
        key=ArchivePhaseKey(
            binding=value.binding, store="content", action="rollback", batch_sequence=0
        ),
        expected_revision=1,
        expected_token=token,
        rollback_token=str(uuid4()),
        terminal=True,
        retirement_candidates=original.receipt.introduced,
        writer_statements="""DELETE raw_captures WHERE uuid=$id;
        LET $sibyl_archive_phase_outcomes=[{kind:'raw_capture',disposition:'retired',destination_id:$id}];""",
        writer_parameters={"id": original.receipt.introduced[0].destination_id},
    )
    await commit(phase_client, rollback)
    closed = await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
    assert (closed.state, closed.token, closed.receipt) == ("closed", None, None)
    other = replace(value, run_id=str(uuid4()), artifact_id=str(uuid4()))
    other_key = ArchivePhaseKey(
        binding=other.binding, store="content", action="apply", batch_sequence=0
    )
    _, tx0, _ = create_tx(other.binding, token)
    await commit(phase_client, tx0)
    _, tx1, _ = create_tx(other.binding, token, revision=1, batch=1)
    await commit(phase_client, tx1)
    assert len((await progress_payload(phase_client, other_key))["receipts"]) == 2
    extra = await read_archive_apply_progress(
        phase_client.execute_query, key=other_key, prepared=other
    )
    assert (extra.state, extra.token, extra.receipt) == ("unavailable", None, None)


async def test_archive_apply_progress_empty_requires_both_memberships_absent(
    phase_client, tmp_path
):
    value, key = progress_inputs(tmp_path, store="graph")
    assert (
        await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
    ).state == "not_required"
    await phase_client.execute_query(
        "CREATE archive_phase_controls CONTENT $row;", row=seed_control(key, str(uuid4()))
    )
    result = await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
    assert (result.state, result.token) == ("unavailable", None)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_control",
        "duplicate_receipt",
        "orphan",
        "actor_id",
        "store",
        "run_id",
        "organization_id",
        "checked_plan_sha256",
        "binding_sha256",
        "binding_json",
        "phase",
        "action",
        "batch_sequence",
        "previous_revision",
        "committed_revision",
        "token",
        "previous_token",
        "counts",
        "introduced",
        "empty_introduced",
        "bool_revision",
        "unknown_state",
    ],
)
async def test_archive_apply_progress_rejects_corrupted_membership(
    phase_client, tmp_path, mutation
):
    value, key = progress_inputs(tmp_path)
    _, _, tx = raw_fixtures.phase(value, key=key, url=phase_client._url)
    await commit(phase_client, tx)
    payload = deepcopy(await progress_payload(phase_client, key))
    row = payload["receipts"][0]
    if mutation == "duplicate_control":
        payload["controls"].append(deepcopy(payload["controls"][0]))
    elif mutation == "duplicate_receipt":
        payload["receipts"].append(deepcopy(row))
    elif mutation == "orphan":
        payload["controls"] = []
    elif mutation == "bool_revision":
        payload["controls"][0]["revision"] = True
    elif mutation == "unknown_state":
        payload["controls"][0]["state"] = "unknown"
    elif mutation == "counts":
        row["counts"][0]["created"] = 2
    elif mutation == "introduced":
        row["introduced"][0]["destination_id"] = str(uuid4())
    elif mutation == "empty_introduced":
        row["introduced"] = []
    else:
        replacements = {
            "actor_id": str(uuid4()),
            "store": "graph",
            "run_id": str(uuid4()),
            "organization_id": str(uuid4()),
            "checked_plan_sha256": "a" * 64,
            "binding_sha256": "b" * 64,
            "binding_json": "{}",
            "phase": "content_rollback",
            "action": "rollback",
            "batch_sequence": 1,
            "previous_revision": 1,
            "committed_revision": 2,
            "token": str(uuid4()),
            "previous_token": str(uuid4()),
        }
        row[mutation] = replacements[mutation]
    calls = []

    async def execute(query, **params):
        calls.append((query, params))
        return payload

    result = await read_archive_apply_progress(execute, key=key, prepared=value)
    assert (result.state, result.token, result.receipt) == ("unavailable", None, None)
    assert len(calls) == 1 and calls[0][1] == {
        "org": key.binding.organization_id,
        "run": key.binding.run_id,
    }
    assert (
        "actor_id =" not in calls[0][0]
        and "store =" not in calls[0][0]
        and "checked_plan_sha256 =" not in calls[0][0]
    )
    assert calls[0][0].count("LIMIT 2") == 2


async def test_archive_apply_progress_invalid_saved_inputs_before_io(tmp_path):
    value, key = progress_inputs(tmp_path)

    async def execute(query, **params):
        raise AssertionError("invalid saved input reached executor")

    for field, changed in (("actor_id", str(uuid4())), ("run_id", str(uuid4()))):
        wrong = key.model_copy(update={"binding": key.binding.model_copy(update={field: changed})})
        with pytest.raises(ValueError, match="original prepared"):
            await read_archive_apply_progress(execute, key=wrong, prepared=value)
    for changes in ({"action": "rollback"}, {"batch_sequence": 1}):
        with pytest.raises(ValueError, match="original prepared"):
            await read_archive_apply_progress(
                execute, key=key.model_copy(update=changes), prepared=value
            )
    with pytest.raises(ValueError):
        await read_archive_apply_progress(execute, key=key, prepared=replace(value, rows=()))


async def test_archive_apply_progress_native_count_mismatch_without_identity_corruption(
    phase_client, tmp_path
):
    value, key = progress_inputs(tmp_path)
    token = str(uuid4())
    _, _, record = create_tx(
        value.binding, token, record_id=raw_fixtures.raw_rows(value)[0].row.destination_id
    )
    second = {**record, "uuid": str(uuid4())}
    tx = prepare_archive_phase_transaction(
        key=key,
        url=phase_client._url,
        expected_revision=0,
        expected_token=token,
        writer_statements="""CREATE raw_captures CONTENT $first;
        CREATE raw_captures CONTENT $second;
        LET $sibyl_archive_phase_outcomes=[
            {kind:'raw_capture', disposition:'created', destination_id:$first.uuid},
            {kind:'raw_capture', disposition:'created', destination_id:$second.uuid}];""",
        writer_parameters={"first": record, "second": second},
        planned_creates=tuple(
            ArchiveCreatedIdentity(kind="raw_capture", destination_id=row["uuid"])
            for row in (record, second)
        ),
    )
    await commit(phase_client, tx)
    proof = await read_archive_phase_receipt(phase_client.execute_query, key=key, token=token)
    assert proof.counts[0].created == 2 and len(proof.introduced) == 2
    progress = await read_archive_apply_progress(
        phase_client.execute_query, key=key, prepared=value
    )
    assert (progress.state, progress.token) == ("unavailable", None)


async def test_archive_apply_progress_native_graph_compiler_complete_inventory(
    phase_client, tmp_path
):
    parsed, plan = graph_fixtures.inputs(tmp_path)
    value = graph_fixtures.prepared(parsed, plan)
    key, token, tx = graph_fixtures.phase(value, url=phase_client._url)
    assert (
        await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
    ).state == "missing"
    await commit(phase_client, tx)
    progress = await read_archive_apply_progress(
        phase_client.execute_query, key=key, prepared=value
    )
    assert progress.state == "committed" and progress.token == token
    assert {count.kind: count.created for count in progress.receipt.counts} == {
        "graph_entity": 2,
        "graph_relationship": 1,
    }
    assert len(progress.receipt.introduced) == 3


async def test_archive_apply_progress_native_raw_quarantine_normalized_counts(
    phase_client, tmp_path
):
    parsed, plan = raw_fixtures.inputs(tmp_path, count=2, protected=(1,))
    value = raw_fixtures.prepared(parsed, plan)
    key, token, tx = raw_fixtures.phase(value, url=phase_client._url)
    await commit(phase_client, tx)
    progress = await read_archive_apply_progress(
        phase_client.execute_query, key=key, prepared=value
    )
    assert progress.state == "committed" and progress.token == token
    assert (progress.receipt.counts[0].created, progress.receipt.counts[0].quarantined) == (1, 1)
    assert len(progress.receipt.introduced) == 1


async def test_archive_apply_progress_native_mapped_aliases_acknowledge_one_physical_guard(
    phase_client, tmp_path
):
    parsed, plan = graph_fixtures.inputs(tmp_path / "existing", count=1)
    installed = graph_fixtures.prepared(parsed, plan)
    await commit(phase_client, graph_fixtures.phase(installed, url=phase_client._url)[2])
    destination = graph_fixtures.graph_rows(installed)[0].row.destination_id
    await phase_client.execute_query(
        "UPDATE entity SET entity_type='project' WHERE uuid=$id;", id=destination
    )
    snapshot = await graph_fixtures.cut(phase_client, plan.organization_id, destination)
    source_org, owner = str(uuid4()), str(uuid4())
    fixtures = graph_fixtures.fixtures
    records = [fixtures._entity(source_org, owner, entity_type="project") for _ in range(2)]
    records[0]["name"], records[1]["name"] = "First source project", "Second source project"
    archive = fixtures._parsed(
        tmp_path / "aliases", source_org, graph=fixtures._graph(source_org, records)
    )
    mappings = fixtures._mapping(plan.actor_id, owner).model_copy(
        update={"projects": {row["uuid"]: destination for row in records}}
    )
    candidates = normalize_archive_candidates(archive, mappings, actor_id=plan.actor_id)
    rows = tuple(
        candidate.initial_preview(
            organization_id=plan.organization_id, actor_id=plan.actor_id, origin=archive.origin
        ).model_copy(update={"witnesses": (graph_fixtures.witness(destination, snapshot),)})
        for candidate in candidates
    )
    aliases = CheckedArchivePlan(
        organization_id=plan.organization_id,
        actor_id=plan.actor_id,
        origin=archive.origin,
        archive_sha256=archive.archive_sha256,
        artifact_sha256=archive.artifact_sha256,
        mappings=mappings,
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=rows,
        counts=preview_counts(rows),
    )
    value = graph_fixtures.prepared(archive, aliases)
    assert len(graph_fixtures.graph_rows(value)) == 2
    assert all(
        item.body is None and item.row.disposition is ArchiveDisposition.SKIPPED
        for item in graph_fixtures.graph_rows(value)
    )
    key, token, tx = graph_fixtures.phase(value, url=phase_client._url)
    assert len(tx.parameters["archive_graph_guards"]) == 1
    assert (
        await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
    ).state == "missing"
    await commit(phase_client, tx)
    receipt = await read_archive_phase_receipt(phase_client.execute_query, key=key, token=token)
    result = await read_archive_apply_progress(phase_client.execute_query, key=key, prepared=value)
    assert result.state == "committed" and result.receipt == receipt
    assert sum(count.skipped for count in result.receipt.counts) == 2
    assert not result.receipt.introduced
    assert snapshot == await graph_fixtures.cut(phase_client, plan.organization_id, destination)


@pytest.mark.parametrize("malformation", ["duplicate_created", "missing_active"])
async def test_archive_apply_progress_malformed_destinations_before_io(tmp_path, malformation):
    value, _ = progress_inputs(tmp_path, count=2)
    plan = value.plan
    items = list(value.rows)
    selected = raw_fixtures.raw_rows(value)
    first, second = selected[:2]
    body = second.body
    destination = first.row.destination_id if malformation == "duplicate_created" else None
    row = second.row.model_copy(
        update={
            "destination_id": destination,
            "disposition": ArchiveDisposition.CREATED
            if malformation == "duplicate_created"
            else ArchiveDisposition.CONFLICTED,
        }
    )
    if malformation == "duplicate_created":
        body["source_id"] = destination
        row = row.model_copy(update={"semantic_sha256": destination_semantic_digest(row, body)})
    else:
        body = None
    items[items.index(second)] = PreparedArchiveRow(
        row_json=canonical_json(row), body_json=None if body is None else canonical_json(body)
    )
    rows = tuple(item.row for item in items)
    changed = plan.model_copy(update={"rows": rows, "counts": preview_counts(rows)})
    malformed = replace(
        value,
        rows=tuple(items),
        checked_plan_json=checked_plan_bytes(changed),
        checked_plan_sha256=checked_plan_digest(changed),
    )
    # Local prepared validation succeeds: the reader must enforce its physical
    # destination contract rather than pass this malformed selection to I/O.
    assert malformed.plan == changed
    key = ArchivePhaseKey(
        binding=malformed.binding, store="content", action="apply", batch_sequence=0
    )

    async def execute(query, **params):
        raise AssertionError("malformed physical destinations reached executor")

    with pytest.raises(ValueError, match=r"repeats a create|omits an active"):
        await read_archive_apply_progress(execute, key=key, prepared=malformed)
