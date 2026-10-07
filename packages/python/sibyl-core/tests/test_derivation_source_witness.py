"""Canonical association publication must conflict on its exact retained sources."""

import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    _content_schema_migrations,
)
from sibyl_core.backends.surreal.schema import (
    ANALYZER_DEFINITIONS,
    EDGE_DEFINITIONS,
    NODE_DEFINITIONS,
    _graph_schema_migrations,
    render_surreal_compatible_sql,
)
from sibyl_core.backends.surreal.schema_source_integrity import (
    prepare_source_integrity_upgrade,
    source_derivation_event,
)
from sibyl_core.backends.surreal.schema_version import apply_schema_migrations, get_schema_version
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.memory_pipeline.observations import (
    SourceIdentity,
    SourceKind,
    SourceObservation,
    evidence_hash,
)
from sibyl_core.migrate.archive_phase_receipts import ArchivePhaseKey
from sibyl_core.models import Entity, EntityType
from sibyl_core.services.archive_phase_store import (
    prepare_archive_phase_transaction,
    read_archive_phase_receipt,
)
from sibyl_core.services.content_models import RawMemory, raw_memory_record
from sibyl_core.services.content_raw_persistence import replace_raw_memory_records_bulk
from sibyl_core.services.graph_entity_store import _entity_record
from sibyl_core.services.graph_records import _entity_from_row
from sibyl_core.services.source_archive_store import (
    export_source_integrity,
    restore_source_integrity,
)
from sibyl_core.services.source_observations import graph_evidence
from tests.test_archive_phase_receipts import binding

pytestmark = pytest.mark.asyncio(loop_scope="module")


def observe(value):
    if path := os.environ.get("SIBYL_ASSOCIATION_FENCE_EVIDENCE_PATH"):
        with Path(path).open("a") as output:
            output.write(json.dumps(value, default=str, sort_keys=True) + "\n")


def instrument(client, role, trace):
    original = client._send_query

    async def send(native, query, *, params, raw):
        response = await original(native, query, params=params, raw=raw)
        event = {"role": role, "query": query, "parameters": params, "response": response}
        trace.append(event)
        observe({"native": event, "namespace": client._namespace})
        return response

    client._send_query = send


async def initialize(client, name, url, migrations):
    if name == "graph":
        # The real graph bootstrap creates its base tables before the registry,
        # whose historical version-two entry intentionally has no statements.
        await prepare_source_integrity_upgrade(client.execute_query)
        for block in (ANALYZER_DEFINITIONS, NODE_DEFINITIONS, EDGE_DEFINITIONS):
            await client.execute_query(render_surreal_compatible_sql(block, url=url))
    return await apply_schema_migrations(client.execute_query, migrations, name=name)


@pytest_asyncio.fixture(scope="module", loop_scope="module", params=("content", "graph"))
async def store(request):
    namespace = "derivation_source_witness_" + uuid4().hex
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://")
    opts = {
        "url": url,
        "username": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        "password": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        "namespace": namespace,
        "database": request.param,
    }
    client = SurrealContentClient(**opts)
    writer = client if is_embedded_surreal_url(url) else SurrealContentClient(**opts)
    org = str(uuid4())
    migrations = (
        _content_schema_migrations(url=url)
        if request.param == "content"
        else _graph_schema_migrations(url=url, group_id=org)
    )
    kind = SourceKind.RAW_CAPTURE if request.param == "content" else SourceKind.GRAPH_ENTITY
    trace = []
    try:
        applied = await initialize(client, request.param, url, migrations)
        assert applied[-1].version == (
            CONTENT_SCHEMA_CURRENT_VERSION if request.param == "content" else 35
        )
        assert (
            await apply_schema_migrations(client.execute_query, migrations, name=request.param)
            == []
        )
        info = await client.execute_query("INFO FOR DB;")
        assert ("relates_to" if request.param == "content" else "raw_captures") not in info[
            "tables"
        ]
        instrument(client, "phase", trace)
        if writer is not client:
            instrument(writer, "publication", trace)
        yield SimpleNamespace(
            client=client,
            writer=writer,
            url=url,
            name=request.param,
            kind=kind,
            table="raw_captures" if request.param == "content" else "entity",
            org_field="organization_id" if request.param == "content" else "group_id",
            migrations=migrations,
            trace=trace,
        )
    finally:
        await writer.execute_query(f"REMOVE NAMESPACE {namespace};")
        observe({"namespace": namespace, "cleanup": "owned_namespace_removed"})
        if writer is not client:
            await writer.close()
        await client.close()


