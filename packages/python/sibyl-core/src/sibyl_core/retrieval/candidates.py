"""Shared retrieval candidate contracts across search surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class CandidateKind(StrEnum):
    RAW_MEMORY = "raw_memory"
    NODE = "node"
    EDGE = "edge"
    EPISODE = "episode"
    DOCUMENT = "document"


class CandidateSignal(StrEnum):
    RAW_LEXICAL = "raw_lexical"
    FULLTEXT = "fulltext"
    VECTOR = "vector"
    GRAPH_EXPANSION = "graph_expansion"
    HYBRID = "hybrid"
    DOCUMENT_VECTOR = "document_vector"
    DOCUMENT_FULLTEXT = "document_fulltext"


@dataclass(frozen=True, slots=True)
class CandidateScope:
    organization_id: str | None = None
    project_id: str | None = None
    memory_scope: str | None = None
    scope_key: str | None = None
    principal_id: str | None = None
    visibility: str | None = None
    policy_reason: str | None = None

    def as_metadata(self) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for key, value in (
            ("candidate_organization_id", self.organization_id),
            ("candidate_project_id", self.project_id),
            ("candidate_memory_scope", self.memory_scope),
            ("candidate_scope_key", self.scope_key),
            ("candidate_principal_id", self.principal_id),
            ("candidate_visibility", self.visibility),
            ("candidate_policy_reason", self.policy_reason),
        ):
            if value:
                metadata[key] = value
        return metadata


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    id: str
    type: str
    name: str
    content: str
    score: float
    source: str | None
    metadata: dict[str, Any]
    result_origin: str = "graph"
    project_id: str | None = None
    created_at: datetime | None = None
    policy_reason: str = "authorized"
    visibility: str | None = None
    kind: CandidateKind | str | None = None
    retrieval_signals: tuple[str, ...] = ()
    scope: CandidateScope | None = None
    source_revision: int | None = None

    def contract_metadata(self) -> dict[str, Any]:
        return candidate_contract_metadata(
            kind=self.kind,
            signals=self.retrieval_signals,
            scope=self.scope,
            metadata=self.metadata,
        )


@dataclass(frozen=True, slots=True)
class VectorCandidateFetch:
    node_candidates: list[RetrievalCandidate]
    edge_candidates: list[RetrievalCandidate]
    requested: bool
    attempted: bool
    failures: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def candidate_count(self) -> int:
        return len(self.node_candidates) + len(self.edge_candidates)

    @property
    def degraded(self) -> bool:
        return bool(self.failures or self.reason in {"embedding_failed", "invalid_embedding"})

    @property
    def status(self) -> str:
        if not self.requested:
            return "not_requested"
        if self.reason is not None:
            return self.reason
        if self.failures and self.candidate_count:
            return "partial"
        if self.failures:
            return "query_failed"
        if not self.attempted:
            return "unavailable"
        if self.candidate_count == 0:
            return "empty"
        return "ok"

    def as_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "vector_status": self.status,
            "vector_requested": self.requested,
            "vector_attempted": self.attempted,
            "vector_degraded": self.degraded,
            "vector_candidate_count": self.candidate_count,
        }
        if self.failures:
            metadata["vector_failures"] = list(self.failures)
        return metadata


def candidate_contract_metadata(
    *,
    kind: CandidateKind | str | None,
    signals: object = (),
    scope: CandidateScope | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    if kind is not None:
        merged["candidate_kind"] = str(getattr(kind, "value", kind))
    normalized_signals = _normalized_signals(signals or merged.get("retrieval_signals") or ())
    if normalized_signals:
        merged["retrieval_signals"] = normalized_signals
    if scope is not None:
        merged.update(scope.as_metadata())
    return merged


def merge_candidate_signals(
    *signal_groups: object,
) -> list[str]:
    signals: list[str] = []
    for group in signal_groups:
        signals.extend(_normalized_signals(group or ()))
    return list(dict.fromkeys(signals))


def _normalized_signals(signals: tuple[str, ...] | list[str] | object) -> list[str]:
    if not isinstance(signals, list | tuple):
        return []
    return [str(getattr(signal, "value", signal)) for signal in signals if str(signal)]
