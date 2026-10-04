"""Authenticated entity links reject hidden targets and preserve stored revision."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from sibyl.api.routes import entity_links, entity_policy, entity_reads
from sibyl.api.schemas.entities import EntityLinksRequest, EntityLinksResponse
from sibyl_core.auth import ProjectRole
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_link_writes import (
    EntityLinkConflictError,
    EntityLinkSnapshot,
    EntityLinksResult,
)
from tests.harness.auth import stub_auth_context


@pytest.fixture
def link_route(monkeypatch):
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = stub_auth_context()
    source = EntityLinkSnapshot(
        Entity(
            id="task-source",
            entity_type=EntityType.TASK,
            name="source",
            revision=7,
            metadata={"project_id": "project-one", "status": "done"},
        ),
        "source-physical",
        "source-state",
        "source-sha",
    )
    target = EntityLinkSnapshot(
        Entity(
            id="task-target",
            entity_type=EntityType.TASK,
            name="target",
            revision=2,
        ),
        "target-physical",
        "target-state",
        "target-sha",
    )
    snapshots = {source.entity.id: source, target.entity.id: target}

    async def load(_client, entity_id, **_):
        return snapshots.get(entity_id)

    runtime = SimpleNamespace(client=object(), entity_manager=object())
    monkeypatch.setattr(entity_policy, "get_entity_graph_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(entity_links, "load_entity_link_snapshot", load)
    verify = AsyncMock()
    visible = AsyncMock(return_value=set())
    readable = AsyncMock(return_value=set())
    monkeypatch.setattr(entity_links, "verify_entity_project_access", verify)
    monkeypatch.setattr(entity_policy, "require_entity_scope_visible", visible)
    monkeypatch.setattr(entity_policy, "require_entity_read_access", readable)
    monkeypatch.setattr(
        entity_policy,
        "reader_scope",
        AsyncMock(
            return_value=entity_policy.ReaderScope(
                user_id="owner",
                accessible_projects={"project-one"},
                memory_grants=None,
            )
        ),
    )
    monkeypatch.setattr(entity_policy, "declared_bulk_relationships", AsyncMock(return_value=[]))
    result = EntityLinksResult(
        source.entity.id,
        8,
        ["rel_task-source_depends_on_task-target"],
        [],
        None,
        None,
        ["task-target"],
        False,
    )
    writer = AsyncMock(return_value=result)
    monkeypatch.setattr(entity_links, "add_entity_links_if_revision", writer)
    return SimpleNamespace(
        org=org,
        ctx=ctx,
        source=source,
        target=target,
        snapshots=snapshots,
        verify=verify,
        visible=visible,
        readable=readable,
        writer=writer,
        result=result,
    )


@pytest.mark.asyncio
async def test_link_endpoint_uses_source_contribution_and_target_read_policy(link_route):
    fixture = link_route
    response = await entity_links.add_entity_links(
        fixture.source.entity.id,
        EntityLinksRequest(expected_revision=7, depends_on=[fixture.target.entity.id]),
        org=fixture.org,
        ctx=fixture.ctx,
        content_session=None,
    )
    assert isinstance(response, EntityLinksResponse)
    assert response.revision == 8
    assert fixture.verify.await_args.kwargs["required_role"] == ProjectRole.CONTRIBUTOR
    assert fixture.verify.await_args.kwargs["require_existing_project"] is True
    fixture.visible.assert_awaited_once()
    fixture.readable.assert_awaited_once_with(fixture.ctx, fixture.target.entity)
    assert fixture.writer.await_args.kwargs["source"] is fixture.source
    assert fixture.writer.await_args.kwargs["targets"] == [fixture.target]
    edge = fixture.writer.await_args.kwargs["relationships"][0]
    assert (edge.source_id, edge.target_id, edge.relationship_type.value) == (
        "task-source",
        "task-target",
        "DEPENDS_ON",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["verify", "visible", "readable"])
async def test_link_endpoint_denial_never_calls_native_writer(link_route, gate):
    fixture = link_route
    getattr(fixture, gate).side_effect = HTTPException(status_code=404, detail="Entity not found")
    with pytest.raises(HTTPException):
        await entity_links.add_entity_links(
            fixture.source.entity.id,
            EntityLinksRequest(expected_revision=7, depends_on=[fixture.target.entity.id]),
            org=fixture.org,
            ctx=fixture.ctx,
            content_session=None,
        )
    fixture.writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_endpoint_missing_target_never_calls_native_writer(link_route):
    fixture = link_route
    with pytest.raises(HTTPException) as caught:
        await entity_links.add_entity_links(
            fixture.source.entity.id,
            EntityLinksRequest(expected_revision=7, depends_on=["missing"]),
            org=fixture.org,
            ctx=fixture.ctx,
            content_session=None,
        )
    assert caught.value.status_code == 404
    fixture.writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_endpoint_revision_conflict_returns_409(link_route):
    fixture = link_route
    fixture.writer.side_effect = EntityLinkConflictError("revision")
    with pytest.raises(HTTPException) as caught:
        await entity_links.add_entity_links(
            fixture.source.entity.id,
            EntityLinksRequest(expected_revision=7, depends_on=[fixture.target.entity.id]),
            org=fixture.org,
            ctx=fixture.ctx,
            content_session=None,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["reason"] == "revision"


@pytest.mark.asyncio
async def test_link_endpoint_exact_readonly_replay_is_reported(link_route):
    fixture = link_route
    fixture.writer.return_value = replace(
        fixture.result,
        revision=12,
        added_relationship_ids=[],
        existing_relationship_ids=fixture.result.added_relationship_ids,
        replayed=True,
    )
    response = await entity_links.add_entity_links(
        fixture.source.entity.id,
        EntityLinksRequest(expected_revision=7, depends_on=[fixture.target.entity.id]),
        org=fixture.org,
        ctx=fixture.ctx,
        content_session=None,
    )
    assert response.replayed
    assert response.revision == 12
    assert response.added_relationship_ids == []


@pytest.mark.parametrize("extra", ["content", "status", "metadata", "principal_id", "project_id"])
def test_link_request_refuses_body_and_ownership_fields(extra):
    with pytest.raises(ValidationError):
        EntityLinksRequest.model_validate({"expected_revision": 1, extra: "replace"})


@pytest.mark.parametrize("revision", [True, False, 0, -1, "1", 1.1])
def test_link_request_requires_actual_positive_revision(revision):
    with pytest.raises(ValidationError):
        EntityLinksRequest(expected_revision=revision)


@pytest.mark.asyncio
async def test_get_entity_returns_actual_graph_revision(link_route, monkeypatch):
    fixture = link_route
    service = SimpleNamespace(get_entity=AsyncMock(return_value=fixture.source.entity))
    monkeypatch.setattr(entity_policy, "require_entity_read_access", AsyncMock(return_value=set()))
    response = await entity_reads.get_entity(
        fixture.source.entity.id,
        org=fixture.org,
        ctx=fixture.ctx,
        service=service,
        include_summary=False,
        related_limit=0,
    )
    assert response.revision == 7