async def source(store, org):
    identity = str(uuid4())
    if store.name == "content":
        now = datetime.now(UTC)
        record = raw_memory_record(
            RawMemory(
                id=identity,
                organization_id=org,
                source_id=identity,
                principal_id=str(uuid4()),
                title="Synthetic association source",
                raw_content="Native source witness",
                created_at=now,
                captured_at=now,
            )
        )
    else:
        record = _entity_record(
            Entity(
                id=identity,
                entity_type=EntityType.TOPIC,
                name="Synthetic association source",
                content="Native source witness",
                metadata={"memory_scope": "private"},
            ),
            group_id=org,
        )
    await store.client.execute_query(f"CREATE {store.table} CONTENT $record;", record=record)
    return identity


def association(store, org, identity):
    return {
        "organization_id": org,
        "target_kind": store.kind.value,
        "target_id": identity,
        "body_sha256": "b" * 64,
        "principal_id": str(uuid4()),
        "authority_ceiling": {},
        "observations": [],
        "active": True,
    }


async def publish(store, org, identity):
    await store.client.execute_query(
        "CREATE memory_derivations CONTENT $association;",
        association=association(store, org, identity),
    )


async def cut(store, org, identity):
    return await store.client.execute_query(
        f"""RETURN {{
        LET $row = (SELECT * FROM {store.table}
            WHERE {store.org_field}=$org AND uuid=$identity)[0];
        LET $full = (SELECT * FROM source_states
            WHERE organization_id=$org AND source_kind=$kind AND source_id=$identity)[0];
        LET $stable = (SELECT * OMIT validation_write_witness FROM source_states
            WHERE organization_id=$org AND source_kind=$kind AND source_id=$identity)[0];
        LET $association = (SELECT * FROM memory_derivations
            WHERE organization_id=$org AND target_kind=$kind AND target_id=$identity)[0];
        RETURN {{row:$row, state:$stable, full:$full, association:$association,
            row_sha:IF $row=NONE THEN NULL ELSE crypto::sha256(type::string($row)) END,
            state_sha:IF $stable=NONE THEN NULL ELSE crypto::sha256(type::string($stable)) END,
            full_sha:IF $full=NONE THEN NULL ELSE crypto::sha256(type::string($full)) END,
            association_sha:IF $association=NONE THEN NULL ELSE crypto::sha256(type::string($association)) END}};
    }};""",
        org=org,
        kind=store.kind.value,
        identity=identity,
    )


def conflicts(trace, query):
    return [
        statement["result"]
        for item in trace
        if item["query"] == query
        for statement in item["response"].get("result", [])
        if statement.get("status") == "ERR"
        and "Transaction write conflict" in str(statement["result"])
    ]


