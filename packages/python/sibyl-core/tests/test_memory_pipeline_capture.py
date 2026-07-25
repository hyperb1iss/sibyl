from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService


@pytest.mark.asyncio
async def test_memory_capture_service_writes_raw_source_before_graph_entity() -> None:
    events: list[tuple[str, object]] = []

    async def remember_raw_memory(request: MemoryCaptureRequest) -> Mapping[str, Any]:
        events.append(("raw", request))
        return {
            "id": "raw_123",
            "source_id": "cli:manual",
            "policy_reason": "private_principal_bound",
            "mutation_receipt": {
                "operation_id": "remember-1",
                "applied": True,
                "revision": 1,
            },
        }

    async def create_graph_entity(
        request: MemoryCaptureRequest,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        events.append(("graph", dict(metadata)))
        return {"id": "decision_123", "metadata": dict(metadata)}

    service = MemoryCaptureService(
        remember_raw_memory=remember_raw_memory,
        create_graph_entity=create_graph_entity,
    )

    result = await service.capture(
        MemoryCaptureRequest(
            title="Use context packs",
            content="Agents should receive grouped memory before building.",
            entity_type="decision",
            metadata={
                "capture_mode": "remember",
                "memory_importance": 0.8,
                "reflection_confidence": 0.9,
            },
        )
    )

    assert [name for name, _event in events] == ["raw", "graph"]
    assert result.to_payload() == {
        "id": "decision_123",
        "metadata": {
            "capture_mode": "remember",
            "confidence": 0.9,
            "importance": 0.8,
            "memory_scope": "private",
            "raw_memory_id": "raw_123",
            "raw_source_id": "cli:manual",
            "raw_policy_reason": "private_principal_bound",
        },
        "raw_memory_id": "raw_123",
        "raw_source_id": "cli:manual",
        "raw_policy_reason": "private_principal_bound",
        "mutation_receipt": {
            "operation_id": "remember-1",
            "applied": True,
            "revision": 1,
        },
    }
    assert "mutation_receipt" not in result.to_payload()["metadata"]


@pytest.mark.asyncio
async def test_memory_capture_service_omits_missing_raw_receipts_from_metadata() -> None:
    async def remember_raw_memory(_request: MemoryCaptureRequest) -> Mapping[str, Any]:
        return {"id": "raw_123"}

    async def create_graph_entity(
        _request: MemoryCaptureRequest,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {"id": "episode_123", "metadata": dict(metadata)}

    service = MemoryCaptureService(
        remember_raw_memory=remember_raw_memory,
        create_graph_entity=create_graph_entity,
    )

    result = await service.capture(
        MemoryCaptureRequest(
            title="Raw only source",
            content="Body",
            metadata={"capture_mode": "remember"},
        )
    )

    assert result.to_payload()["metadata"] == {
        "capture_mode": "remember",
        "memory_scope": "private",
        "raw_memory_id": "raw_123",
    }
    assert result.raw_source_id is None
    assert result.raw_policy_reason is None


@pytest.mark.asyncio
async def test_memory_capture_service_stamps_scope_onto_graph_metadata() -> None:
    """Graph rows carry no scope column, so capture must stamp it into metadata.

    Without this the entity lands unscoped, and retrieval's scope filter treats
    a missing memory_scope as unrestricted and serves it to the whole org.
    """
    captured: dict[str, Any] = {}

    async def remember_raw_memory(_request: MemoryCaptureRequest) -> Mapping[str, Any]:
        return {"id": "raw_private_1", "source_id": "cli:manual"}

    async def create_graph_entity(
        _request: MemoryCaptureRequest,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        captured.update(metadata)
        return {"id": "note_1", "metadata": dict(metadata)}

    service = MemoryCaptureService(
        remember_raw_memory=remember_raw_memory,
        create_graph_entity=create_graph_entity,
    )

    await service.capture(
        MemoryCaptureRequest(
            title="Private reflection",
            content="Something only I should read.",
            entity_type="note",
            metadata={"capture_mode": "remember"},
            principal_id="user-alice",
        )
    )

    assert captured["memory_scope"] == "private"
    assert captured["principal_id"] == "user-alice"


@pytest.mark.asyncio
async def test_memory_capture_service_stamps_non_private_scope_and_key() -> None:
    captured: dict[str, Any] = {}

    async def remember_raw_memory(_request: MemoryCaptureRequest) -> Mapping[str, Any]:
        return {"id": "raw_project_1"}

    async def create_graph_entity(
        _request: MemoryCaptureRequest,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        captured.update(metadata)
        return {"id": "note_2", "metadata": dict(metadata)}

    service = MemoryCaptureService(
        remember_raw_memory=remember_raw_memory,
        create_graph_entity=create_graph_entity,
    )

    await service.capture(
        MemoryCaptureRequest(
            title="Project decision",
            content="Shared with the project.",
            entity_type="decision",
            memory_scope="project",
            scope_key="project_123",
            principal_id="user-alice",
        )
    )

    assert captured["memory_scope"] == "project"
    assert captured["scope_key"] == "project_123"
    assert captured["principal_id"] == "user-alice"
