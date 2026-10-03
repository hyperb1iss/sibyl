"""Two authors who write the same title must not overwrite each other.

Entity ids hash type, title, and category, and the write path upserts at the
id, so on a shared server one teammate's memory replaced another's of the
same title and took over its authorship, private memories included. These
run the real `add()` against embedded Surreal and assert the ids each write
lands on. The embedded engine does not apply a replace to an existing row the
way a 3.x server does, so content assertions are limited to rows that only
one write ever touched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

import sibyl_core.tools.add as add_module
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import (
    EntityManager,
    GraphRuntime,
    RelationshipManager,
    SurrealGraphClient,
    prepare_graph_schema,
)
from sibyl_core.tools.add import add
from sibyl_core.tools.helpers import _generate_id

GROUP = "author-collision-org"
ALICE = "user-alice"
BOB = "user-bob"


@pytest.fixture
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[GraphRuntime]:
    client = SurrealGraphClient(group_id=GROUP, url="memory://")
    await client.connect()
    try:
        await prepare_graph_schema(client)
        graph = GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=GROUP),
            relationship_manager=RelationshipManager(client, group_id=GROUP),
        )

        async def runtime_factory(requested_group_id: str, **_kwargs: Any) -> GraphRuntime:
            assert requested_group_id == GROUP
            return graph

        async def no_auto_links(**_kwargs: Any) -> list[tuple[str, float]]:
            return []

        monkeypatch.setattr(add_module, "get_graph_runtime", runtime_factory)
        monkeypatch.setattr(add_module, "_auto_discover_links", no_auto_links)
        yield graph
    finally:
        await client.close()


async def _write(
    title: str,
    content: str,
    *,
    principal: str | None,
    entity_type: str = "decision",
    scope: str = "private",
) -> Any:
    return await add(
        title,
        content,
        entity_type=entity_type,
        metadata={"organization_id": GROUP},
        principal_id=principal,
        memory_scope=scope,
        sync=True,
        generate_embeddings=False,
        check_conflicts=False,
    )


async def _content(graph: GraphRuntime, entity_id: str) -> str:
    return (await graph.entity_manager.get(entity_id)).content


@pytest.mark.asyncio
async def test_a_second_author_gets_their_own_row(runtime: GraphRuntime) -> None:
    first = await _write("Use Temporal for workflows", "Alice's private reasoning", principal=ALICE)
    second = await _write("Use Temporal for workflows", "Bob's private reasoning", principal=BOB)

    assert first.success and second.success
    assert first.id != second.id
    assert await _content(runtime, str(first.id)) == "Alice's private reasoning"
    assert await _content(runtime, str(second.id)) == "Bob's private reasoning"
    alice_row = await runtime.entity_manager.get(str(first.id))
    assert alice_row.metadata.get("principal_id") == ALICE


@pytest.mark.asyncio
async def test_an_author_rewriting_their_title_keeps_their_id(runtime: GraphRuntime) -> None:
    first = await _write("Use Temporal for workflows", "Alice's private reasoning", principal=ALICE)
    second = await _write("Use Temporal for workflows", "Bob's private reasoning", principal=BOB)
    bob_again = await _write("Use Temporal for workflows", "Bob, revised", principal=BOB)
    alice_again = await _write("Use Temporal for workflows", "Alice, revised", principal=ALICE)

    assert bob_again.id == second.id
    assert alice_again.id == first.id
    assert first.id == _generate_id("decision", "Use Temporal for workflows", "general")


@pytest.mark.asyncio
async def test_a_row_with_no_recorded_author_keeps_its_id(runtime: GraphRuntime) -> None:
    legacy_id = _generate_id("decision", "Legacy decision", "general")
    await runtime.entity_manager.create_direct(
        Entity(
            id=legacy_id,
            entity_type=EntityType.DECISION,
            name="Legacy decision",
            description="written before authors were recorded",
            content="written before authors were recorded",
            metadata={"organization_id": GROUP},
        ),
        generate_embedding=False,
    )

    written = await _write("Legacy decision", "now claimed", principal=ALICE, scope="project")

    assert written.success, written.message
    assert written.id == legacy_id


@pytest.mark.asyncio
async def test_a_second_authors_project_of_the_same_name_is_refused(
    runtime: GraphRuntime,
) -> None:
    first = await _write(
        "Platform", "Alice's project", principal=ALICE, entity_type="project", scope="project"
    )
    second = await _write(
        "Platform", "Bob's project", principal=BOB, entity_type="project", scope="project"
    )

    assert first.success, first.message
    assert not second.success
    assert "already exists" in second.message
    assert await _content(runtime, str(first.id)) == "Alice's project"

    alice_again = await _write(
        "Platform",
        "Alice's project, revised",
        principal=ALICE,
        entity_type="project",
        scope="project",
    )
    assert alice_again.id == first.id
