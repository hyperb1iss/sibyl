"""The window between correcting a capture and projecting it into the graph.

`remember` writes the capture, queues the graph write, and hands back an id the
caller can correct immediately. The queued payload was serialized before the
correction existed, so the worker builds the row from a description of a memory
that has since been retired. Nothing reconciles it afterwards: the correction
write-through already ran and found no rows.

These tests run the real `create_entity` job against a real embedded graph and
read the result back through `compile_context`, because the claim being made is
about what an agent can recall, not about which keys a dict carries.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import AsyncIterator, Mapping
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import sibyl_core.tools.add as add_module
from sibyl.jobs.entities import create_entity
from sibyl_core.services.graph import (
    EntityManager,
    RelationshipManager,
    SurrealGraphClient,
    prepare_graph_schema,
)
from sibyl_core.services.surreal_content import RawMemory
from sibyl_core.tools import context as context_module

PROJECT_ID = "proj-correction-race"
PRINCIPAL = "user-correction-race"
RAW_MEMORY_ID = "raw-corrected-capture"


class _Runtime:
    def __init__(self, client: Any, entities: Any, relationships: Any, group_id: str) -> None:
        self.client = client
        self.entity_manager = entities
        self.relationship_manager = relationships
        self.group_id = group_id


@pytest_asyncio.fixture
async def graph(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Runtime]:
    group_id = f"correction-race-{uuid_module.uuid4().hex[:12]}"
    client = SurrealGraphClient(group_id=group_id, url="memory://")
    await client.connect()
    await prepare_graph_schema(client)
    runtime = _Runtime(
        client,
        EntityManager(client, group_id=group_id),
        RelationshipManager(client, group_id=group_id),
        group_id,
    )

    async def runtime_factory(requested_group_id: str, **_kwargs: object) -> _Runtime:
        assert str(requested_group_id) == group_id
        return runtime

    import sibyl.jobs.entities as jobs_entities
    from sibyl_core.retrieval import _search_database as search_database

    monkeypatch.setattr(jobs_entities, "get_surreal_graph_runtime", runtime_factory)
    monkeypatch.setattr(search_database, "get_surreal_graph_runtime", runtime_factory)
    monkeypatch.setattr(context_module, "get_surreal_graph_runtime", runtime_factory)
    monkeypatch.setattr(context_module, "get_graph_runtime", runtime_factory, raising=False)
    monkeypatch.setattr(context_module, "configured_embedding_provider", lambda: None)
    yield runtime
    await client.close()


def _corrected_capture(**overrides: Any) -> RawMemory:
    """The capture as `raw_captures` holds it after `sibyl correct` ran."""

    values: dict[str, Any] = {
        "id": RAW_MEMORY_ID,
        "organization_id": "org-correction-race",
        "source_id": "source-corrected-capture",
        "principal_id": PRINCIPAL,
        "review_state": "pending",
        "entity_type": "pattern",
        "title": "Deploy to Fly",
        "raw_content": "We deploy to fly.io.",
        "metadata": {
            "lifecycle_state": "contested",
            "lifecycle_flags": [],
            "lifecycle_action": "mark_wrong",
        },
    }
    values.update(overrides)
    return RawMemory(**values)


class _RecordingQueue:
    """Stands in for arq, keeping the payload `add()` actually serialized."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def enqueue_create_entity(
        self,
        *,
        entity_id: str,
        entity_data: Mapping[str, Any],
        entity_type: str,
        group_id: str,
        relationships: Any = None,
        auto_link_params: Mapping[str, Any] | None = None,
        generate_embeddings: bool = True,
    ) -> str:
        self.payloads.append(dict(entity_data))
        return f"job-{entity_id}"

    async def enqueue_entity_embedding_backfill(self, **_kwargs: object) -> str:
        return "job-backfill"


async def _queued_payload(monkeypatch: pytest.MonkeyPatch, group_id: str) -> dict[str, Any]:
    """The payload the real `add()` puts on the queue, not one built by hand.

    Building it by hand is how the first version of this test hid a defect that
    made the whole reconciliation inert: `add()` strips server-owned keys from
    caller metadata, and the capture pipeline stamps provenance before calling
    it, so the queued payload named no capture at all. A test that supplies
    provenance itself proves nothing about the path that has to supply it.
    """

    body = "\n\n".join(
        f"## Fly section {index}\n\n" + f"fly hosting rollout body line {index} " * 40
        for index in range(4)
    )
    queue = _RecordingQueue()
    monkeypatch.setattr(add_module, "get_queue_port", lambda: queue)

    response = await add_module.add(
        title="Deploy to Fly",
        content=body,
        entity_type="pattern",
        project=PROJECT_ID,
        memory_scope="project",
        scope_key=PROJECT_ID,
        principal_id=PRINCIPAL,
        check_conflicts=False,
        # Exactly what the capture pipeline hands the graph writer: the raw
        # ids on the dedicated argument, and a metadata bag that also carries
        # them and gets them stripped.
        metadata={
            "organization_id": group_id,
            "raw_memory_id": RAW_MEMORY_ID,
            "raw_source_id": "source-corrected-capture",
        },
        capture_provenance={
            "raw_memory_id": RAW_MEMORY_ID,
            "raw_source_id": "source-corrected-capture",
        },
    )

    assert response.success
    assert len(queue.payloads) == 1, "the write has to have gone through the queue"
    return queue.payloads[0]


async def _no_raw_memories(**_kwargs: object) -> list[Any]:
    return []


async def _no_active_work(**_kwargs: object) -> list[Any]:
    return []


