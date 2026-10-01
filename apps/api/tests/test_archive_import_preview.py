from __future__ import annotations

import base64
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from sibyl.api.routes import archive_import_preview as preview, memory_auth
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.context import AuthContext
from sibyl.persistence.surreal import auth_runtime
from sibyl_core.auth import OrganizationRole
from sibyl_core.auth.memory_policy import stamp_memory_scope_metadata
from sibyl_core.auth.models import AuthOrganization, AuthUser
from sibyl_core.backends.surreal import SurrealAuthClient, SurrealContentClient
from sibyl_core.backends.surreal.auth_schema import bootstrap_auth_schema
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive import build_manifest, write_archive
from sibyl_core.migrate.personal_archive_intake import ArchiveIntakeBudget, parse_personal_archive
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveMappings,
    CheckedArchivePlan,
    preview_counts,
)
from sibyl_core.migrate.source_integrity import build_integrity_archive
from sibyl_core.models import Entity, Relationship, RelationshipType, Task
from sibyl_core.services.content_models import RawMemory, raw_memory_record
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.graph_relationships import RelationshipManager


def _request():
    return Request(
        {"type": "http", "method": "POST", "path": "/archive-imports/check", "headers": []}
    )


def _budget():
    return ArchiveIntakeBudget(
        compressed_bytes=100_000,
        inflated_bytes=1_000_000,
        member_bytes=500_000,
        members=16,
        json_depth=32,
        json_scalar_bytes=100_000,
        json_nodes=50_000,
        parsed_rows=10_000,
        encoded_artifact_bytes=2_000_000,
        encoded_plan_bytes=1_000_000,
        metadata_transaction_bytes=3_000_000,
    )


def _archive(tmp_path, *, actor_id, edge=False, edge_carriers=False, project_anchor_id=None):
    org, owner = str(uuid4()), str(uuid4())
    now = datetime(2026, 9, 30, tzinfo=UTC)
    raw = raw_memory_record(
        RawMemory(
            id=str(uuid4()),
            organization_id=org,
            source_id="declared-source",
            principal_id=owner,
            title="Source raw",
            raw_content="Source body",
            created_at=now,
            captured_at=now,
        )
    )
    nodes = [
        {
            "uuid": str(uuid4()),
            "group_id": org,
            "entity_type": "topic",
            "name": name,
            "description": "Source description",
            "content": "Source graph body",
            "attributes": {"memory_scope": "private", "principal_id": owner},
            "revision": 1,
            "derivation_required": False,
            "created_at": now,
            "updated_at": now,
        }
        for name in ("Source topic", "Source target")
    ]

    if project_anchor_id is not None:
        nodes[0]["uuid"] = project_anchor_id
        nodes[0]["entity_type"] = "project"
        for node in nodes:
            node["attributes"] = {
                "memory_scope": "project",
                "scope_key": project_anchor_id,
                "project_id": project_anchor_id,
            }

    def state(row, kind):
        return {
            "organization_id": org,
            "source_kind": kind.value,
            "source_id": row["uuid"],
            "generation": 1,
            "revision": 1,
            "deleted": False,
            "incarnation": str(uuid4()),
        }

    relationships = (
        []
        if not edge
        else [
            {
                "id": str(uuid4()),
                "relationship_type": "RELATED_TO",
                "source_id": nodes[0]["uuid"],
                "target_id": nodes[1]["uuid"],
                "weight": 1.0,
                "metadata": {},
                "created_at": now.isoformat(),
            }
        ]
    )
    if edge_carriers:
        relationships[0]["metadata"] = {
            "fact": f"{nodes[0]['uuid']} related_to {nodes[1]['uuid']}",
            "source_id": nodes[0]["uuid"],
            "episodes": [],
        }
    graph = {
        "version": "3.0",
        "organization_id": org,
        "entities": [entity_from_surreal_row(row).model_dump(mode="json") for row in nodes],
        "entity_count": len(nodes),
        "relationships": relationships,
        "relationship_count": len(relationships),
        "source_integrity": build_integrity_archive(
            kind=SourceKind.GRAPH_ENTITY,
            organizations=[org],
            source_rows=nodes,
            source_states=[state(row, SourceKind.GRAPH_ENTITY) for row in nodes],
            derivations=[],
        ),
    }
    content = {
        "version": "2.0",
        "organization_id": org,
        "tables": {"raw_captures": [raw]},
        "row_counts": {"raw_captures": 1},
        "total_rows": 1,
        "source_integrity": build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            source_rows=[raw],
            source_states=[state(raw, SourceKind.RAW_CAPTURE)],
            derivations=[],
        ),
    }
    files = {
        name: json.dumps(value, default=lambda v: v.isoformat()).encode()
        for name, value in {"graph.json": graph, "content.json": content}.items()
    }
    path = tmp_path / "source.tgz"
    write_archive(
        path,
        manifest=build_manifest(organization_id=org, source_store="surreal", files=files),
        files=files,
    )
    mappings = ArchiveMappings(
        source_private_owner_id=owner,
        quarantine=ArchiveAudience(memory_scope="private", scope_key=actor_id),
    )
    return parse_personal_archive(path, _budget()), mappings


