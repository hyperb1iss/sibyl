"""Canonical graph compilation controls using real native schema and receipts."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from surrealdb import SurrealError

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.dedicated_client import _checked_query_result
from sibyl_core.backends.surreal.schema import (
    ANALYZER_DEFINITIONS,
    EDGE_DEFINITIONS,
    NODE_DEFINITIONS,
    _graph_schema_migrations,
    render_surreal_compatible_sql,
)
from sibyl_core.backends.surreal.schema_version import apply_schema_migrations
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.migrate.archive_phase_receipts import ArchivePhaseKey
from sibyl_core.migrate.personal_archive_candidates import normalize_archive_candidates
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveStoreWitness,
    CheckedArchivePlan,
    preview_counts,
)
from sibyl_core.migrate.personal_archive_prepared import (
    archive_semantic_body,
    destination_semantic_digest,
    graph_archive_body,
    prepare_archive_body,
    prepare_archive_records,
    semantic_archive_metadata,
)
from sibyl_core.models import Entity, Relationship, RelationshipType
from sibyl_core.services.archive_graph_compiler import prepare_archive_graph_apply
from sibyl_core.services.archive_phase_store import (
    prepare_archive_phase_transaction,
    read_archive_phase_receipt,
)
from sibyl_core.services.graph_entity_store import _ENTITY_BULK_UPSERT_QUERY, _entity_record
from sibyl_core.services.graph_records import entity_from_surreal_row, relationship_from_surreal_row
from tests import test_personal_archive_candidates as fixtures

_TIMESTAMP = "2026-09-30T01:02:03.123456789Z"


def observe(value):
    if name := os.environ.get("SIBYL_GRAPH_COMPILER_EVIDENCE_PATH"):
        with Path(name).open("a") as output:
            output.write(json.dumps(value, sort_keys=True, default=str) + "\n")


def inputs(tmp_path, *, metadata=None, edge_metadata=None, protected=False, count=2):
    org, owner, actor, destination = (str(uuid4()) for _ in range(4))
    records = [
        fixtures._entity(org, owner, protected=protected, entity_type="task") for _ in range(count)
    ]
    for record in records:
        record["attributes"].update(
            due_date=_TIMESTAMP, user_json={"values": [17, None, {"leaf": None}]}
        )
        record["attributes"].update(metadata or {})
    relationships = []
    if count > 1:
        records[0]["attributes"]["task_id"] = records[1]["uuid"]
        relationship = Relationship(
            id=str(uuid4()),
            source_id=records[0]["uuid"],
            target_id=records[1]["uuid"],
            relationship_type=RelationshipType.RELATED_TO,
            weight=0.375,
            metadata={
                "valid_at": _TIMESTAMP,
                "user_json": {"array": [None, {"x": None}]},
                **(edge_metadata or {}),
            },
        ).model_dump(mode="json")
        relationships.append(relationship)
    parsed = fixtures._parsed(
        tmp_path, org, graph=fixtures._graph(org, records, relationships=relationships)
    )
    mappings = fixtures._mapping(actor, owner)
    candidates = normalize_archive_candidates(parsed, mappings, actor_id=actor)
    initial = {
        (candidate.kind, candidate.original_id): candidate.initial_preview(
            organization_id=destination, actor_id=actor, origin=parsed.origin
        )
        for candidate in candidates
    }
    node_ids = {
        row.original_id: row.destination_id
        for row in initial.values()
        if row.kind is ArchiveKind.GRAPH_ENTITY and row.destination_id
    }
    rows = []
    for candidate in candidates:
        row = initial[candidate.kind, candidate.original_id]
        if candidate.protection == "ordinary" and candidate.kind in {
            ArchiveKind.GRAPH_ENTITY,
            ArchiveKind.GRAPH_RELATIONSHIP,
        }:
            metadata_value = json.loads(candidate.semantic_json).get("metadata", {})
            endpoints = tuple(node_ids[x] for x in candidate.original_endpoint_ids)
            endpoints += tuple(
                node_ids[value]
                for value in dict.fromkeys(
                    metadata_value[field]
                    for field in ("epic_id", "parent_task_id", "task_id", "milestone_id")
                    if isinstance(metadata_value.get(field), str)
                    and metadata_value[field] in node_ids
                )
                if node_ids[value] not in endpoints
            )
            row = row.model_copy(update={"endpoint_ids": endpoints})
            body = prepare_archive_body(
                candidate, row, actor_id=actor, node_ids=node_ids, organization_id=destination
            )
            prefix = "entity:" if row.kind is ArchiveKind.GRAPH_ENTITY else "relates_to:"
            own = ArchiveStoreWitness(store="graph", identity=prefix + row.destination_id)
            witnesses = (
                own,
                *(
                    ArchiveStoreWitness(store="graph", identity="entity:" + identity)
                    for identity in dict.fromkeys(endpoints)
                    if row.kind is ArchiveKind.GRAPH_RELATIONSHIP or identity != row.destination_id
                ),
            )
            row = row.model_copy(
                update={
                    "semantic_sha256": destination_semantic_digest(row, body),
                    "witnesses": witnesses,
                }
            )
        rows.append(row)
    plan = CheckedArchivePlan(
        organization_id=destination,
        actor_id=actor,
        origin=parsed.origin,
        archive_sha256=parsed.archive_sha256,
        artifact_sha256=parsed.artifact_sha256,
        mappings=mappings,
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=tuple(rows),
        counts=preview_counts(tuple(rows)),
    )
    return parsed, plan


def prepared(parsed, plan):
    return prepare_archive_records(parsed, plan, run_id=str(uuid4()), artifact_id=str(uuid4()))


def phase(value, *, url="memory://", token=None, key=None, revision=0):
    key = key or ArchivePhaseKey(
        binding=value.binding, store="graph", action="apply", batch_sequence=0
    )
    token = token or str(uuid4())
    tx = prepare_archive_graph_apply(
        prepared=value, key=key, url=url, expected_revision=revision, expected_token=token
    )
    return key, token, tx


def graph_rows(value):
    return [
        item
        for item in value.rows
        if item.row.kind in {ArchiveKind.GRAPH_ENTITY, ArchiveKind.GRAPH_RELATIONSHIP}
    ]


def change_rows(plan, changes):
    rows = tuple(row.model_copy(update=changes.get(row.original_id, {})) for row in plan.rows)
    return plan.model_copy(update={"rows": rows, "counts": preview_counts(rows)})


def test_graph_compiler_pure_complete_immutable_closed_batch(tmp_path):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    key, token, tx = phase(value)
    candidates = tx.parameters["archive_graph_candidates"]
    assert len(candidates) == 3 and len(tx.parameters["archive_graph_guards"]) == 3
    assert len(tx.parameters["sibyl_archive_phase_planned_creates"]) == 3
    assert {row["kind"] for row in candidates} == {"graph_entity", "graph_relationship"}
    assert phase(value, token=token, key=key)[2] == tx
    snapshot = tx.parameters
    snapshot["archive_graph_candidates"][0]["endpoint_ids"].append("mutated")
    assert "mutated" not in tx.parameters["archive_graph_candidates"][0]["endpoint_ids"]
    assert "_SOURCE_STATE" not in tx.query
    assert tx.query.index("LET $archive_graph_cut") < tx.query.index("UPDATE $source_state.id")
    assert tx.query.index("LET $archive_graph_cut") < tx.query.index("CREATE entity CONTENT")
    assert tx.query.index("CREATE entity CONTENT") < tx.query.index("RELATE $source")
    assert all(
        item.row.declarations > 1
        for item in graph_rows(value)
        if item.row.kind is ArchiveKind.GRAPH_ENTITY
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"raw_source_ids": ["foreign"]},
        {"raw_source_ids": {}},
        {"raw_source_ids": False},
        {"review_capture_id": "foreign"},
        {"parent_entity_id": "foreign"},
        {"parent_entity_id": None},
        {"source_entity_id": "foreign"},
        {"projection_kind": "passage"},
    ],
)
def test_graph_compiler_pure_rejects_hidden_dependencies(tmp_path, metadata):
    parsed, plan = inputs(tmp_path, metadata=metadata)
    assert any(row.disposition is ArchiveDisposition.CREATED for row in plan.rows)
    with pytest.raises(ValueError):
        phase(prepared(parsed, plan))


@pytest.mark.parametrize(
    "metadata",
    [
        {"raw_source_ids": None},
        {"raw_source_ids": ""},
        {"raw_source_ids": []},
        {"raw_source_ids": [" "]},
        {"review_capture_id": None},
    ],
)
def test_graph_compiler_pure_preserves_validated_neutral_metadata(tmp_path, metadata):
    parsed, plan = inputs(tmp_path, metadata=metadata)
    _, _, tx = phase(prepared(parsed, plan))
    assert len(tx.parameters["archive_graph_candidates"]) == 3


def test_graph_import_keeps_the_stored_completion_record(tmp_path):
    """The archive is the server's record of who finished a task, not a caller's claim."""
    stored = {
        "status": "done",
        "completed_at": "2026-09-30T01:02:03.123456789Z",
        "completed_by": str(uuid4()),
        "modified_by": str(uuid4()),
    }
    parsed, plan = inputs(tmp_path, metadata=stored)
    value = prepared(parsed, plan)
    bodies = [item.body for item in graph_rows(value) if item.row.kind is ArchiveKind.GRAPH_ENTITY]
    assert bodies
    for body in bodies:
        assert body is not None
        # modified_by is a storage marker the import never carries.
        for key in ("completed_at", "completed_by"):
            assert body["metadata"][key] == stored[key], key


@pytest.mark.parametrize(
    "field",
    [
        "organization_id",
        "actor_id",
        "run_id",
        "artifact_id",
        "archive_sha256",
        "artifact_sha256",
        "mappings_sha256",
        "checked_plan_sha256",
        "credential",
    ],
)
def test_graph_compiler_pure_binds_every_run_field(tmp_path, field):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    changed = (
        {"credential_kind": "api_key", "api_key_id": str(uuid4())}
        if field == "credential"
        else "0" * 64
        if field.endswith("sha256")
        else str(uuid4())
    )
    key = ArchivePhaseKey(
        binding=value.binding.model_copy(update={field: changed}),
        store="graph",
        action="apply",
        batch_sequence=0,
    )
    with pytest.raises(ValueError):
        phase(value, key=key)


@pytest.mark.parametrize(
    "change", [{"store": "content"}, {"action": "rollback"}, {"batch_sequence": 1}]
)
def test_graph_compiler_pure_rejects_phase_relabeling(tmp_path, change):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    key = ArchivePhaseKey(
        binding=value.binding, store="graph", action="apply", batch_sequence=0
    ).model_copy(update=change)
    with pytest.raises(ValueError, match="phase key"):
        phase(value, key=key)


def test_graph_compiler_pure_rejects_tampered_snapshot(tmp_path):
    parsed, plan = inputs(tmp_path)
    value = prepared(parsed, plan)
    with pytest.raises(ValueError):
        phase(replace(value, checked_plan_sha256="0" * 64))
    with pytest.raises(ValueError):
        phase(replace(value, rows=value.rows[:-1]))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def native():
    namespace = "archive_graph_compiler_author_" + uuid4().hex
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://")
    options = {
        "url": url,
        "namespace": namespace,
        "pool_size": 1,
        "username": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        "password": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    }
    client = SurrealContentClient(**options)
    writer = client if is_embedded_surreal_url(url) else SurrealContentClient(**options)
    trace = []
    try:
        await client.execute_query(
            render_surreal_compatible_sql(
                ANALYZER_DEFINITIONS + NODE_DEFINITIONS + EDGE_DEFINITIONS, url=url
            )
        )
        await apply_schema_migrations(
            client.execute_query, _graph_schema_migrations(url=url), name="graph"
        )
        await client.execute_query(
            "DEFINE TABLE archive_graph_compiler_test_markers SCHEMALESS; DEFINE FIELD archive_graph_compiler_unknown ON source_states TYPE option<string>;"
        )
        for role, connection in [("phase", client), ("canonical_writer", writer)]:
            if connection is client and role != "phase":
                continue
            original = connection._send_query

            async def send(socket, query, *, params, raw, original=original, role=role):
                response = await original(socket, query, params=params, raw=raw)
                event = {
                    "role": role,
                    "socket": id(socket),
                    "query": query,
                    "params": params,
                    "response": response,
                }
                trace.append(event)
                observe({"namespace": namespace, "native": event})
                return response

            connection._send_query = send
        await writer.execute_query("RETURN {namespace:session::ns(),database:session::db()};")
        yield SimpleNamespace(
            client=client, writer=writer, url=url, trace=trace, namespace=namespace
        )
    finally:
        await client.execute_query(f"REMOVE NAMESPACE {namespace};")
        assert namespace not in (await client.execute_query("INFO FOR ROOT;"))["namespaces"]
        observe(
            {"namespace": namespace, "cleanup": "owned_namespace_removed_and_inventory_checked"}
        )
        if writer is not client:
            await writer.close()
        await client.close()


async def cut(client, org, identity, *, kind=ArchiveKind.GRAPH_ENTITY, scheme="native-full-v1"):
    table = "entity" if kind is ArchiveKind.GRAPH_ENTITY else "relates_to"
    query = (
        """RETURN {
        LET $row=(SELECT * FROM """
        + table
        + """ WHERE uuid=$identity)[0];
        LET $state=(SELECT * OMIT validation_write_witness FROM source_states
            WHERE organization_id=$org AND source_kind='graph_entity' AND source_id=$identity)[0];
        LET $association=IF $scheme='graph-association-authority-v2' THEN
            (SELECT * OMIT validation_write_witness FROM memory_derivations
                WHERE organization_id=$org AND target_kind='graph_entity' AND target_id=$identity)[0]
            ELSE (SELECT * FROM memory_derivations
                WHERE organization_id=$org AND target_kind='graph_entity' AND target_id=$identity)[0] END;
        RETURN {row:$row,state:$state,association:$association,
            row_sha:IF $row=NONE THEN NULL ELSE crypto::sha256(type::string($row)) END,
            state_sha:IF $state=NONE THEN NULL ELSE crypto::sha256(type::string($state)) END,
            association_sha:IF $association=NONE THEN NULL ELSE crypto::sha256(type::string($association)) END};
    };"""
    )
    return await client.execute_query(query, identity=identity, org=org, scheme=scheme)


def witness(identity, snapshot, *, edge=False):
    return ArchiveStoreWitness(
        store="graph",
        identity=("relates_to:" if edge else "entity:") + identity,
        row_sha256=snapshot["row_sha"],
        state_sha256=None if edge else snapshot["state_sha"],
        associations_sha256=None if edge else snapshot["association_sha"],
    )


async def existing(native, tmp_path, *, scheme=None):
    parsed, plan = inputs(tmp_path)
    initial = prepared(parsed, plan)
    key, token, tx = phase(initial, url=native.url)
    await native.client.execute_query(tx.query, **tx.parameters)
    assert (
        len(
            (
                await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
            ).introduced
        )
        == 3
    )
    effective = scheme or "native-full-v1"
    changes = {}
    for item in graph_rows(initial):
        row = item.row
        snapshot = await cut(
            native.client, plan.organization_id, row.destination_id, kind=row.kind, scheme=effective
        )
        witnesses = [
            witness(row.destination_id, snapshot, edge=row.kind is ArchiveKind.GRAPH_RELATIONSHIP)
        ]
        for endpoint in dict.fromkeys(row.endpoint_ids):
            if row.kind is ArchiveKind.GRAPH_ENTITY and endpoint == row.destination_id:
                continue
            witnesses.append(
                witness(
                    endpoint,
                    await cut(native.client, plan.organization_id, endpoint, scheme=effective),
                )
            )
        changes[row.original_id] = {
            "disposition": ArchiveDisposition.SKIPPED,
            "reason": "destination_canonical_identical",
            "witnesses": tuple(witnesses),
        }
    plan = change_rows(plan, changes)
    if scheme:
        plan = plan.model_copy(update={"witness_scheme": scheme})
    return parsed, plan, prepared(parsed, plan)


async def no_receipt(native, value):
    assert (
        await native.client.execute_query(
            "SELECT * FROM archive_phase_receipts WHERE run_id=$run;", run=value.run_id
        )
        == []
    )
    assert (
        await native.client.execute_query(
            "SELECT * FROM archive_phase_controls WHERE run_id=$run;", run=value.run_id
        )
        == []
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_graph_compiler_native_exact_create_nulls_dates_weight_and_physical_endpoints(
    native, tmp_path
):
    payload = {'odd"key; COMMIT TRANSACTION;': "NULL; BEGIN TRANSACTION; $x"}
    parsed, plan = inputs(tmp_path, metadata=payload, edge_metadata={"literal_date": _TIMESTAMP})
    value = prepared(parsed, plan)
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(tx.query, **tx.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    assert len(receipt.introduced) == 3
    for item in graph_rows(value):
        row = item.row
        snapshot = await cut(native.client, plan.organization_id, row.destination_id, kind=row.kind)
        actual = snapshot["row"]
        evidence = next(x for x in receipt.introduced if x.destination_id == row.destination_id)
        assert evidence.row_sha256 == snapshot["row_sha"] and evidence.physical_id == str(
            actual["id"]
        )
        if row.kind is ArchiveKind.GRAPH_ENTITY:
            assert (
                graph_archive_body(
                    entity_from_surreal_row(actual), organization_id=plan.organization_id
                )
                == item.body
            )
            assert actual["created_by"] == actual["modified_by"] == plan.actor_id
            assert actual["attributes"]["due_date"] == _TIMESTAMP
        else:
            body = relationship_from_surreal_row(actual).model_dump(
                mode="json", exclude={"created_at"}
            )
            body["metadata"] = semantic_archive_metadata(body["metadata"])
            assert archive_semantic_body(body, row.kind) == archive_semantic_body(
                item.body, row.kind
            )
            assert body["weight"] == 0.375
            proof = await native.client.execute_query(
                "RETURN {date:type::string((SELECT * FROM relates_to WHERE uuid=$id)[0].valid_at),null:(SELECT * FROM relates_to WHERE uuid=$id)[0].attributes.user_json.array[0]=NULL};",
                id=row.destination_id,
            )
            assert proof == {"date": _TIMESTAMP, "null": True}
            nodes = [
                await cut(native.client, plan.organization_id, x) for x in row.endpoint_ids[:2]
            ]
            assert (actual["in"], actual["out"]) == tuple(x["row"]["id"] for x in nodes)
            assert tuple(evidence.endpoint_ids) == row.endpoint_ids[:2]
    assert set(count.kind for count in receipt.counts) == {"graph_entity", "graph_relationship"}
    observe(
        {
            "case": "exact_create",
            "receipt": receipt.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
        }
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_graph_compiler_native_skips_retain_full_native_rows(native, tmp_path):
    _, plan, value = await existing(native, tmp_path)
    before = [
        await cut(native.client, plan.organization_id, item.row.destination_id, kind=item.row.kind)
        for item in graph_rows(value)
    ]
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(tx.query, **tx.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    after = [
        await cut(native.client, plan.organization_id, item.row.destination_id, kind=item.row.kind)
        for item in graph_rows(value)
    ]
    assert before == after
    assert len(receipt.introduced) == 0 and sum(x.skipped for x in receipt.counts) == 3
    observe(
        {
            "case": "skipped_full_preservation",
            "receipt": receipt.model_dump(mode="json"),
            "cuts": after,
        }
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_graph_compiler_native_quarantine_only_counts_graph_rows(native, tmp_path):
    parsed, plan = inputs(tmp_path, protected=True)
    value = prepared(parsed, plan)
    key, token, tx = phase(value, url=native.url)
    assert all(
        x["disposition"] == "quarantined" and x["destination_id"] is None
        for x in tx.parameters["archive_graph_candidates"]
    )
    await native.client.execute_query(tx.query, **tx.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    assert receipt.introduced == () and sum(x.quarantined for x in receipt.counts) == 3
    assert set(count.kind for count in receipt.counts) == {"graph_entity", "graph_relationship"}


async def checked_after(
    native, parsed, plan, *, disposition=ArchiveDisposition.SKIPPED, scheme="native-full-v1"
):
    updates = {}
    for item in graph_rows(prepared(parsed, plan)):
        row = item.row
        snapshots = [
            witness(
                row.destination_id,
                await cut(
                    native.client,
                    plan.organization_id,
                    row.destination_id,
                    kind=row.kind,
                    scheme=scheme,
                ),
                edge=row.kind is ArchiveKind.GRAPH_RELATIONSHIP,
            )
        ]
        for endpoint in dict.fromkeys(row.endpoint_ids):
            if row.kind is ArchiveKind.GRAPH_ENTITY and endpoint == row.destination_id:
                continue
            snapshots.append(
                witness(
                    endpoint,
                    await cut(native.client, plan.organization_id, endpoint, scheme=scheme),
                )
            )
        updates[row.original_id] = {"disposition": disposition, "witnesses": tuple(snapshots)}
    changed = change_rows(plan, updates).model_copy(
        update={"witness_scheme": None if scheme == "native-full-v1" else scheme}
    )
    return changed, prepared(parsed, changed)


async def publish(client, org, identity):
    association = {
        "organization_id": org,
        "target_kind": "graph_entity",
        "target_id": identity,
        "body_sha256": "a" * 64,
        "principal_id": str(uuid4()),
        "authority_ceiling": {},
        "observations": [],
        "active": True,
    }
    await client.execute_query(
        "CREATE memory_derivations CONTENT $association;", association=association
    )
    return association


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    "mutation",
    [
        "node_body",
        "edge_attributes",
        "edge_one_ns",
        "node_delete",
        "edge_delete",
        "physical_replacement",
        "incarnation",
        "generation",
        "revision",
        "deleted",
        "missing_ledger",
        "unknown_state",
        "association",
    ],
)
async def test_graph_compiler_native_stale_cut_leaves_no_phase_effect(native, tmp_path, mutation):
    _, plan, value = await existing(native, tmp_path)
    node = next(x.row for x in graph_rows(value) if x.row.kind is ArchiveKind.GRAPH_ENTITY)
    edge = next(x.row for x in graph_rows(value) if x.row.kind is ArchiveKind.GRAPH_RELATIONSHIP)
    key, _token, tx = phase(value, url=native.url)
    org, identity = plan.organization_id, node.destination_id
    if mutation == "node_body":
        await native.client.execute_query(
            "UPDATE entity SET attributes.unknown_future_field={null:NULL,nested:[NULL]} WHERE uuid=$id;",
            id=identity,
        )
    elif mutation == "edge_attributes":
        await native.client.execute_query(
            "UPDATE relates_to SET attributes.unknown_future_field='change' WHERE uuid=$id;",
            id=edge.destination_id,
        )
    elif mutation == "edge_one_ns":
        await native.client.execute_query(
            "UPDATE relates_to SET valid_at=<datetime>$stamp WHERE uuid=$id;",
            id=edge.destination_id,
            stamp=_TIMESTAMP[:-2] + "8Z",
        )
    elif mutation in {"node_delete", "edge_delete"}:
        await native.client.execute_query(
            ("DELETE entity" if mutation == "node_delete" else "DELETE relates_to")
            + " WHERE uuid=$id;",
            id=identity if mutation == "node_delete" else edge.destination_id,
        )
    elif mutation == "physical_replacement":
        await native.client.execute_query(
            "BEGIN TRANSACTION; LET $old=(SELECT * FROM relates_to WHERE uuid=$id)[0];"
            "DELETE $old.id; LET $source=$old.in; LET $target=$old.out; RELATE $source->relates_to->$target CONTENT object::from_entries("
            "object::entries($old).filter(|$entry| $entry[0] NOT IN ['id','in','out'])); COMMIT TRANSACTION;",
            id=edge.destination_id,
        )
    elif mutation == "association":
        await publish(native.client, org, identity)
    elif mutation == "missing_ledger":
        await native.client.execute_query(
            "DELETE source_states WHERE organization_id=$org AND source_id=$id;",
            org=org,
            id=identity,
        )
    else:
        fields = {
            "incarnation": "incarnation=type::string(rand::uuid())",
            "generation": "generation+=1",
            "revision": "revision+=1",
            "deleted": "deleted=true",
            "unknown_state": "archive_graph_compiler_unknown='changed'",
        }
        await native.client.execute_query(
            "UPDATE source_states SET "
            + fields[mutation]
            + " WHERE organization_id=$org AND source_id=$id;",
            org=org,
            id=identity,
        )
    changed = await cut(native.client, org, identity)
    with pytest.raises(SurrealError, match=r"witness changed|not absent|invalid"):
        await native.client.execute_query(tx.query, **tx.parameters)
    await no_receipt(native, value)
    assert await cut(native.client, org, identity) == changed
    observe(
        {
            "case": "stale_cut_atomic_failure",
            "mutation": mutation,
            "key": key.model_dump(mode="json"),
            "before_failed_apply": changed,
            "receipt": None,
        }
    )


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("scheme", ["native-full-v1", "graph-association-authority-v2"])
async def test_graph_compiler_native_conflict_association_scheme_is_exact(native, tmp_path, scheme):
    parsed, plan, _ = await existing(native, tmp_path)
    identity = next(x.destination_id for x in plan.rows if x.kind is ArchiveKind.GRAPH_ENTITY)
    await publish(native.client, plan.organization_id, identity)
    plan, value = await checked_after(
        native, parsed, plan, disposition=ArchiveDisposition.CONFLICTED, scheme=scheme
    )
    before = await cut(native.client, plan.organization_id, identity, scheme=scheme)
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(
        "UPDATE memory_derivations SET validation_write_witness=type::string(rand::uuid()) WHERE organization_id=$org AND target_id=$id;",
        org=plan.organization_id,
        id=identity,
    )
    after = await cut(native.client, plan.organization_id, identity, scheme=scheme)
    assert before["row_sha"] == after["row_sha"] and before["state_sha"] == after["state_sha"]
    if scheme == "native-full-v1":
        assert before["association_sha"] != after["association_sha"]
        with pytest.raises(SurrealError, match="witness changed"):
            await native.client.execute_query(tx.query, **tx.parameters)
        await no_receipt(native, value)
    else:
        assert before["association_sha"] == after["association_sha"]
        await native.client.execute_query(tx.query, **tx.parameters)
        receipt = await read_archive_phase_receipt(
            native.client.execute_query, key=key, token=token
        )
        assert not receipt.introduced and sum(x.conflicted for x in receipt.counts) == 3
        assert after == await cut(native.client, plan.organization_id, identity, scheme=scheme)


@pytest.mark.asyncio(loop_scope="module")
async def test_graph_compiler_native_all_checked_cuts_precede_mixed_batch_creates(native, tmp_path):
    parsed, plan = inputs(tmp_path, count=3)
    referenced = {identity for row in plan.rows for identity in row.endpoint_ids}
    new = next(
        row
        for row in plan.rows
        if row.kind is ArchiveKind.GRAPH_ENTITY and row.destination_id not in referenced
    )
    initial_plan = change_rows(
        plan, {new.original_id: {"disposition": ArchiveDisposition.QUARANTINED}}
    )
    initial = prepared(parsed, initial_plan)
    initial_tx = phase(initial, url=native.url)[2]
    await native.client.execute_query(initial_tx.query, **initial_tx.parameters)
    changes = {}
    for row in plan.rows:
        if (
            row.kind not in {ArchiveKind.GRAPH_ENTITY, ArchiveKind.GRAPH_RELATIONSHIP}
            or row.original_id == new.original_id
        ):
            continue
        witnesses = [
            witness(
                row.destination_id,
                await cut(native.client, plan.organization_id, row.destination_id, kind=row.kind),
                edge=row.kind is ArchiveKind.GRAPH_RELATIONSHIP,
            )
        ]
        for endpoint in dict.fromkeys(row.endpoint_ids):
            if row.kind is ArchiveKind.GRAPH_ENTITY and endpoint == row.destination_id:
                continue
            witnesses.append(
                witness(endpoint, await cut(native.client, plan.organization_id, endpoint))
            )
        changes[row.original_id] = {
            "disposition": ArchiveDisposition.SKIPPED,
            "witnesses": tuple(witnesses),
        }
    mixed = prepared(parsed, change_rows(plan, changes))
    _, _, tx = phase(mixed, url=native.url)
    edge = next(row for row in plan.rows if row.kind is ArchiveKind.GRAPH_RELATIONSHIP)
    await native.client.execute_query(
        "UPDATE relates_to SET attributes.changed_after_check=true WHERE uuid=$id;",
        id=edge.destination_id,
    )
    with pytest.raises(SurrealError, match="witness changed"):
        await native.client.execute_query(tx.query, **tx.parameters)
    assert (
        await native.client.execute_query(
            "SELECT * FROM entity WHERE uuid=$id;", id=new.destination_id
        )
        == []
    )
    await no_receipt(native, mixed)


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("history", ["foreign_uuid", "retained_tombstone", "prior_receipt"])
async def test_graph_compiler_native_new_identity_requires_complete_native_absence(
    native, tmp_path, history
):
    parsed, plan = inputs(tmp_path, count=1)
    value = prepared(parsed, plan)
    item = graph_rows(value)[0]
    identity = item.row.destination_id
    _, _, tx = phase(value, url=native.url)
    if history == "foreign_uuid":
        entity = Entity.model_validate(item.body)
        await native.client.execute_query(
            "CREATE entity CONTENT $record;", record=_entity_record(entity, group_id=str(uuid4()))
        )
    else:
        initial_key, initial_token, initial = phase(value, url=native.url)
        await native.client.execute_query(initial.query, **initial.parameters)
        if history == "retained_tombstone":
            await native.client.execute_query("DELETE entity WHERE uuid=$id;", id=identity)
        else:
            receipt = await read_archive_phase_receipt(
                native.client.execute_query, key=initial_key, token=initial_token
            )
            rollback = prepare_archive_phase_transaction(
                key=initial_key.model_copy(update={"action": "rollback"}),
                url=native.url,
                expected_revision=1,
                expected_token=initial_token,
                rollback_token=str(uuid4()),
                terminal=True,
                retirement_candidates=receipt.introduced,
                writer_statements="DELETE entity WHERE uuid=$retire; LET $sibyl_archive_phase_outcomes=[{kind:'graph_entity',disposition:'retired',destination_id:$retire}];",
                writer_parameters={"retire": identity},
            )
            await native.client.execute_query(rollback.query, **rollback.parameters)
            await native.client.execute_query(
                "DELETE source_states WHERE organization_id=$org AND source_id=$id;",
                org=plan.organization_id,
                id=identity,
            )
        value = prepared(parsed, plan)
        _, _, tx = phase(value, url=native.url)
    with pytest.raises(
        SurrealError, match=r"witness changed|not absent|retired|introduced|already exists"
    ):
        await native.client.execute_query(tx.query, **tx.parameters)
    await no_receipt(native, value)
    assert (
        await native.client.execute_query(
            "SELECT * FROM entity WHERE uuid=$id AND group_id=$org;",
            id=identity,
            org=plan.organization_id,
        )
        == []
    )


async def execute_once(client, query, params):
    # Exercise the actual first COMMIT without the ordinary client's retry owner
    # turning a losing canonical transaction into a later independent write.
    slot = await client._available.get()
    try:
        socket = await slot.connect(attempt=1)
        response = await client._send_query(socket, query, params=params, raw=True)
        return _checked_query_result(response)
    finally:
        client._available.put_nowait(slot)


def native_conflicts(trace):
    return [
        item
        for event in trace
        if isinstance(event["response"], dict)
        for item in event["response"].get("result", [])
        if isinstance(item, dict)
        and item.get("status") == "ERR"
        and "Transaction write conflict" in str(item.get("result"))
    ]


def delay_before(client, marker):
    original = client._send_query

    async def delayed(socket, query, *, params, raw):
        return await original(
            socket, query.replace(marker, "SLEEP 1s; " + marker), params=params, raw=raw
        )

    client._send_query = delayed
    return original


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("winner", ["canonical", "phase"])
@pytest.mark.parametrize(
    "mutation",
    [
        "node_edit",
        "node_delete",
        "edge_edit",
        "edge_delete",
        "association_create",
        "association_update",
        "association_delete",
        "rebind_old",
        "rebind_new",
        "first_create",
        "same_admission",
        "unrelated_edge",
    ],
)
async def test_graph_compiler_native_actual_competing_transactions(
    native, tmp_path, winner, mutation
):
    if is_embedded_surreal_url(native.url):
        pytest.skip("actual optimistic conflict proof requires independent native sockets")
    parsed, plan = inputs(tmp_path)
    initial = prepared(parsed, plan)
    node = next(x for x in graph_rows(initial) if x.row.kind is ArchiveKind.GRAPH_ENTITY)
    edge = next(x for x in graph_rows(initial) if x.row.kind is ArchiveKind.GRAPH_RELATIONSHIP)
    org, identity = plan.organization_id, node.row.destination_id
    if mutation not in {"first_create", "same_admission"}:
        installed = phase(initial, url=native.url)[2]
        await native.client.execute_query(installed.query, **installed.parameters)
    other = str(uuid4())
    association = {}
    if mutation.startswith("association") or mutation.startswith("rebind"):
        if mutation.startswith("rebind"):
            entity = Entity.model_validate({**node.body, "id": other})
            await native.client.execute_query(
                "CREATE entity CONTENT $record;", record=_entity_record(entity, group_id=org)
            )
        target = other if mutation == "rebind_new" else identity
        if mutation != "association_create":
            association = await publish(native.client, org, target)
        else:
            association = {
                "organization_id": org,
                "target_kind": "graph_entity",
                "target_id": identity,
                "body_sha256": "a" * 64,
                "principal_id": str(uuid4()),
                "authority_ceiling": {},
                "observations": [],
                "active": True,
            }
    if mutation in {"first_create", "same_admission"}:
        value = initial
    else:
        plan, value = await checked_after(
            native,
            parsed,
            plan,
            disposition=ArchiveDisposition.CONFLICTED
            if mutation.startswith(("association", "rebind"))
            else ArchiveDisposition.SKIPPED,
        )
    key, token, tx = phase(value, url=native.url)
    operations = {
        "node_delete": "DELETE entity WHERE uuid=$id;",
        "edge_edit": "UPDATE relates_to SET attributes.canonical_race=type::string(rand::uuid()) WHERE uuid=$edge;",
        "edge_delete": "DELETE relates_to WHERE uuid=$edge;",
        "association_create": "CREATE memory_derivations CONTENT $association;",
        "association_update": "UPDATE memory_derivations SET active=false WHERE organization_id=$org AND target_id=$id;",
        "association_delete": "DELETE memory_derivations WHERE organization_id=$org AND target_id=$id;",
        "rebind_old": "UPDATE memory_derivations SET target_id=$other WHERE organization_id=$org AND target_id=$id;",
        "rebind_new": "UPDATE memory_derivations SET target_id=$id WHERE organization_id=$org AND target_id=$other;",
        "first_create": "CREATE entity CONTENT $record;",
        "unrelated_edge": "LET $old=(SELECT * FROM relates_to WHERE uuid=$edge)[0]; LET $source=$old.in; LET $target=$old.out; RELATE $source->relates_to->$target CONTENT object::from_entries(array::concat(object::entries($old).filter(|$entry| $entry[0] NOT IN ['id','in','out','uuid']), [['uuid',$other]]));",
    }
    if mutation == "same_admission":
        operation = tx.query
        params = tx.parameters
    elif mutation == "node_edit":
        actual = (await cut(native.client, org, identity))["row"]
        entity = entity_from_surreal_row(actual).model_copy(
            update={"summary": "canonical ordinary writer race"}
        )
        operation = (
            "BEGIN TRANSACTION;"
            + render_surreal_compatible_sql(_ENTITY_BULK_UPSERT_QUERY, url=native.url)
            + "COMMIT TRANSACTION;"
        )
        params = {"rows": [_entity_record(entity, group_id=org)]}
    else:
        operation = "BEGIN TRANSACTION;" + operations[mutation] + "COMMIT TRANSACTION;"
        params = {
            "org": org,
            "id": identity,
            "edge": edge.row.destination_id,
            "other": other,
            "record": _entity_record(Entity.model_validate(node.body), group_id=org),
            "association": association,
        }
    before = (
        await cut(native.client, org, identity)
        if mutation not in {"first_create", "same_admission"}
        else None
    )
    before_cuts = [
        await cut(native.client, org, item.row.destination_id, kind=item.row.kind)
        for item in graph_rows(value)
    ]
    start = len(native.trace)
    delayed = native.client if winner == "canonical" else native.writer
    marker = (
        "LET $archive_graph_cut"
        if mutation in {"first_create", "same_admission"}
        else "LET $source_states_to_fence"
    )
    original = delay_before(delayed, marker if winner == "canonical" else "COMMIT TRANSACTION;")

    async def apply():
        return await execute_once(native.client, tx.query, tx.parameters)

    async def canonical():
        return await execute_once(native.writer, operation, params)

    try:
        pending = asyncio.create_task(apply() if winner == "canonical" else canonical())
        await asyncio.sleep(0.25)
        assert not pending.done(), (
            "delayed transaction exited before the competing transaction began"
        )
        immediate = await asyncio.gather(
            canonical() if winner == "canonical" else apply(), return_exceptions=True
        )
        assert not isinstance(immediate[0], Exception), str(immediate[0])
        assert not pending.done(), "transactions did not overlap"
        result = await asyncio.gather(pending, return_exceptions=True)
    finally:
        delayed._send_query = original
    trace = native.trace[start:]
    attempts = [
        event for event in trace if event["query"].lstrip().startswith("BEGIN TRANSACTION;")
    ]
    assert len(attempts) == 2 and {
        role: sum(event["role"] == role for event in attempts)
        for role in ("phase", "canonical_writer")
    } == {"phase": 1, "canonical_writer": 1}
    sockets = {
        role: {event["socket"] for event in trace if event["role"] == role}
        for role in ("phase", "canonical_writer")
    }
    assert (
        sockets["phase"]
        and sockets["canonical_writer"]
        and sockets["phase"].isdisjoint(sockets["canonical_writer"])
    )
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    if mutation == "unrelated_edge":
        assert not isinstance(result[0], Exception), str(result[0])
        assert not native_conflicts(trace) and receipt is not None
        assert before == await cut(native.client, org, identity)
    else:
        assert isinstance(result[0], SurrealError), str(result[0])
        assert native_conflicts(trace), str(result[0])
        if mutation == "same_admission":
            assert receipt is not None and len(receipt.introduced) == 3
        else:
            assert (receipt is None) == (winner == "canonical")
            if winner == "phase" and mutation != "first_create":
                assert before_cuts == [
                    await cut(native.client, org, item.row.destination_id, kind=item.row.kind)
                    for item in graph_rows(value)
                ]
            if winner == "canonical":
                await no_receipt(native, value)
                with pytest.raises(SurrealError, match=r"witness changed|not absent|endpoint"):
                    await execute_once(native.client, tx.query, tx.parameters)
                await no_receipt(native, value)
    observe(
        {
            "case": "actual_competing_transactions",
            "mutation": mutation,
            "winner": winner,
            "sockets": {key: sorted(values) for key, values in sockets.items()},
            "native_conflicts": native_conflicts(trace),
            "first_attempts": len(attempts),
            "before_cuts": before_cuts,
            "after_cuts": [
                await cut(native.client, org, item.row.destination_id, kind=item.row.kind)
                for item in graph_rows(value)
            ],
            "receipt": None if receipt is None else receipt.model_dump(mode="json"),
        }
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_graph_compiler_native_bodyless_anchor_aliases_count_logically_guard_physically(
    native, tmp_path
):
    parsed, plan = inputs(tmp_path / "existing", count=1)
    installed = prepared(parsed, plan)
    tx = phase(installed, url=native.url)[2]
    await native.client.execute_query(tx.query, **tx.parameters)
    destination = graph_rows(installed)[0].row.destination_id
    await native.client.execute_query(
        "UPDATE entity SET entity_type='project' WHERE uuid=$id;", id=destination
    )
    snapshot = await cut(native.client, plan.organization_id, destination)
    source_org, owner = str(uuid4()), str(uuid4())
    records = [fixtures._entity(source_org, owner, entity_type="project") for _ in range(2)]
    records[0]["name"] = "first foreign project body"
    records[1]["name"] = "second foreign project body"
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
        ).model_copy(update={"witnesses": (witness(destination, snapshot),)})
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
    value = prepared(archive, aliases)
    assert len(graph_rows(value)) == 2 and all(item.body is None for item in graph_rows(value))
    key, token, tx = phase(value, url=native.url)
    assert len(tx.parameters["archive_graph_guards"]) == 1
    await native.client.execute_query(tx.query, **tx.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    assert sum(x.skipped for x in receipt.counts) == 2 and not receipt.introduced
    assert snapshot == await cut(native.client, plan.organization_id, destination)
    divergent = change_rows(
        aliases,
        {
            graph_rows(value)[-1].row.original_id: {
                "witnesses": (witness(destination, {**snapshot, "row_sha": "b" * 64}),)
            }
        },
    )
    with pytest.raises(ValueError, match=r"witnesses disagree|consistent mapped anchors"):
        phase(prepared(archive, divergent), url=native.url)


@pytest.mark.asyncio(loop_scope="module")
async def test_graph_compiler_native_neutral_lifecycle_and_nested_json_roundtrip(native, tmp_path):
    parsed, plan = inputs(
        tmp_path, metadata={"lifecycle_flags": ["helpful"], "metadata": {}, "episodes": []}
    )
    initial = prepared(parsed, plan)
    tx = phase(initial, url=native.url)[2]
    await native.client.execute_query(tx.query, **tx.parameters)
    _, value = await checked_after(native, parsed, plan)
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(tx.query, **tx.parameters)
    assert (
        sum(
            x.skipped
            for x in (
                await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
            ).counts
        )
        == 3
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_graph_compiler_native_tombstone_conflict_retains_ledger_without_adoption(
    native, tmp_path
):
    parsed, plan = inputs(tmp_path, count=1)
    tx = phase(prepared(parsed, plan), url=native.url)[2]
    await native.client.execute_query(tx.query, **tx.parameters)
    identity = next(row.destination_id for row in plan.rows if row.kind is ArchiveKind.GRAPH_ENTITY)
    await native.client.execute_query("DELETE entity WHERE uuid=$id;", id=identity)
    plan, value = await checked_after(
        native, parsed, plan, disposition=ArchiveDisposition.CONFLICTED
    )
    before = await cut(native.client, plan.organization_id, identity)
    assert before["row"] is None and before["state"]["deleted"] is True
    key, token, tx = phase(value, url=native.url)
    await native.client.execute_query(tx.query, **tx.parameters)
    receipt = await read_archive_phase_receipt(native.client.execute_query, key=key, token=token)
    assert sum(item.conflicted for item in receipt.counts) == 1 and not receipt.introduced
    assert before == await cut(native.client, plan.organization_id, identity)