@pytest.mark.parametrize("winner", ("publication", "phase"))
@pytest.mark.parametrize(
    "mutation", ("create", "create_ordinary", "update", "delete", "rebind_old", "rebind_new")
)
async def test_derivation_source_witness_actual_overlap(store, winner, mutation):
    if is_embedded_surreal_url(store.url):
        pytest.skip("write conflict controls require independent native server transactions")
    model, token = binding(), str(uuid4())
    old = await source(store, model.organization_id)
    new = await source(store, model.organization_id)
    await publish(store, model.organization_id, old)
    # A removed association leaves sticky protection, so CREATE cannot depend on
    # an incidental raw/entity marker write to enlist the source conflict.
    if mutation.startswith("create"):
        await store.client.execute_query(
            "DELETE memory_derivations WHERE organization_id=$org AND target_id=$identity;",
            org=model.organization_id,
            identity=old,
        )
        if mutation == "create_ordinary":
            old = await source(store, model.organization_id)
    if mutation.startswith("rebind"):
        await publish(store, model.organization_id, new)
        await store.client.execute_query(
            "DELETE memory_derivations WHERE organization_id=$org AND target_id=$identity;",
            org=model.organization_id,
            identity=new,
        )
    fenced = new if mutation == "rebind_new" else old
    before = await cut(store, model.organization_id, fenced)
    key = ArchivePhaseKey(binding=model, store=store.name, action="apply", batch_sequence=0)
    check = f"""
        LET $row=(SELECT * FROM {store.table} WHERE {store.org_field}=$org AND uuid=$identity)[0];
        LET $state=(SELECT * OMIT validation_write_witness FROM source_states
            WHERE organization_id=$org AND source_kind=$kind AND source_id=$identity)[0];
        LET $association=(SELECT * FROM memory_derivations
            WHERE organization_id=$org AND target_kind=$kind AND target_id=$identity)[0];
        IF crypto::sha256(type::string($row))!=$row_sha
            OR crypto::sha256(type::string($state))!=$state_sha
            OR (IF $association=NONE THEN NULL ELSE crypto::sha256(type::string($association)) END)!=($association_sha ?? NULL) {{
            THROW 'checked association changed';
        }};
        {"SLEEP 1s;" if winner == "publication" else ""}
        UPDATE $state.id SET validation_write_witness=type::string(rand::uuid());
        LET $sibyl_archive_phase_outcomes=[{{kind:$kind,disposition:'conflicted',destination_id:$identity}}];
    """
    tx = prepare_archive_phase_transaction(
        key=key,
        url=store.url,
        expected_revision=0,
        expected_token=token,
        writer_statements=check,
        writer_parameters={
            "org": model.organization_id,
            "identity": fenced,
            "kind": store.kind.value,
            "row_sha": before["row_sha"],
            "state_sha": before["state_sha"],
            "association_sha": before["association_sha"],
        },
    )
    params = {
        "org": model.organization_id,
        "identity": old,
        "new": new,
        "association": association(store, model.organization_id, old),
    }
    if mutation.startswith("create"):
        operation = "CREATE memory_derivations CONTENT $association;"
    elif mutation == "delete":
        operation = "DELETE memory_derivations WHERE organization_id=$org AND target_id=$identity;"
    elif mutation.startswith("rebind"):
        operation = "UPDATE memory_derivations SET target_id=$new WHERE organization_id=$org AND target_id=$identity;"
    else:
        operation = "UPDATE memory_derivations SET active=false WHERE organization_id=$org AND target_id=$identity;"
    publication = (
        ("BEGIN TRANSACTION;" + operation + "SLEEP 1s; COMMIT TRANSACTION;")
        if winner == "phase"
        else operation
    )

    async def commit_phase():
        try:
            await store.client.execute_query(tx.query, **tx.parameters)
            return True
        except Exception as error:
            assert "checked association changed" in str(error)
            return False

    trace_start = len(store.trace)
    if winner == "publication":
        pending = asyncio.create_task(commit_phase())
        await asyncio.sleep(0.35)
        assert not pending.done()
        await store.writer.execute_query(publication, **params)
        assert not pending.done()
        assert await pending is False
        assert conflicts(store.trace[trace_start:], tx.query)
        assert (
            await read_archive_phase_receipt(store.client.execute_query, key=key, token=token)
            is None
        )
    else:
        pending = asyncio.create_task(store.writer.execute_query(publication, **params))
        await asyncio.sleep(0.35)
        assert not pending.done()
        assert await commit_phase() is True
        assert not pending.done()
        await pending
        assert conflicts(store.trace[trace_start:], publication)
        assert (
            await read_archive_phase_receipt(store.client.execute_query, key=key, token=token)
            is not None
        )
    after = await cut(store, model.organization_id, fenced)
    assert before["state"] == after["state"]
    observe(
        {"case": mutation, "winner": winner, "store": store.name, "before": before, "after": after}
    )


async def test_derivation_source_witness_tombstone_and_finite_retirement(store):
    org = str(uuid4())
    identity = await source(store, org)
    await publish(store, org, identity)
    before = await cut(store, org, identity)
    await store.client.execute_query(
        f"DELETE {store.table} WHERE uuid=$identity;", identity=identity
    )
    retired = await cut(store, org, identity)
    assert retired["row"] is None and retired["state"]["deleted"] is True
    assert retired["state"]["generation"] == before["state"]["generation"] + 1
    assert retired["state"]["incarnation"] == before["state"]["incarnation"]
    assert retired["association"]["active"] is False
    await store.client.execute_query(
        "UPDATE memory_derivations SET active=true WHERE organization_id=$org AND target_id=$identity;",
        org=org,
        identity=identity,
    )
    assert (await cut(store, org, identity))["state"] == retired["state"]
    await store.client.execute_query(
        "DELETE memory_derivations WHERE organization_id=$org AND target_id=$identity;",
        org=org,
        identity=identity,
    )
    assert (await cut(store, org, identity))["state"] == retired["state"]