@pytest.fixture
async def destination(monkeypatch):
    context = AuthContext(
        user=AuthUser(id=uuid4()),
        organization=AuthOrganization(id=uuid4()),
        org_role=OrganizationRole.MEMBER,
    )
    url = os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://")
    options = {
        "url": url,
        "username": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        "password": os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    }
    content = SurrealContentClient(namespace="archive_preview_" + uuid4().hex, **options)
    graph = SurrealGraphClient(group_id=context.organization_id, **options)
    await bootstrap_content_schema(content)
    await bootstrap_schema(graph)
    calls = []
    content_execute, graph_execute = content.execute_query, graph.execute_query

    async def content_query(query, **params):
        calls.append(("content", query, params))
        return await content_execute(query, **params)

    async def graph_query(query, **params):
        calls.append(("graph", query, params))
        return await graph_execute(query, **params)

    content.execute_query = content_query
    graph.execute_query = graph_query

    @asynccontextmanager
    async def content_scope():
        yield content

    async def graph_client(organization_id):
        assert organization_id == context.organization_id
        return graph

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(preview, "surreal_content_client", content_scope)
    monkeypatch.setattr(preview, "get_surreal_graph_client", graph_client)
    monkeypatch.setattr(memory_auth, "log_memory_audit_event", no_audit)
    try:
        yield context, content, graph, calls
    finally:
        await content.close()
        await graph.close()


async def _build(parsed, mappings, context):
    return await preview.build_archive_preview(
        parsed=parsed,
        mappings=mappings,
        context=context,
        request=_request(),
    )


def _row(rows, kind):
    return next(row for row in rows if row.kind is kind)


