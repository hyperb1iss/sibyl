"""Canonical raw-to-graph memory capture orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryCaptureRequest:
    title: str
    content: str
    entity_type: str = "episode"
    domain: str | None = None
    tags: Sequence[str] | None = None
    related_to: Sequence[str] | None = None
    languages: Sequence[str] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    source_id: str | None = None
    memory_scope: str = "private"
    scope_key: str | None = None
    capture_surface: str = "cli"
    wait_searchable: bool = False
    skip_conflicts: bool = False
    diary: bool = False
    agent_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryCaptureResult:
    payload: dict[str, Any]
    raw_memory_id: str | None
    raw_source_id: str | None
    raw_policy_reason: str | None

    def to_payload(self) -> dict[str, Any]:
        return dict(self.payload)


type RawMemoryCaptureWriter = Callable[
    [MemoryCaptureRequest],
    Awaitable[Mapping[str, Any]],
]
type GraphMemoryCaptureWriter = Callable[
    [MemoryCaptureRequest, Mapping[str, Any]],
    Awaitable[Mapping[str, Any]],
]


class MemoryCaptureService:
    def __init__(
        self,
        *,
        remember_raw_memory: RawMemoryCaptureWriter,
        create_graph_entity: GraphMemoryCaptureWriter,
    ) -> None:
        self._remember_raw_memory = remember_raw_memory
        self._create_graph_entity = create_graph_entity

    async def capture(self, request: MemoryCaptureRequest) -> MemoryCaptureResult:
        raw_memory = await self._remember_raw_memory(request)
        raw_memory_id = _optional_str(raw_memory.get("id"))
        raw_source_id = _optional_str(raw_memory.get("source_id"))
        raw_policy_reason = _optional_str(raw_memory.get("policy_reason"))

        graph_metadata: dict[str, Any] = dict(request.metadata)
        if raw_memory_id:
            graph_metadata["raw_memory_id"] = raw_memory_id
        if raw_source_id:
            graph_metadata["raw_source_id"] = raw_source_id
        if raw_policy_reason:
            graph_metadata["raw_policy_reason"] = raw_policy_reason

        graph_payload = dict(await self._create_graph_entity(request, graph_metadata))
        graph_payload["raw_memory_id"] = raw_memory_id
        graph_payload["raw_source_id"] = raw_source_id
        graph_payload["raw_policy_reason"] = raw_policy_reason
        return MemoryCaptureResult(
            payload=graph_payload,
            raw_memory_id=raw_memory_id,
            raw_source_id=raw_source_id,
            raw_policy_reason=raw_policy_reason,
        )


def _optional_str(value: object) -> str | None:
    return str(value) if value else None