async def test_derivation_source_witness_missing_state_and_orphan_cleanup(store):
    org = str(uuid4())
    identity = await source(store, org)
    await store.client.execute_query(
        "DELETE source_states WHERE organization_id=$org AND source_id=$identity;",
        org=org,
        identity=identity,
    )
    for target in (identity, str(uuid4())):
        with pytest.raises(Exception, match="derivation target source state is missing"):
            await publish(store, org, target)
        assert (await cut(store, org, target))["association"] is None
        assert (await cut(store, org, target))["state"] is None
    live = await source(store, org)
    await publish(store, org, live)
    await store.client.execute_query(
        "DELETE source_states WHERE organization_id=$org AND source_id=$identity;",
        org=org,
        identity=live,
    )
    damaged = await cut(store, org, live)
    with pytest.raises(Exception, match="derivation target source state is missing"):
        await store.client.execute_query(
            "DELETE memory_derivations WHERE organization_id=$org AND target_id=$identity;",
            org=org,
            identity=live,
        )
    assert await cut(store, org, live) == damaged
    valid = await source(store, org)
    await publish(store, org, valid)
    retained = await cut(store, org, valid)
    with pytest.raises(Exception, match="derivation target source state is missing"):
        await store.client.execute_query(
            "UPDATE memory_derivations SET target_id=$missing WHERE organization_id=$org AND target_id=$identity;",
            missing=identity,
            org=org,
            identity=valid,
        )
    assert await cut(store, org, valid) == retained
    # Historical schema admitted associations without a physical target. The
    # current event must permit removing that inert orphan without inventing history.
    orphan = str(uuid4())
    await store.client.execute_query(
        "REMOVE EVENT require_target_derivation ON memory_derivations;"
    )
    try:
        await publish(store, org, orphan)
    finally:
        await store.client.execute_query(source_derivation_event(store.kind))
    await store.client.execute_query(
        "DELETE memory_derivations WHERE organization_id=$org AND target_id=$identity;",
        org=org,
        identity=orphan,
    )
    assert (await cut(store, org, orphan))["state"] is None
    assert (await cut(store, org, orphan))["association"] is None
    orphan = str(uuid4())
    await store.client.execute_query(
        "REMOVE EVENT require_target_derivation ON memory_derivations;"
    )
    try:
        await publish(store, org, orphan)
    finally:
        await store.client.execute_query(source_derivation_event(store.kind))
    target = await source(store, org)
    before = await cut(store, org, target)
    await store.client.execute_query(
        "UPDATE memory_derivations SET target_id=$target WHERE organization_id=$org AND target_id=$orphan;",
        target=target,
        org=org,
        orphan=orphan,
    )
    after = await cut(store, org, target)
    assert after["association"]["target_id"] == target
    assert after["state"] == before["state"]
    assert after["full_sha"] != before["full_sha"]
    assert (await cut(store, org, orphan))["state"] is None


async def test_derivation_source_witness_org_isolation_and_unrelated_write(store):
    org, foreign = str(uuid4()), str(uuid4())
    identity, other = await source(store, org), await source(store, org)
    before = await cut(store, org, identity)
    unrelated = await cut(store, org, other)
    with pytest.raises(Exception, match="derivation target source state is missing"):
        await publish(store, foreign, identity)
    assert await cut(store, org, identity) == before
    await publish(store, org, identity)
    assert await cut(store, org, other) == unrelated
    await store.writer.execute_query(
        f"UPDATE {store.table} SET title='unrelated native write' WHERE uuid=$identity;"
        if store.name == "content"
        else "UPDATE entity SET name='unrelated native write' WHERE uuid=$identity;",
        identity=other,
    )
    assert (await cut(store, org, other))["row_sha"] != unrelated["row_sha"]


async def test_derivation_source_witness_cross_org_rebinding(store):
    old_org, new_org = str(uuid4()), str(uuid4())
    old, new = await source(store, old_org), await source(store, new_org)
    await publish(store, old_org, old)
    await publish(store, new_org, new)
    await store.client.execute_query(
        "DELETE memory_derivations WHERE organization_id=$org AND target_id=$identity;",
        org=new_org,
        identity=new,
    )
    before_old, before_new = await cut(store, old_org, old), await cut(store, new_org, new)
    await store.client.execute_query(
        "UPDATE memory_derivations SET organization_id=$new_org, target_id=$new WHERE organization_id=$old_org AND target_id=$old;",
        old_org=old_org,
        new_org=new_org,
        old=old,
        new=new,
    )
    after_old, after_new = await cut(store, old_org, old), await cut(store, new_org, new)
    assert after_old["association"] is None
    assert after_new["association"]["organization_id"] == new_org
    for before, after in ((before_old, after_old), (before_new, after_new)):
        assert before["row_sha"] == after["row_sha"]
        assert before["state"] == after["state"]
        assert before["full_sha"] != after["full_sha"]
    observe(
        {
            "case": "cross_org_rebinding",
            "store": store.name,
            "old": {"before": before_old, "after": after_old},
            "new": {"before": before_new, "after": after_new},
        }
    )