async def _insert_current(destination, row, *, kind=None, owner=None, name="Source topic"):
    context, content, graph, _ = destination
    owner = owner or context.user_id
    metadata = stamp_memory_scope_metadata(
        {},
        memory_scope="private",
        scope_key=owner,
        principal_id=owner,
    )
    if row.kind is ArchiveKind.RAW_CAPTURE:
        record = raw_memory_record(
            RawMemory(
                id=row.destination_id,
                organization_id=context.organization_id,
                source_id=row.destination_id,
                principal_id=owner,
                title="Source raw",
                raw_content="Source body",
                metadata=metadata,
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
        )
        await content.execute_query("CREATE raw_captures CONTENT $record;", record=record)
    else:
        await graph.execute_query(
            "CREATE entity CONTENT $record;",
            record={
                "uuid": row.destination_id,
                "group_id": context.organization_id,
                "entity_type": kind or "topic",
                "name": name,
                "description": "Source description",
                "content": "Source graph body",
                "attributes": metadata,
            },
        )


async def test_archive_preview_native_absence_is_bound_without_active_writes(tmp_path, destination):
    context, content, graph, _ = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    rows = await _build(parsed, mappings, context)
    for kind in (ArchiveKind.RAW_CAPTURE, ArchiveKind.GRAPH_ENTITY):
        row = _row(rows, kind)
        assert row.disposition is ArchiveDisposition.CREATED
        assert row.witnesses[0].row_sha256 is None
        assert row.witnesses[0].state_sha256 is None
    assert _row(rows, ArchiveKind.SOURCE_STATE).disposition is ArchiveDisposition.QUARANTINED
    assert not await content.execute_query("SELECT * FROM raw_captures;")
    assert not await content.execute_query("SELECT * FROM archive_import_runs;")
    assert not await graph.execute_query("SELECT * FROM entity;")
    assert not await graph.execute_query("SELECT * FROM relates_to;")


@pytest.mark.parametrize("kind", [ArchiveKind.RAW_CAPTURE, ArchiveKind.GRAPH_ENTITY])
async def test_archive_preview_native_identical_destination_skips_and_different_body_conflicts(
    tmp_path, destination, kind
):
    context, content, graph, _ = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    initial = await _build(parsed, mappings, context)
    row = _row(initial, kind)
    source_name = (
        next(item["name"] for item in parsed.graph["entities"] if item["id"] == row.original_id)
        if kind is ArchiveKind.GRAPH_ENTITY
        else "Source topic"
    )
    await _insert_current(destination, row, name=source_name)
    checked = _row(await _build(parsed, mappings, context), kind)
    assert checked.disposition is ArchiveDisposition.SKIPPED
    assert checked.reason == "destination_canonical_identical"
    assert checked.witnesses[0].row_sha256 is not None
    assert checked.witnesses[0].state_sha256 is not None
    if kind is ArchiveKind.GRAPH_ENTITY:
        await graph.execute_query(
            "UPDATE entity SET retrieval_count=19,citation_count=7,misled_count=2 "
            "WHERE uuid=$identity;",
            identity=row.destination_id,
        )
        used = _row(await _build(parsed, mappings, context), kind)
        assert used.disposition is ArchiveDisposition.SKIPPED
        assert used.semantic_sha256 == checked.semantic_sha256
        assert used.witnesses[0].row_sha256 != checked.witnesses[0].row_sha256
    client, query = (
        (
            content,
            "UPDATE raw_captures SET raw_content='Different authorized body' WHERE uuid=$identity;",
        )
        if kind is ArchiveKind.RAW_CAPTURE
        else (graph, "UPDATE entity SET content='Different authorized body' WHERE uuid=$identity;")
    )
    await client.execute_query(query, identity=row.destination_id)
    conflicted = _row(await _build(parsed, mappings, context), kind)
    assert conflicted.disposition is ArchiveDisposition.CONFLICTED
    assert conflicted.reason == "destination_body_conflict"
    assert conflicted.witnesses[0].row_sha256 != checked.witnesses[0].row_sha256


@pytest.mark.parametrize("kind", [ArchiveKind.RAW_CAPTURE, ArchiveKind.GRAPH_ENTITY])
async def test_archive_preview_native_current_foreign_private_owner_is_opaque_before_counts(
    tmp_path, destination, kind
):
    context, _, _, _ = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    row = _row(await _build(parsed, mappings, context), kind)
    await _insert_current(destination, row, owner=str(uuid4()))
    with pytest.raises(HTTPException) as error:
        await _build(parsed, mappings, context)
    assert error.value.status_code == 403
    assert error.value.detail == "archive_destination_unavailable"


@pytest.mark.parametrize("kind", [ArchiveKind.RAW_CAPTURE, ArchiveKind.GRAPH_ENTITY])
async def test_archive_preview_native_orphan_tombstone_is_opaque(tmp_path, destination, kind):
    context, content, graph, _ = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    row = _row(await _build(parsed, mappings, context), kind)
    await _insert_current(destination, row)
    client, query = (
        (content, "DELETE raw_captures WHERE uuid=$identity;")
        if kind is ArchiveKind.RAW_CAPTURE
        else (graph, "DELETE entity WHERE uuid=$identity;")
    )
    await client.execute_query(query, identity=row.destination_id)
    with pytest.raises(HTTPException) as error:
        await _build(parsed, mappings, context)
    assert error.value.detail == "archive_destination_unavailable"


async def test_archive_preview_restricted_empty_key_denies_before_destination_reads(
    tmp_path, destination
):
    context, _, _, calls = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    restricted = replace(context, api_key_id=str(uuid4()), api_key_memory_scope_keys=frozenset())
    calls.clear()
    with pytest.raises(HTTPException) as error:
        await _build(parsed, mappings, restricted)
    assert error.value.status_code == 403
    assert calls == []


async def test_archive_preview_native_relationship_checks_planned_endpoint_dependencies(
    tmp_path, destination
):
    context, _, graph, _ = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id, edge=True)
    initial = await _build(parsed, mappings, context)
    edge = _row(initial, ArchiveKind.GRAPH_RELATIONSHIP)
    assert edge.disposition is ArchiveDisposition.CREATED
    assert len(edge.endpoint_ids) == 2
    assert len(edge.witnesses) == 3
    first = next(row for row in initial if row.destination_id == edge.endpoint_ids[0])
    await _insert_current(destination, first)
    await graph.execute_query(
        "UPDATE entity SET content='Conflicting endpoint' WHERE uuid=$identity;",
        identity=first.destination_id,
    )
    checked = _row(await _build(parsed, mappings, context), ArchiveKind.GRAPH_RELATIONSHIP)
    assert checked.disposition is ArchiveDisposition.QUARANTINED
    assert checked.reason == "dependent_destination_conflict"


