"""Drive `sibyl remember` and `sibyl correct` through the server code they reach.

The CLI's default remember is two requests: the verbatim raw write, then a
graph write naming the raw id it got back. `cli_remember` runs the CLI's own
orchestration (`MemoryCaptureService`, shared with `sibyl_cli.capture_support`)
and answers each request with the server code behind it, so a test sees the
exact body the CLI sends and the exact route that strips or binds it.

`POST /memory/raw` is answered by the raw writer and its response serializer
rather than the route itself: the route's own work is scope resolution and
audit logging, and the tests here pass an explicit scope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.api.routes.entity_mutations import create_entity
from sibyl.api.routes.memory_serialization import raw_memory_response
from sibyl.api.schemas import EntityCreate
from sibyl.auth.context import AuthContext
from sibyl.persistence.surreal import content as api_content
from sibyl_core.auth import AuthOrganization
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
from sibyl_core.services import content_client
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.surreal_content import remember_raw_memory

type GraphMetadataTamper = Callable[[dict[str, Any]], dict[str, Any]]


def _http_request() -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.cookies = {}
    return request


@dataclass(frozen=True, slots=True)
class CaptureStack:
    """One organization's content store and graph, wired into the server code."""

    organization: AuthOrganization
    runtime: GraphRuntime

    @property
    def group_id(self) -> str:
        return str(self.organization.id)


def wire_capture_stack(
    monkeypatch: pytest.MonkeyPatch,
    *,
    organization: AuthOrganization,
    content: SurrealContentClient,
    graph: SurrealGraphClient,
    teams: set[str] | None = None,
) -> CaptureStack:
    """Point the entity route, the graph writer and the correction at one store.

    ``teams`` is what the auth store answers for the caller's team memberships,
    which is where every route resolves team grants from.
    """
    group_id = str(organization.id)
    runtime = GraphRuntime(
        client=graph,
        entity_manager=EntityManager(graph, group_id=group_id),
        relationship_manager=RelationshipManager(graph, group_id=group_id),
    )

    @asynccontextmanager
    async def session():
        yield content

    async def runtime_for(requested: str, **_kwargs: object) -> GraphRuntime:
        assert str(requested) == group_id
        return runtime

    monkeypatch.setattr(api_content, "surreal_content_client", session)
    monkeypatch.setattr(content_client, "surreal_content_client", session)
    monkeypatch.setattr("sibyl_core.tools.add.get_graph_runtime", runtime_for)
    monkeypatch.setattr("sibyl.api.routes.entity_policy.get_entity_graph_runtime", runtime_for)
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime", runtime_for
    )
    monkeypatch.setattr("sibyl.api.routes.entity_mutations.broadcast_event", AsyncMock())
    monkeypatch.setattr(
        "sibyl.api.routes.memory_auth.list_accessible_team_scope_keys",
        AsyncMock(return_value=set(teams or ())),
    )
    return CaptureStack(organization=organization, runtime=runtime)


async def post_memory_raw(
    stack: CaptureStack,
    ctx: AuthContext,
    capture: MemoryCaptureRequest,
) -> dict[str, Any]:
    """`POST /memory/raw`: store the verbatim capture, answer with its response body."""
    assert ctx.user_id is not None
    memory = await remember_raw_memory(
        organization_id=stack.group_id,
        principal_id=ctx.user_id,
        source_id=capture.source_id or f"{capture.capture_surface}:manual",
        raw_content=capture.content,
        title=capture.title,
        memory_scope=capture.memory_scope or "private",
        scope_key=capture.scope_key,
        tags=list(capture.tags) if capture.tags is not None else None,
        metadata=dict(capture.metadata),
        provenance=dict(capture.provenance),
        capture_surface=capture.capture_surface,
        embedding_provider=None,
    )
    return raw_memory_response(memory).model_dump(mode="json")


async def post_entities(
    stack: CaptureStack,
    ctx: AuthContext,
    *,
    title: str,
    content: str,
    kind: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """`POST /entities?sync=true` with the body `client_graph.create_entity` sends."""
    response = await create_entity(
        request=_http_request(),
        entity=EntityCreate(
            name=title,
            content=content,
            entity_type=kind,
            metadata=dict(metadata),
            skip_conflicts=True,
        ),
        org=stack.organization,
        ctx=ctx,
        content_session=None,
        sync=True,
        protect_ownership=False,
        replay_interrupted=True,
    )
    return response.model_dump(mode="json")


async def cli_remember(
    stack: CaptureStack,
    ctx: AuthContext,
    *,
    title: str,
    content: str,
    kind: str = "episode",
    memory_scope: str | None = None,
    scope_key: str | None = None,
    tamper: GraphMetadataTamper | None = None,
) -> dict[str, Any]:
    """`sibyl remember --wait-searchable`, the default two-request path.

    ``tamper`` rewrites the graph body before it is sent, which is how a test
    plays a client that lies about the raw id it is projecting.
    """
    request = MemoryCaptureRequest(
        title=title,
        content=content,
        entity_type=kind,
        metadata={"capture_mode": "remember", "capture_surface": "cli", "remember_kind": kind},
        provenance={"remember_kind": kind, "related_to": []},
        memory_scope=memory_scope,
        scope_key=scope_key,
        capture_surface="cli",
        wait_searchable=True,
        skip_conflicts=True,
    )

    async def remember_raw(capture: MemoryCaptureRequest) -> dict[str, Any]:
        return await post_memory_raw(stack, ctx, capture)

    async def create_graph_entity(
        capture: MemoryCaptureRequest, graph_metadata: Mapping[str, Any]
    ) -> dict[str, Any]:
        body = dict(graph_metadata)
        if tamper is not None:
            body = tamper(body)
        return await post_entities(
            stack,
            ctx,
            title=capture.title,
            content=capture.content,
            kind=capture.entity_type,
            metadata=body,
        )

    result = await MemoryCaptureService(
        remember_raw_memory=remember_raw,
        create_graph_entity=create_graph_entity,
    ).capture(request)
    return result.to_payload()
