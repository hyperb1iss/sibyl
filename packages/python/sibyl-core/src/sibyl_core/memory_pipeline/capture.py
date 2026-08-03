"""Canonical raw-to-graph memory capture orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sibyl_core.auth.memory_policy import stamp_memory_scope_metadata
from sibyl_core.memory_pipeline.quality import normalize_memory_quality_metadata
from sibyl_core.memory_pipeline.structure import MemoryStructure, build_memory_structure


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
    principal_id: str | None = None
    capture_surface: str = "cli"
    wait_searchable: bool = False
    skip_conflicts: bool = False
    diary: bool = False
    agent_id: str | None = None
    project_id: str | None = None
    # Structure the writing agent supplies for its own memory. Spans are
    # ``[start, end)`` offsets into ``stored_content`` and never replacement
    # text; ``atomic`` refuses cutting outright; probes are the questions this
    # memory has to answer, rehearsed against live retrieval at write time.
    spans: Sequence[Mapping[str, Any]] | None = None
    atomic: bool = False
    probes: Sequence[str] | None = None

    @property
    def stored_content(self) -> str:
        """The body the graph row will hold, and the string spans address.

        The graph write strips the content, so offsets computed against an
        unstripped string would land one place at validation and another at
        projection. Validating against this property keeps both readings the
        same one.
        """
        return self.content.strip()

    def structure(self) -> MemoryStructure:
        """Validate the declared structure against the body that will be stored."""
        return build_memory_structure(
            self.stored_content,
            spans=self.spans,
            atomic=self.atomic,
            probes=self.probes,
        )


@dataclass(frozen=True, slots=True)
class MemoryCaptureResult:
    payload: dict[str, Any]
    raw_memory_id: str | None
    raw_source_id: str | None
    raw_policy_reason: str | None
    mutation_receipt: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.payload)
        if self.mutation_receipt is not None:
            payload["mutation_receipt"] = dict(self.mutation_receipt)
        return payload


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
        # Before the raw write, not after. The raw capture lands first and the
        # graph write validates the same plan again, so validating late would
        # leave a verbatim record behind for a write that then fails, and the
        # caller would retry into a duplicate.
        request.structure()
        raw_memory = await self._remember_raw_memory(request)
        raw_memory_id = _optional_str(raw_memory.get("id"))
        raw_source_id = _optional_str(raw_memory.get("source_id"))
        raw_policy_reason = _optional_str(raw_memory.get("policy_reason"))
        raw_receipt = raw_memory.get("mutation_receipt")
        mutation_receipt = dict(raw_receipt) if isinstance(raw_receipt, Mapping) else None

        # The graph row carries no scope column, so retrieval and projection
        # authorize a candidate from this metadata alone. An unstamped row reads
        # as unscoped and is served to every principal in the organization, and
        # an owner field surviving from request.metadata names whoever the
        # caller chose rather than whoever the capture was authorized for.
        graph_metadata = normalize_memory_quality_metadata(
            stamp_memory_scope_metadata(
                request.metadata,
                memory_scope=request.memory_scope,
                scope_key=request.scope_key,
                principal_id=request.principal_id,
            )
        )
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
            mutation_receipt=mutation_receipt,
        )


def _optional_str(value: object) -> str | None:
    return str(value) if value else None