async def test_archive_preview_native_witness_preserves_nanoseconds_and_ignores_unrelated_rows(
    tmp_path, destination
):
    if not os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL"):
        pytest.skip("native datetime precision requires the server transport")
    context, content, _, _ = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    row = _row(await _build(parsed, mappings, context), ArchiveKind.RAW_CAPTURE)
    await _insert_current(destination, row)
    await content.execute_query(
        "UPDATE raw_captures SET created_at=<datetime>'2026-09-30T00:00:00.000000111Z' WHERE uuid=$identity;",
        identity=row.destination_id,
    )
    first = _row(await _build(parsed, mappings, context), ArchiveKind.RAW_CAPTURE)
    await content.execute_query(
        "UPDATE raw_captures SET created_at=<datetime>'2026-09-30T00:00:00.000000222Z' WHERE uuid=$identity;",
        identity=row.destination_id,
    )
    second = _row(await _build(parsed, mappings, context), ArchiveKind.RAW_CAPTURE)
    assert first.disposition is ArchiveDisposition.SKIPPED
    assert second.disposition is ArchiveDisposition.SKIPPED
    assert first.witnesses[0].row_sha256 != second.witnesses[0].row_sha256
    other = row.model_copy(update={"destination_id": str(uuid4())})
    await _insert_current(destination, other)
    third = _row(await _build(parsed, mappings, context), ArchiveKind.RAW_CAPTURE)
    assert third.witnesses == second.witnesses


async def test_archive_preview_post_replay_rechecks_actor_and_current_memory_ceiling(
    tmp_path, destination
):
    context, _, _, calls = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    rows = await _build(parsed, mappings, context)
    plan = CheckedArchivePlan(
        organization_id=context.organization_id,
        actor_id=context.user_id,
        archive_sha256=parsed.archive_sha256,
        artifact_sha256=parsed.artifact_sha256,
        origin=parsed.origin,
        mappings=mappings,
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=rows,
        counts=preview_counts(rows),
    )
    await preview.authorize_archive_plan(plan=plan, context=context, request=_request())
    calls.clear()
    restricted = replace(context, api_key_id=str(uuid4()), api_key_memory_scope_keys=frozenset())
    with pytest.raises(HTTPException):
        await preview.authorize_archive_plan(plan=plan, context=restricted, request=_request())
    assert calls == []
    other = replace(context, user=AuthUser(id=uuid4()))
    with pytest.raises(HTTPException):
        await preview.authorize_archive_plan(plan=plan, context=other, request=_request())
    assert calls == []


@pytest.mark.parametrize("kind", [ArchiveKind.RAW_CAPTURE, ArchiveKind.GRAPH_ENTITY])
async def test_archive_preview_native_current_protection_cannot_be_canonical_skip(
    tmp_path, destination, kind
):
    context, content, graph, _ = destination
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    row = _row(await _build(parsed, mappings, context), kind)
    await _insert_current(destination, row)
    client, query = (
        (content, "UPDATE raw_captures SET derivation_required=true WHERE uuid=$identity;")
        if kind is ArchiveKind.RAW_CAPTURE
        else (graph, "UPDATE entity SET derivation_required=true WHERE uuid=$identity;")
    )
    await client.execute_query(query, identity=row.destination_id)
    checked = _row(await _build(parsed, mappings, context), kind)
    assert checked.disposition is ArchiveDisposition.CONFLICTED
    assert checked.reason == "destination_protected_or_retired"


