"""Raw recall serves a remembered memory once, through its raw row, never its sidecar.

The entity route archives a capture sidecar beside every memory remembered
over REST: a private row carrying the graph row's id and type. The sidecar has
no lifecycle of its own, so a correction reaches the raw row and the graph row
and leaves the sidecar untouched. Recall that read the sidecar served every
memory twice to its author and kept serving one the author had corrected.
"""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import content_client, content_models, content_raw_recall
from sibyl_core.services.content_models import RawMemory, raw_memory_recallable
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import (
    get_raw_memory,
    list_raw_memories_for_scope,
    recall_raw_memory,
    recall_raw_memory_with_sources,
    remember_raw_memory,
)
from tests.test_raw_recall_embedding_space import NEAR, StaticProvider

OWNER = "owner"
PROJECT = "project-a"


@pytest.fixture
async def store(monkeypatch):
    """An embedded content store, or a scratch namespace on a live server.

    The embedded engine accepts statements a 3.x server rejects, so set
    SIBYL_LIVE_SURREAL_TESTS=1 and SIBYL_SURREAL_URL to run every recall
    statement here against a real server. The graph stays embedded; recall
    never reads it.
    """
    org = str(uuid4())
    namespace: str | None = None
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") == "1":
        namespace = f"raw_recall_sidecars_{uuid4().hex}"
        content = SurrealContentClient(
            url=os.environ["SIBYL_SURREAL_URL"],
            username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            namespace=namespace,
        )
    else:
        content = SurrealContentClient(url="memory://")
    graph = SurrealGraphClient(group_id=org, url="memory://")
    try:
        await bootstrap_content_schema(content, reset=namespace is None)
        await prepare_graph_schema(graph)
        runtime = GraphRuntime(
            client=graph,
            entity_manager=EntityManager(graph, group_id=org),
            relationship_manager=RelationshipManager(graph, group_id=org),
        )

        @asynccontextmanager
        async def session():
            yield content

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        monkeypatch.setattr(
            "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
            AsyncMock(return_value=runtime),
        )
        yield SimpleNamespace(org=org, runtime=runtime)
    finally:
        if namespace is not None:
            await content.execute_query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
        await content.close()
        await graph.close()


async def _remember(store, *, provider=None, **kwargs) -> RawMemory:
    return await remember_raw_memory(
        organization_id=store.org,
        principal_id=OWNER,
        source_id=f"cli:remember:{uuid4()}",
        title="Telescope routing lesson",
        raw_content="Route telescope traffic through the east mount first.",
        metadata={
            "capture_mode": "remember",
            "capture_surface": "cli",
            "remember_kind": "decision",
            "project_id": PROJECT,
        },
        capture_surface="cli",
        embedding_provider=provider,
        **kwargs,
    )


async def _archive_sidecar(store, raw: RawMemory) -> str:
    """Write the pair the REST remember path leaves behind.

    The record is the one archive_raw_capture builds, field for field: the
    graph row's id and type, no source, private to the writer, filed under
    the verified project. The graph row names the raw memory, and the raw
    memory is stamped with the sidecar, as mark_raw_capture_projected does.
    """
    entity_id = f"decision_{uuid4().hex[:12]}"
    await store.runtime.entity_manager.create_direct(
        Entity(
            id=entity_id,
            name=raw.title,
            content=raw.raw_content,
            entity_type=EntityType.DECISION,
            metadata={"memory_scope": "private", "principal_id": OWNER, "raw_memory_id": raw.id},
        ),
        generate_embedding=False,
    )
    sidecar_id = str(uuid4())
    metadata: dict[str, object] = {
        "capture_mode": "remember",
        "capture_surface": "cli",
        "remember_kind": "decision",
    }
    if "embedding_metadata" in raw.metadata:
        # The raw embedding repair stamps every capture it embeds, sidecars too.
        metadata["embedding_metadata"] = raw.metadata["embedding_metadata"]
    now = datetime.now(UTC)
    async with content_client.surreal_content_client() as client:
        await content_client.replace_record(
            client,
            "raw_captures",
            uuid=sidecar_id,
            record={
                "uuid": sidecar_id,
                "organization_id": store.org,
                "source_id": "",
                "principal_id": OWNER,
                "memory_scope": "private",
                "scope_key": None,
                "agent_id": None,
                "project_id": PROJECT,
                "review_state": "pending",
                "entity_id": entity_id,
                "title": raw.title,
                "raw_content": raw.raw_content,
                "entity_type": "decision",
                "tags": [],
                "embedding": raw.embedding,
                "metadata": metadata,
                "provenance": {},
                "capture_surface": "cli",
                "created_by_user_id": OWNER,
                "captured_at": now,
                "deleted_at": None,
                "purge_after": None,
                "created_at": now,
            },
        )
        await client.execute_query(
            "UPDATE raw_captures SET metadata.projected_capture_id = $sidecar_id "
            "WHERE uuid = $raw_id;",
            sidecar_id=sidecar_id,
            raw_id=raw.id,
        )
    return sidecar_id