async def test_derivation_source_witness_binding_origin_events_remain_intact(store):
    org = str(uuid4())
    identity = await source(store, org)
    events = (await store.client.execute_query("INFO FOR TABLE memory_derivations;"))["events"]
    row = association(store, org, identity)
    if store.name == "content":
        assert {"retain_validation_binding", "retain_derivation_origin"} <= events.keys()
        row.update(
            validation_binding_json="bound",
            validation_entity_id=str(uuid4()),
            origin_execution_id=str(uuid4()),
        )
        fields = (("validation_binding_json", "changed"), ("origin_execution_id", str(uuid4())))
    else:
        # Graph has its own association contract; content-only fields and
        # events must not be imported as an incidental part of the fence.
        assert "retain_validation_binding" not in events
        assert "retain_derivation_origin" not in events
        fields = (("validation_binding_json", "unsupported"), ("origin_execution_id", str(uuid4())))
    await store.client.execute_query(
        "CREATE memory_derivations CONTENT $association;", association=row
    )
    before = await cut(store, org, identity)

    async def change_field(field, value):
        await store.client.execute_query(
            f"UPDATE memory_derivations SET {field}=$value WHERE organization_id=$org AND target_id=$identity;",
            value=value,
            org=org,
            identity=identity,
        )

    for field, value in fields:
        if store.name == "graph" and is_embedded_surreal_url(store.url):
            # The embedded engine discards unknown SCHEMAFULL fields. Native
            # Surreal rejects the same unsupported fields instead.
            await change_field(field, value)
        else:
            with pytest.raises(
                Exception, match="immutable" if store.name == "content" else "no such field exists"
            ):
                await change_field(field, value)
        assert await cut(store, org, identity) == before


async def test_derivation_source_witness_registered_upgrade_retains_history(store):
    namespace = "derivation_source_witness_upgrade_" + uuid4().hex
    client = SurrealContentClient(
        url=store.url,
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace=namespace,
        database=store.name,
    )
    upgrade = SimpleNamespace(**{**vars(store), "client": client, "writer": client})
    try:
        old_version = 51 if store.name == "content" else 32
        historical = tuple(
            migration for migration in store.migrations if migration.version <= old_version + 1
        )
        await initialize(client, store.name, store.url, historical[:-1])
        assert await get_schema_version(client.execute_query, name=store.name) == old_version
        org = str(uuid4())
        identity = await source(upgrade, org)
        await publish(upgrade, org, identity)
        before = await cut(upgrade, org, identity)
        events_before = (await client.execute_query("INFO FOR TABLE memory_derivations;"))["events"]
        applied = await apply_schema_migrations(client.execute_query, historical, name=store.name)
        assert [migration.version for migration in applied] == [old_version + 1]
        assert await cut(upgrade, org, identity) == before
        events_after = (await client.execute_query("INFO FOR TABLE memory_derivations;"))["events"]
        assert events_after.keys() == events_before.keys()
        assert "validation_write_witness" in events_after["require_target_derivation"]
        assert (
            await apply_schema_migrations(client.execute_query, historical, name=store.name) == []
        )
        await client.execute_query(
            "UPDATE memory_derivations SET active=false WHERE organization_id=$org AND target_id=$identity;",
            org=org,
            identity=identity,
        )
        after = await cut(upgrade, org, identity)
        assert after["state"] == before["state"]
        assert after["full_sha"] != before["full_sha"]
        observe(
            {"case": "registered_upgrade", "store": store.name, "before": before, "after": after}
        )
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        observe({"namespace": namespace, "cleanup": "owned_namespace_removed"})
        await client.close()