async def _insert_current_edge(destination, edge, source, target):
    context, _, graph, _ = destination
    await RelationshipManager(graph, group_id=context.organization_id).create(
        Relationship(
            id=edge.destination_id,
            relationship_type=RelationshipType.RELATED_TO,
            source_id=source,
            target_id=target,
            weight=1.0,
            metadata=stamp_memory_scope_metadata(
                {},
                memory_scope="private",
                scope_key=context.user_id,
                principal_id=context.user_id,
            ),
        )
    )


@pytest.mark.parametrize("foreign_endpoint", [False, True])
@pytest.mark.parametrize("edge_carriers", [False, True])
async def test_archive_preview_native_existing_edge_authorizes_actual_endpoints_before_counts(
    tmp_path, destination, foreign_endpoint, edge_carriers
):
    context, _, _, _ = destination
    parsed, mappings = _archive(
        tmp_path, actor_id=context.user_id, edge=True, edge_carriers=edge_carriers
    )
    initial = await _build(parsed, mappings, context)
    edge = _row(initial, ArchiveKind.GRAPH_RELATIONSHIP)
    for node in (row for row in initial if row.kind is ArchiveKind.GRAPH_ENTITY):
        source_name = next(
            item["name"] for item in parsed.graph["entities"] if item["id"] == node.original_id
        )
        await _insert_current(destination, node, name=source_name)
    source, target = edge.endpoint_ids
    if foreign_endpoint:
        foreign = _row(initial, ArchiveKind.GRAPH_ENTITY).model_copy(
            update={"destination_id": str(uuid4())}
        )
        await _insert_current(destination, foreign, owner=str(uuid4()))
        target = foreign.destination_id
    await _insert_current_edge(destination, edge, source, target)
    if foreign_endpoint:
        with pytest.raises(HTTPException) as error:
            await _build(parsed, mappings, context)
        assert error.value.detail == "archive_destination_unavailable"
    else:
        checked = _row(await _build(parsed, mappings, context), ArchiveKind.GRAPH_RELATIONSHIP)
        assert checked.disposition is ArchiveDisposition.SKIPPED
        assert checked.reason == "destination_canonical_identical"
        _, _, graph, _ = destination
        await graph.execute_query(
            "UPDATE relates_to SET fact='Different authorized fact' WHERE uuid=$identity;",
            identity=edge.destination_id,
        )
        changed = _row(await _build(parsed, mappings, context), ArchiveKind.GRAPH_RELATIONSHIP)
        assert changed.disposition is ArchiveDisposition.CONFLICTED
        assert changed.reason == "destination_body_conflict"
        await graph.execute_query(
            "UPDATE relates_to SET fact=$fact, episodes=['retained-episode'] WHERE uuid=$identity;",
            fact=f"{source} related_to {target}",
            identity=edge.destination_id,
        )
        changed = _row(await _build(parsed, mappings, context), ArchiveKind.GRAPH_RELATIONSHIP)
        assert changed.disposition is ArchiveDisposition.CONFLICTED
        assert changed.reason == "destination_body_conflict"


@pytest.fixture
async def destination_access(destination, monkeypatch):
    context, _, _, _ = destination
    auth = SurrealAuthClient(
        namespace="archive_preview_auth_" + uuid4().hex,
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    )
    await bootstrap_auth_schema(auth)

    @asynccontextmanager
    async def scope():
        yield auth

    monkeypatch.setattr(auth_runtime, "_auth_client_scope", scope)
    project_id, project_graph_id, team_id = str(uuid4()), str(uuid4()), str(uuid4())
    await auth.execute_query(
        "CREATE users CONTENT $user; CREATE organizations CONTENT $org; "
        "CREATE organization_members CONTENT $membership; "
        "CREATE projects CONTENT $project; CREATE project_members CONTENT $project_member; "
        "CREATE teams CONTENT $team; CREATE team_members CONTENT $team_member;",
        user={"uuid": context.user_id, "email": "preview@example.test", "name": "Preview actor"},
        org={"uuid": context.organization_id, "name": "Preview org", "slug": "preview-org"},
        membership={
            "uuid": str(uuid4()),
            "organization_id": context.organization_id,
            "user_id": context.user_id,
            "role": "member",
        },
        project={
            "uuid": project_id,
            "organization_id": context.organization_id,
            "name": "Mapped project",
            "slug": "mapped-project",
            "graph_project_id": project_graph_id,
            "visibility": "private",
        },
        project_member={
            "uuid": str(uuid4()),
            "organization_id": context.organization_id,
            "project_id": project_id,
            "user_id": context.user_id,
            "role": "project_contributor",
        },
        team={
            "uuid": team_id,
            "organization_id": context.organization_id,
            "name": "Mapped team",
            "slug": "mapped-team",
        },
        team_member={
            "uuid": str(uuid4()),
            "team_id": team_id,
            "user_id": context.user_id,
            "role": "member",
        },
    )
    try:
        yield auth, project_graph_id, team_id
    finally:
        await auth.close()


