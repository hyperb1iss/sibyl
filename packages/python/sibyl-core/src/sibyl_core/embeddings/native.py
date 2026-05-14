"""Native embedding contracts for Surreal-backed graph paths."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from sibyl_core.models.entities import Entity, Relationship

type NativeEmbeddingInputKind = Literal["query", "document"]


@dataclass(frozen=True, slots=True)
class NativeEmbeddingMetadata:
    provider: str
    model: str
    dimensions: int
    cache_namespace: str
    tokenizer_estimate_method: str
    text_version: str = "native-graph-v1"
    normalize: bool = True

    def to_dict(self) -> dict[str, str | int | bool]:
        return asdict(self)


class NativeEmbeddingProvider(Protocol):
    @property
    def metadata(self) -> NativeEmbeddingMetadata: ...

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: NativeEmbeddingInputKind = "document",
    ) -> list[list[float]]: ...


class DeterministicNativeEmbeddingProvider:
    def __init__(self, metadata: NativeEmbeddingMetadata | None = None) -> None:
        self._metadata = metadata or NativeEmbeddingMetadata(
            provider="deterministic",
            model="sha256-v1",
            dimensions=8,
            cache_namespace="native-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
        if self._metadata.dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")

    @property
    def metadata(self) -> NativeEmbeddingMetadata:
        return self._metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: NativeEmbeddingInputKind = "document",
    ) -> list[list[float]]:
        return [
            _deterministic_vector(text, input_kind=input_kind, metadata=self.metadata)
            for text in texts
        ]


def native_entity_embedding_text(entity: Entity) -> str:
    parts = [
        entity.entity_type.value,
        entity.name,
        entity.description,
        entity.content,
        str(entity.metadata.get("summary") or ""),
    ]
    return "\n".join(part for part in parts if part)


def native_relationship_embedding_text(relationship: Relationship) -> str:
    fact = relationship.metadata.get("fact")
    if isinstance(fact, str) and fact.strip():
        return fact.strip()
    return (
        f"{relationship.source_id} "
        f"{relationship.relationship_type.value.lower()} "
        f"{relationship.target_id}"
    )


def _deterministic_vector(
    text: str,
    *,
    input_kind: NativeEmbeddingInputKind,
    metadata: NativeEmbeddingMetadata,
) -> list[float]:
    seed = (
        f"{metadata.cache_namespace}:{metadata.provider}:{metadata.model}:"
        f"{metadata.text_version}:{input_kind}:{text}"
    )
    values: list[float] = []
    for index in range(metadata.dimensions):
        digest = hashlib.sha256(f"{seed}:{index}".encode()).digest()
        unit = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
        values.append((unit * 2.0) - 1.0)
    if not metadata.normalize:
        return [round(value, 8) for value in values]
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return [0.0 for _ in values]
    return [round(value / norm, 8) for value in values]


__all__ = [
    "DeterministicNativeEmbeddingProvider",
    "NativeEmbeddingInputKind",
    "NativeEmbeddingMetadata",
    "NativeEmbeddingProvider",
    "native_entity_embedding_text",
    "native_relationship_embedding_text",
]