async def test_derivation_source_witness_overlapping_unrelated_same_org(store):
    if is_embedded_surreal_url(store.url):
        pytest.skip("write conflict controls require independent native server transactions")
    org = str(uuid4())
    target, unrelated = await source(store, org), await source(store, org)
    await publish(store, org, target)
    before = await cut(store, org, unrelated)
    query = "BEGIN TRANSACTION; UPDATE memory_derivations SET active=false WHERE organization_id=$org AND target_id=$identity; SLEEP 1s; COMMIT TRANSACTION;"
    trace_start = len(store.trace)
    pending = asyncio.create_task(store.writer.execute_query(query, org=org, identity=target))
    await asyncio.sleep(0.35)
    assert not pending.done()
    await store.client.execute_query(
        "UPDATE source_states SET validation_write_witness=type::string(rand::uuid()) WHERE organization_id=$org AND source_kind=$kind AND source_id=$identity;",
        org=org,
        kind=store.kind.value,
        identity=unrelated,
    )
    assert not pending.done()
    await pending
    assert not conflicts(store.trace[trace_start:], query)
    assert (await cut(store, org, unrelated))["state"] == before["state"]


async def test_derivation_source_witness_trusted_operator_tombstone_restore(store):
    org = str(uuid4())
    identity, evidence_id = await source(store, org), await source(store, org)
    evidence = await cut(store, org, evidence_id)
    content_sha = (
        evidence_hash({"version": 1, "raw_content": evidence["row"]["raw_content"]})
        if store.name == "content"
        else graph_evidence(_entity_from_row(evidence["row"]))
    )
    captured = SourceObservation(
        source=SourceIdentity(org, store.kind, evidence_id),
        generation=evidence["state"]["generation"],
        content_sha256=content_sha,
        revision=evidence["state"]["revision"],
        durable=True,
        incarnation=evidence["state"]["incarnation"],
    )
    row = association(store, org, identity)
    row["observations"] = [asdict(captured)]
    await store.client.execute_query(
        "CREATE memory_derivations CONTENT $association;", association=row
    )
    await store.client.execute_query(
        f"DELETE {store.table} WHERE uuid=$identity;", identity=identity
    )
    before = await cut(store, org, identity)
    snapshot = await export_source_integrity(
        store.client.execute_query, kind=store.kind, organizations=[org]
    )
    namespace = "derivation_source_witness_restore_" + uuid4().hex
    client = SurrealContentClient(
        url=store.url,
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace=namespace,
        database=store.name,
    )
    target = SimpleNamespace(**{**vars(store), "client": client, "writer": client})
    try:
        await initialize(client, store.name, store.url, store.migrations)
        report = await restore_source_integrity(
            client.execute_query, snapshot, kind=store.kind, organizations=[org]
        )
        assert not report["conflicts"]
        after = await cut(target, org, identity)
        assert after["row"] is None
        assert after["state"]["deleted"] is True
        assert after["state"]["generation"] == before["state"]["generation"]
        assert after["state"]["revision"] == before["state"]["revision"]
        assert after["state"]["incarnation"] == before["state"]["incarnation"]
        assert after["association"]["active"] is False
        observe(
            {
                "case": "trusted_operator_tombstone_restore",
                "store": store.name,
                "before": before,
                "after": after,
                "report": report,
            }
        )
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        observe({"namespace": namespace, "cleanup": "owned_namespace_removed"})
        await client.close()


async def test_derivation_source_witness_canonical_raw_ingestion():
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://")
    namespace = "derivation_source_witness_ingestion_" + uuid4().hex
    client = SurrealContentClient(
        url=url,
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
        namespace=namespace,
    )
    try:
        await initialize(client, "content", url, _content_schema_migrations(url=url))
        org, actor, identity = str(uuid4()), str(uuid4()), str(uuid4())
        now = datetime.now(UTC)
        memory = RawMemory(
            id=identity,
            organization_id=org,
            source_id=identity,
            principal_id=actor,
            title="Canonical ingest",
            raw_content="Canonical derived ingestion",
            captured_at=now,
            created_at=now,
        )
        derivation = {
            "organization_id": org,
            "target_kind": "raw_capture",
            "target_id": identity,
            "body_sha256": hashlib.sha256(memory.raw_content.encode()).hexdigest(),
            "principal_id": actor,
            "authority_ceiling": {},
            "observations": [],
            "active": True,
        }
        rows = await replace_raw_memory_records_bulk(
            client, [raw_memory_record(memory)], derivations=[derivation]
        )
        assert len(rows) == 1 and rows[0]["derivation_required"] is True
        state = (
            await client.execute_query(
                "SELECT * FROM source_states WHERE organization_id=$org AND source_id=$identity;",
                org=org,
                identity=identity,
            )
        )[0]
        assert state["generation"] == state["revision"] == 1
        assert state["deleted"] is False and state["validation_write_witness"]
        observe({"case": "canonical_raw_ingestion", "rows": rows, "state": state})
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        observe({"namespace": namespace, "cleanup": "owned_namespace_removed"})
        await client.close()