@pytest.mark.parametrize("protection", [None, "hidden", "derivation_required"])
async def test_archive_preview_native_mapped_anchor_requires_retained_eligible_row(
    tmp_path, destination, destination_access, protection
):
    context, _, graph, calls = destination
    auth, project_id, _ = destination_access
    original = str(uuid4())
    parsed, mappings = _archive(
        tmp_path, actor_id=context.user_id, edge=True, project_anchor_id=original
    )
    mappings = mappings.model_copy(update={"projects": {original: project_id}})
    await graph.execute_query(
        "CREATE entity CONTENT $row;",
        row={
            "uuid": project_id,
            "group_id": context.organization_id,
            "entity_type": "project",
            "name": "Destination project",
            "attributes": {},
        },
    )
    initial = await _build(parsed, mappings, context)
    anchor = next(row for row in initial if row.destination_id == project_id)
    assert anchor.disposition is ArchiveDisposition.SKIPPED
    assert _row(initial, ArchiveKind.GRAPH_RELATIONSHIP).disposition is ArchiveDisposition.CREATED
    if protection:
        query = (
            "UPDATE entity SET attributes.lifecycle_flags=['hidden'] WHERE uuid=$identity;"
            if protection == "hidden"
            else "UPDATE entity SET derivation_required=true WHERE uuid=$identity;"
        )
        await graph.execute_query(query, identity=project_id)
        checked = await _build(parsed, mappings, context)
        anchor = next(row for row in checked if row.destination_id == project_id)
        assert anchor.disposition is ArchiveDisposition.CONFLICTED
        assert anchor.reason == "destination_protected_or_retired"
        assert (
            _row(checked, ArchiveKind.GRAPH_RELATIONSHIP).disposition
            is ArchiveDisposition.QUARANTINED
        )
    else:
        calls.clear()
        restricted = replace(
            context,
            api_key_id=str(uuid4()),
            api_key_project_ids=frozenset(),
        )
        with pytest.raises(HTTPException) as denied:
            await _build(parsed, mappings, restricted)
        assert denied.value.detail == "archive_destination_unavailable"
        assert calls == []
        restricted = replace(
            context,
            api_key_id=str(uuid4()),
            api_key_memory_scope_keys=frozenset(
                {api_key_memory_scope_key("private", context.user_id)}
            ),
        )
        with pytest.raises(HTTPException):
            await _build(parsed, mappings, restricted)
        assert calls == []
        await auth.execute_query("UPDATE project_members SET role='project_viewer';")
        with pytest.raises(HTTPException):
            await _build(parsed, mappings, context)
        assert calls == []


async def test_archive_preview_native_team_mapping_rechecks_current_membership(
    tmp_path, destination, destination_access
):
    context, _, _, calls = destination
    auth, _, team_id = destination_access
    parsed, mappings = _archive(tmp_path, actor_id=context.user_id)
    mappings = mappings.model_copy(update={"teams": {str(uuid4()): team_id}})
    await _build(parsed, mappings, context)
    await auth.execute_query("DELETE team_members WHERE team_id=$identity;", identity=team_id)
    calls.clear()
    with pytest.raises(HTTPException) as denied:
        await _build(parsed, mappings, context)
    assert denied.value.detail == "archive_destination_unavailable"
    assert calls == []