def _lexical_lanes_only(monkeypatch, *, fulltext: bool) -> None:
    monkeypatch.setattr(
        content_raw_recall, "raw_memory_query_embedding", AsyncMock(return_value=None)
    )
    if not fulltext:
        monkeypatch.setattr(
            content_raw_recall,
            "_recall_raw_memory_fulltext",
            AsyncMock(side_effect=RuntimeError("exercise the lexical scan")),
        )


async def _recalled_ids(store, recall) -> list[str]:
    result = await recall(organization_id=store.org, principal_id=OWNER, query="telescope")
    memories = result if isinstance(result, list) else list(result.memories)
    return [memory.id for memory in memories]


@pytest.mark.parametrize("recall", [recall_raw_memory, recall_raw_memory_with_sources])
@pytest.mark.parametrize("fulltext", [True, False])
async def test_remembered_memory_is_recalled_once_through_its_raw_row(
    store, monkeypatch, recall, fulltext
):
    _lexical_lanes_only(monkeypatch, fulltext=fulltext)
    raw = await _remember(store)
    await _archive_sidecar(store, raw)

    assert await _recalled_ids(store, recall) == [raw.id]
    listed = await list_raw_memories_for_scope(organization_id=store.org, principal_id=OWNER)
    assert [memory.id for memory in listed] == [raw.id]


@pytest.mark.parametrize("action", ["hide", "mark_sensitive"])
@pytest.mark.parametrize("recall", [recall_raw_memory, recall_raw_memory_with_sources])
@pytest.mark.parametrize("fulltext", [True, False])
async def test_corrected_memory_leaves_the_authors_recall_with_its_sidecar(
    store, monkeypatch, action, recall, fulltext
):
    _lexical_lanes_only(monkeypatch, fulltext=fulltext)
    raw = await _remember(store)
    sidecar_id = await _archive_sidecar(store, raw)

    result = await apply_memory_correction(
        organization_id=store.org,
        source_id=raw.id,
        principal_id=OWNER,
        action=action,
        reason="probe",
        accessible_projects={PROJECT},
        writable_projects={PROJECT},
    )
    assert result.applied

    # The correction reached the raw row and left the sidecar as it was, so
    # only recall's own rule keeps the sidecar out.
    sidecar = await get_raw_memory(organization_id=store.org, memory_id=sidecar_id)
    assert sidecar is not None and raw_memory_recallable(sidecar)
    assert await _recalled_ids(store, recall) == []
    assert await list_raw_memories_for_scope(organization_id=store.org, principal_id=OWNER) == []


async def test_vector_lane_skips_an_embedded_sidecar(store, monkeypatch):
    provider = StaticProvider("openai", "text-embedding-3-small", NEAR)
    raw = await _remember(store, provider=provider)
    assert raw.embedding is not None
    await _archive_sidecar(store, raw)
    monkeypatch.setattr(
        content_models, "configured_raw_memory_embedding_provider", lambda: provider
    )

    result = await recall_raw_memory_with_sources(
        organization_id=store.org, principal_id=OWNER, query="unmatched-query-marker"
    )

    lanes = {source.source: source for source in result.sources}
    assert lanes["raw_vector"].failure is None
    assert [memory.id for memory in lanes["raw_vector"].candidates] == [raw.id]
    assert [memory.id for memory in result.memories] == [raw.id]


@pytest.mark.parametrize("fulltext", [True, False])
async def test_raw_rows_filed_under_a_graph_type_stay_recallable(store, monkeypatch, fulltext):
    """Reflection sources and synthesis artifacts are raw writes, not sidecars.

    They carry a graph type in entity_type and no graph id, so recall keeps
    them; a rule that admitted only entity_type 'raw_memory' would drop them.
    """
    _lexical_lanes_only(monkeypatch, fulltext=fulltext)
    session = await _remember(store, entity_type="session")
    artifact = await _remember(store, entity_type="artifact")
    raw = await _remember(store)
    await _archive_sidecar(store, raw)

    assert set(await _recalled_ids(store, recall_raw_memory)) == {
        session.id,
        artifact.id,
        raw.id,
    }