async def _no_fallback(**_kwargs: object) -> list[Any]:
    raise AssertionError("compile_context fell back to the non-native search path")


async def _served_ids(runtime: _Runtime, goal: str) -> set[str]:
    pack = await context_module.compile_context(
        goal,
        intent="build",
        project=PROJECT_ID,
        accessible_projects={PROJECT_ID},
        principal_id=PRINCIPAL,
        organization_id=runtime.group_id,
        search_fn=_no_fallback,
        raw_memory_recall_fn=_no_raw_memories,
        active_work_fn=_no_active_work,
        record_exposure=False,
    )
    return {item.id for section in pack.sections for item in section.items}


async def _run_create_entity(
    runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    payload = await _queued_payload(monkeypatch, runtime.group_id)
    assert payload["metadata"].get("raw_memory_id") == RAW_MEMORY_ID, (
        "the queued payload has to name the capture, or the worker has nothing to read"
    )
    with (
        patch("sibyl.jobs.entities._safe_broadcast", AsyncMock()),
        patch("sibyl.jobs.pending.clear_pending", AsyncMock()),
        patch("sibyl.jobs.pending.process_pending_operations", AsyncMock(return_value=[])),
        patch("sibyl_core.tools.conflicts.find_similar_entities", AsyncMock(return_value=[])),
        patch(
            "sibyl.jobs.memory_extraction.enqueue_memory_extraction_batches",
            AsyncMock(
                return_value=type(
                    "_Enqueue",
                    (),
                    {
                        "status": "skipped",
                        "reason": "disabled",
                        "job_ids": [],
                        "queued_sources": 0,
                        "skipped_sources": 1,
                    },
                )()
            ),
        ),
    ):
        return await create_entity(
            {},
            payload,
            "pattern",
            runtime.group_id,
            generate_embeddings=True,
        )


@pytest.mark.asyncio
async def test_a_row_projected_after_its_capture_was_corrected_is_born_retired(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correction lands first, the worker runs second, and nothing recallable appears.

    The payload the worker holds describes a live memory, because it was
    written before the correction. `raw_captures` is the row the correction
    actually mutated, so it is the authority, and the projection boundary is
    where the two are reconciled.
    """

    from sibyl_core.services import memory_lifecycle

    monkeypatch.setattr(
        memory_lifecycle,
        "get_raw_memory",
        AsyncMock(return_value=_corrected_capture()),
    )

    result = await _run_create_entity(graph, monkeypatch)
    parent_id = result["entity_id"]

    stored = await graph.entity_manager.get(result["entity_id"])
    assert stored is not None, "the row is still written; it is written retired"
    assert stored.metadata.get("excluded_from_recall") is True
    assert stored.metadata.get("lifecycle_state") == "contested"

    served = await _served_ids(graph, "fly hosting rollout body")
    assert parent_id not in served
    assert not {item_id for item_id in served if item_id.startswith("passage")}


@pytest.mark.asyncio
async def test_an_uncorrected_capture_still_projects_a_recallable_row(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: without a correction the same job produces a servable memory.

    Without this, a projection boundary that retired everything, or one that
    silently failed to project at all, would pass the test above.
    """

    from sibyl_core.services import memory_lifecycle

    monkeypatch.setattr(
        memory_lifecycle,
        "get_raw_memory",
        AsyncMock(return_value=_corrected_capture(metadata={})),
    )

    await _run_create_entity(graph, monkeypatch)

    served = await _served_ids(graph, "fly hosting rollout body")
    # The spans are the memory's presence in the pack: a projection whose spans
    # cover the whole body suppresses the fat parent on purpose, so their
    # presence is what says this content is recallable.
    assert {item_id for item_id in served if item_id.startswith("passage")}


@pytest.mark.asyncio
async def test_a_correction_landing_inside_the_write_still_retires_the_row(
    graph: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interval a pre-write read can never cover.

    The insert is an await. A correction can retire the capture and run its
    cascade while that insert is in flight: the cascade finds no row, the
    pre-write read already returned the old verdict, and the stale row commits
    behind both. Only a pass that runs after the row exists can see it, which
    is why the write is bracketed rather than preceded.

    The correction is injected inside the `create_direct` await, which is the
    only place it can land to reproduce this.
    """

    from sibyl_core.services import memory_lifecycle

    capture = _corrected_capture(metadata={})
    monkeypatch.setattr(memory_lifecycle, "get_raw_memory", AsyncMock(return_value=capture))

    original_create = graph.entity_manager.create_direct
    corrected: dict[str, bool] = {}

    async def create_direct_with_correction(entity: Any, **kwargs: Any) -> str:
        created = await original_create(entity, **kwargs)
        if not corrected:
            # Mid-flight: the memory is corrected while the row is being
            # written, so the correction's own cascade cannot see it.
            corrected["done"] = True
            monkeypatch.setattr(
                memory_lifecycle,
                "get_raw_memory",
                AsyncMock(return_value=_corrected_capture()),
            )
        return created

    monkeypatch.setattr(
        graph.entity_manager, "create_direct", create_direct_with_correction, raising=False
    )

    result = await _run_create_entity(graph, monkeypatch)
    parent_id = result["entity_id"]
    assert corrected, "the correction has to have landed inside the write"

    stored = await graph.entity_manager.get(parent_id)
    assert stored is not None
    assert stored.metadata.get("excluded_from_recall") is True
    assert stored.metadata.get("lifecycle_state") == "contested"

    served = await _served_ids(graph, "fly hosting rollout body")
    assert parent_id not in served