async def _written_task_archive(tmp_path, destination, *, marker=None, unknown=False, size=3):
    context, _, _, _ = destination
    organization, owner = str(uuid4()), str(uuid4())
    source = SurrealGraphClient(
        group_id=organization,
        url=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_URL", "memory://"),
        username=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_USERNAME", ""),
        password=os.environ.get("SIBYL_ARCHIVE_TEST_SURREAL_PASSWORD", ""),
    )
    await bootstrap_schema(source)
    models = []
    for index in range(size):
        metadata = stamp_memory_scope_metadata(
            {"reviewed_at": datetime(2026, 9, 1, tzinfo=UTC)},
            memory_scope="private",
            scope_key=owner,
            principal_id=owner,
        )
        parent = models[-1].id if models else None
        if unknown and index == 1:
            parent = str(uuid4())
        if marker and index == 0:
            metadata.update(marker)
        models.append(
            Task(
                id=str(uuid4()),
                title=f"Written task {index}",
                description="Ordinary writer source",
                parent_task_id=parent,
                metadata=metadata,
            )
        )
    models.append(
        Task(
            id=str(uuid4()),
            title="Independent task",
            description="Independent task",
            metadata=metadata.copy(),
        )
    )
    try:
        await EntityManager(source, group_id=organization).create_direct_bulk(models)
        rows = await source.execute_query("SELECT * OMIT id FROM entity ORDER BY uuid;")
        states = await source.execute_query(
            "SELECT * OMIT id FROM source_states ORDER BY source_id;"
        )
        payload = {
            "version": "3.0",
            "organization_id": organization,
            "entities": [entity_from_surreal_row(row).model_dump(mode="json") for row in rows],
            "entity_count": len(rows),
            "relationships": [],
            "relationship_count": 0,
            "source_integrity": build_integrity_archive(
                kind=SourceKind.GRAPH_ENTITY,
                organizations=[organization],
                source_rows=rows,
                source_states=states,
                derivations=[],
            ),
        }
        files = {
            "graph.json": json.dumps(payload, default=lambda value: value.isoformat()).encode()
        }
        path = tmp_path / "written-tasks.tgz"
        write_archive(
            path,
            manifest=build_manifest(
                organization_id=organization, source_store="surreal", files=files
            ),
            files=files,
        )
        budget = replace(
            _budget(),
            compressed_bytes=2_000_000,
            inflated_bytes=16_000_000,
            member_bytes=16_000_000,
            json_nodes=1_000_000,
            encoded_artifact_bytes=32_000_000,
        )
        parsed = parse_personal_archive(path, budget)
        mappings = ArchiveMappings(
            source_private_owner_id=owner,
            quarantine=ArchiveAudience(memory_scope="private", scope_key=context.user_id),
        )
        return parsed, mappings, models
    finally:
        try:
            await source.execute_query("REMOVE NAMESPACE " + source.namespace + ";")
        finally:
            await source.close()


def _checked_plan(parsed, mappings, context, rows):
    return CheckedArchivePlan(
        organization_id=context.organization_id,
        actor_id=context.user_id,
        archive_sha256=parsed.archive_sha256,
        artifact_sha256=parsed.artifact_sha256,
        origin=parsed.origin,
        mappings=mappings,
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=rows,
        counts=preview_counts(rows),
    )


@pytest.mark.parametrize(
    "marker",
    [{"origin_execution_id": "foreign-execution"}, {"lifecycle_flags": ["hidden"]}],
)
async def test_archive_preview_native_archived_blocked_parent_quarantines_whole_chain(
    tmp_path, destination, marker
):
    context, _, graph, _ = destination
    parsed, mappings, models = await _written_task_archive(tmp_path, destination, marker=marker)
    rows = await _build(parsed, mappings, context)
    nodes = {row.original_id: row for row in rows if row.kind is ArchiveKind.GRAPH_ENTITY}
    for model in models[:-1]:
        assert nodes[model.id].disposition is ArchiveDisposition.QUARANTINED
    assert nodes[models[-1].id].disposition is ArchiveDisposition.CREATED
    assert not await graph.execute_query("SELECT * FROM entity;")
    await preview.authorize_archive_plan(
        plan=_checked_plan(parsed, mappings, context, rows), context=context, request=_request()
    )


async def test_archive_preview_native_unknown_inline_label_stays_inert(tmp_path, destination):
    context, _, _, _ = destination
    parsed, mappings, models = await _written_task_archive(tmp_path, destination, unknown=True)
    rows = await _build(parsed, mappings, context)
    child = next(row for row in rows if row.original_id == models[1].id)
    assert child.disposition is ArchiveDisposition.CREATED
    assert child.endpoint_ids == ()
    retained = json.loads(base64.b64decode(json.loads(parsed.staged_payload_json)["graph.json"]))
    retained_child = next(row for row in retained["entities"] if row["id"] == models[1].id)
    assert retained_child["metadata"]["parent_task_id"] == models[1].parent_task_id
    await preview.authorize_archive_plan(
        plan=_checked_plan(parsed, mappings, context, rows), context=context, request=_request()
    )


