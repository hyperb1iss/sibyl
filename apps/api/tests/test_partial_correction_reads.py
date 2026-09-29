"""Native canonical capture controls for reads after an incomplete correction."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.api.routes.entity_reads import get_entity
from sibyl.persistence.graph_runtime import GraphReadServiceAdapter
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
from sibyl_core.memory_pipeline.source_lifecycle import (
    SOURCE_BINDINGS_KEY,
    source_revision_bindings,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import content_client
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import (
    get_raw_memory,
    raw_memory_recallable,
    remember_raw_memory,
)
from sibyl_core.tools.add import _create_entity_record
from tests.harness.auth import stub_auth_context


@pytest.mark.parametrize("stamp_fails", [False, True], ids=["healthy-stamp", "failed-stamp"])
@pytest.mark.parametrize(("include_summary", "related_limit"), [(False, 0), (True, 0), (True, 5)])
async def test_partial_correction_never_serves_canonical_source_text(
    monkeypatch, stamp_fails, include_summary, related_limit
):
    ctx = stub_auth_context(organization_id=uuid4())
    org = ctx.organization
    content = SurrealContentClient(url="memory://")
    graph = SurrealGraphClient(group_id=str(org.id), url="memory://")
    try:
        await bootstrap_content_schema(content, reset=True)
        await prepare_graph_schema(graph)
        runtime = GraphRuntime(
            client=graph,
            entity_manager=EntityManager(graph, group_id=str(org.id)),
            relationship_manager=RelationshipManager(graph, group_id=str(org.id)),
        )

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        monkeypatch.setattr(
            "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
            AsyncMock(return_value=runtime),
        )
        monkeypatch.setattr(
            "sibyl.api.routes.entity_policy.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        )

        async def raw_writer(request):
            memory = await remember_raw_memory(
                organization_id=str(org.id),
                principal_id=ctx.user_id,
                source_id="canonical-capture",
                raw_content=request.content,
                memory_scope="private",
                embedding_provider=None,
            )
            return {
                "id": memory.id,
                "source_id": memory.source_id,
                SOURCE_BINDINGS_KEY: source_revision_bindings([memory]),
            }

        async def graph_writer(request, metadata):
            entity = Entity(
                id="canonical-source-note",
                name=request.title,
                content=request.content,
                entity_type=EntityType.NOTE,
                metadata=dict(metadata),
            )
            entity_id = await _create_entity_record(
                runtime.entity_manager,
                entity,
                generate_embeddings=False,
                organization_id=str(org.id),
            )
            return {"id": entity_id}

        capture = await MemoryCaptureService(
            remember_raw_memory=raw_writer,
            create_graph_entity=graph_writer,
        ).capture(
            MemoryCaptureRequest(
                title="Canonical note",
                content="Source-era secret advice",
                entity_type="note",
                principal_id=ctx.user_id,
                memory_scope="private",
            )
        )
        service = GraphReadServiceAdapter.from_runtime(runtime, str(org.id))
        before = await get_entity(
            "canonical-source-note",
            org=org,
            ctx=ctx,
            service=service,
            include_summary=include_summary,
            related_limit=related_limit,
        )
        assert before.content == "Source-era secret advice"
        if stamp_fails:
            monkeypatch.setattr(
                runtime.entity_manager,
                "update",
                AsyncMock(side_effect=RuntimeError("Injected failed graph stamp")),
            )
        correction = await apply_memory_correction(
            organization_id=str(org.id),
            source_id=capture.raw_memory_id,
            principal_id=ctx.user_id,
            action="delete",
        )
        assert correction.applied
        assert correction.propagation_complete is (not stamp_fails)
        root = await get_raw_memory(organization_id=str(org.id), memory_id=capture.raw_memory_id)
        assert not raw_memory_recallable(root)
        current = await service.get_entity("canonical-source-note")
        native = await available_graph_entities(str(org.id), [current.id], runtime=runtime)
        assert current.id not in native
        with pytest.raises(HTTPException) as error:
            await get_entity(
                current.id,
                org=org,
                ctx=ctx,
                service=service,
                include_summary=include_summary,
                related_limit=related_limit,
            )
        assert error.value.status_code == 404
    finally:
        await content.close()
        await graph.close()