async def test_archive_preview_native_user_timestamp_and_dependency_witness_remain_semantic(
    tmp_path, destination
):
    context, _, graph, calls = destination
    parsed, mappings, models = await _written_task_archive(tmp_path, destination)
    initial = await _build(parsed, mappings, context)
    identities = {
        row.original_id: row.destination_id
        for row in initial
        if row.kind is ArchiveKind.GRAPH_ENTITY
    }
    manager = EntityManager(graph, group_id=context.organization_id)
    for public in parsed.graph["entities"]:
        body = dict(public)
        metadata = stamp_memory_scope_metadata(
            body["metadata"],
            memory_scope="private",
            scope_key=context.user_id,
            principal_id=context.user_id,
        )
        for key in ("epic_id", "parent_task_id", "task_id", "milestone_id"):
            if key in metadata:
                metadata[key] = identities[metadata[key]]
        body.update(id=identities[body["id"]], metadata=metadata)
        await manager.create_direct(Entity.model_validate(body))
    checked = await _build(parsed, mappings, context)
    nodes = {row.original_id: row for row in checked if row.kind is ArchiveKind.GRAPH_ENTITY}
    assert all(row.disposition is ArchiveDisposition.SKIPPED for row in nodes.values())
    parent, child = models[:2]
    parent_identity = "entity:" + identities[parent.id]
    assert parent_identity in {witness.identity for witness in nodes[child.id].witnesses}
    assert nodes[child.id].endpoint_ids == (identities[parent.id],)
    before = nodes[child.id].witnesses
    await graph.execute_query(
        "UPDATE entity SET updated_at=time::now(), attributes.updated_at=time::now() WHERE uuid=$id;",
        id=identities[parent.id],
    )
    checked = await _build(parsed, mappings, context)
    nodes = {row.original_id: row for row in checked if row.kind is ArchiveKind.GRAPH_ENTITY}
    assert nodes[parent.id].disposition is ArchiveDisposition.SKIPPED
    assert nodes[child.id].witnesses != before
    await graph.execute_query(
        "UPDATE entity SET attributes.reviewed_at=$reviewed_at WHERE uuid=$id;",
        id=identities[parent.id],
        reviewed_at="2026-09-02T00:00:00+00:00",
    )
    checked = await _build(parsed, mappings, context)
    nodes = {row.original_id: row for row in checked if row.kind is ArchiveKind.GRAPH_ENTITY}
    assert nodes[parent.id].disposition is ArchiveDisposition.CONFLICTED
    assert all(
        nodes[model.id].disposition is ArchiveDisposition.QUARANTINED for model in models[1:-1]
    )
    assert nodes[models[-1].id].disposition is ArchiveDisposition.SKIPPED
    plan = _checked_plan(parsed, mappings, context, checked)
    forged = nodes[child.id].model_copy(update={"endpoint_ids": (str(uuid4()),)})
    forged_rows = tuple(forged if row.original_id == child.id else row for row in checked)
    calls.clear()
    with pytest.raises(HTTPException):
        await preview.authorize_archive_plan(
            plan=plan.model_copy(update={"rows": forged_rows}),
            context=context,
            request=_request(),
        )
    assert calls == []


async def test_archive_preview_native_deep_inline_chain_preserves_independent_branch(
    tmp_path, destination
):
    context, _, graph, _ = destination
    parsed, mappings, models = await _written_task_archive(
        tmp_path, destination, marker={"origin_execution_id": "foreign-execution"}, size=1000
    )
    rows = await _build(parsed, mappings, context)
    nodes = {row.original_id: row for row in rows if row.kind is ArchiveKind.GRAPH_ENTITY}
    assert all(
        nodes[model.id].disposition is ArchiveDisposition.QUARANTINED for model in models[:-1]
    )
    assert nodes[models[-1].id].disposition is ArchiveDisposition.CREATED
    assert not await graph.execute_query("SELECT * FROM entity;")
