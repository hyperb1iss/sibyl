"""Native embedding contracts for Surreal-backed graph paths."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Protocol, cast

import structlog

from sibyl_core.embeddings.gemini import (
    build_gemini_contents,
    format_gemini_embedding_text,
)
from sibyl_core.models.entities import Entity, Relationship

log = structlog.get_logger()

type NativeEmbeddingInputKind = Literal["query", "document"]
type NativeEmbeddingProviderName = Literal["openai", "gemini"]


@dataclass(frozen=True, slots=True)
class NativeEmbeddingMetadata:
    provider: str
    model: str
    dimensions: int
    cache_namespace: str
    tokenizer_estimate_method: str
    text_version: str = "native-graph-v1"
    normalize: bool = True
    input_kind_sensitive: bool = True

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


class OpenAINativeEmbeddingProvider:
    def __init__(
        self,
        *,
        metadata: NativeEmbeddingMetadata,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._metadata = replace(metadata, input_kind_sensitive=False)
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key or None)
        self._client = client

    @property
    def metadata(self) -> NativeEmbeddingMetadata:
        return self._metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: NativeEmbeddingInputKind = "document",
    ) -> list[list[float]]:
        del input_kind
        if not texts:
            return []
        result = await self._client.embeddings.create(
            model=self.metadata.model,
            input=list(texts),
            dimensions=self.metadata.dimensions,
        )
        return [list(item.embedding) for item in result.data]


class GeminiNativeEmbeddingProvider:
    def __init__(
        self,
        *,
        metadata: NativeEmbeddingMetadata,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._metadata = metadata
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self._client = client

    @property
    def metadata(self) -> NativeEmbeddingMetadata:
        return self._metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: NativeEmbeddingInputKind = "document",
    ) -> list[list[float]]:
        if not texts:
            return []
        from google.genai import types

        kind = "query" if input_kind == "query" else "document"
        formatted = [
            format_gemini_embedding_text(
                text,
                model=self.metadata.model,
                kind=kind,
            )
            for text in texts
        ]
        result = await self._client.aio.models.embed_content(
            model=self.metadata.model,
            contents=cast(Any, build_gemini_contents(formatted)),
            config=types.EmbedContentConfig(output_dimensionality=self.metadata.dimensions),
        )
        if not result.embeddings:
            raise ValueError("No embeddings returned from Gemini API")
        embeddings = []
        for embedding in result.embeddings:
            if not embedding.values:
                raise ValueError("Empty embedding returned from Gemini API")
            embeddings.append(list(embedding.values))
        return embeddings


class CachedNativeEmbeddingProvider:
    def __init__(
        self,
        provider: NativeEmbeddingProvider,
        *,
        max_size: int = 1000,
        stats: dict[str, int] | None = None,
    ) -> None:
        self._provider = provider
        self._max_size = max_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._pending: dict[str, asyncio.Future[list[float]]] = {}
        self._lock = asyncio.Lock()
        self._stats = stats

    @property
    def metadata(self) -> NativeEmbeddingMetadata:
        return self._provider.metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: NativeEmbeddingInputKind = "document",
    ) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[tuple[int, str, str, asyncio.Future[list[float]]]] = []
        pending: list[tuple[int, asyncio.Future[list[float]]]] = []

        async with self._lock:
            for index, text in enumerate(texts):
                cache_key = native_embedding_cache_key(
                    self.metadata,
                    text,
                    input_kind=input_kind,
                )
                if cache_key in self._cache:
                    _increment_stat(self._stats, "hits")
                    self._cache.move_to_end(cache_key)
                    results[index] = self._cache[cache_key]
                elif cache_key in self._pending:
                    pending.append((index, self._pending[cache_key]))
                else:
                    future = asyncio.get_running_loop().create_future()
                    self._pending[cache_key] = future
                    missing.append((index, text, cache_key, future))

        if missing:
            _increment_stat(self._stats, "misses", len(missing))
            try:
                new_embeddings = await self._provider.embed_texts(
                    [text for _, text, _, _ in missing],
                    input_kind=input_kind,
                )
                if len(new_embeddings) != len(missing):
                    raise ValueError(
                        "embedding provider returned "
                        f"{len(new_embeddings)} vectors for {len(missing)} texts"
                    )
            except asyncio.CancelledError:
                async with self._lock:
                    for _index, _text, cache_key, future in missing:
                        self._pending.pop(cache_key, None)
                        _set_future_exception(future, asyncio.CancelledError())
                raise
            except Exception as exc:
                async with self._lock:
                    for _index, _text, cache_key, future in missing:
                        self._pending.pop(cache_key, None)
                        _set_future_exception(future, exc)
                raise

            async with self._lock:
                for (index, _text, cache_key, future), embedding in zip(
                    missing,
                    new_embeddings,
                    strict=True,
                ):
                    vector = [float(value) for value in embedding]
                    self._cache[cache_key] = vector
                    self._cache.move_to_end(cache_key)
                    self._pending.pop(cache_key, None)
                    if not future.done():
                        future.set_result(vector)
                    results[index] = vector
                while len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)
                    _increment_stat(self._stats, "evictions")

        if pending:
            pending_embeddings = await asyncio.gather(*(future for _index, future in pending))
            for (index, _future), embedding in zip(
                pending,
                pending_embeddings,
                strict=True,
            ):
                results[index] = embedding

        if any(result is None for result in results):
            raise ValueError("embedding cache did not resolve every requested text")
        return cast(list[list[float]], results)

    def cache_size(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()


def create_native_embedding_provider(
    *,
    provider: NativeEmbeddingProviderName,
    model: str,
    dimensions: int,
    cache_namespace: str,
    api_key: str | None = None,
    client: Any | None = None,
    max_cache_size: int = 1000,
    tokenizer_estimate_method: str = "provider-default",
) -> NativeEmbeddingProvider:
    metadata = NativeEmbeddingMetadata(
        provider=provider,
        model=model,
        dimensions=dimensions,
        cache_namespace=cache_namespace,
        tokenizer_estimate_method=tokenizer_estimate_method,
    )
    if provider == "gemini":
        return CachedNativeEmbeddingProvider(
            GeminiNativeEmbeddingProvider(
                metadata=metadata,
                api_key=api_key,
                client=client,
            ),
            max_size=max_cache_size,
        )
    return CachedNativeEmbeddingProvider(
        OpenAINativeEmbeddingProvider(
            metadata=metadata,
            api_key=api_key,
            client=client,
        ),
        max_size=max_cache_size,
    )


def configured_native_embedding_provider() -> NativeEmbeddingProvider | None:
    from sibyl_core.config import settings

    provider = (
        os.getenv("SIBYL_GRAPH_EMBEDDING_PROVIDER") or settings.graph_embedding_provider
    ).strip()
    if provider not in ("gemini", "openai"):
        raise ValueError(f"unsupported native graph embedding provider: {provider}")

    model = os.getenv("SIBYL_GRAPH_EMBEDDING_MODEL", "").strip()
    if not model:
        if provider == "gemini" and settings.graph_embedding_model == "text-embedding-3-small":
            model = "gemini-embedding-2"
        else:
            model = settings.graph_embedding_model

    dimensions_raw = os.getenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "").strip()
    dimensions = int(dimensions_raw) if dimensions_raw else settings.graph_embedding_dimensions

    if provider == "gemini":
        api_key = (
            os.getenv("SIBYL_GEMINI_API_KEY", "")
            or os.getenv("GEMINI_API_KEY", "")
            or os.getenv("GOOGLE_API_KEY", "")
            or settings.gemini_api_key.get_secret_value()
        )
    else:
        api_key = (
            os.getenv("SIBYL_OPENAI_API_KEY", "")
            or os.getenv("OPENAI_API_KEY", "")
            or settings.openai_api_key.get_secret_value()
        )

    if not api_key:
        log.info("native_graph_embeddings_disabled", provider=provider, reason="missing_key")
        return None

    return create_native_embedding_provider(
        provider=cast(NativeEmbeddingProviderName, provider),
        model=model,
        dimensions=dimensions,
        cache_namespace="graph",
        api_key=api_key,
        max_cache_size=2000,
    )


def native_embedding_cache_key(
    metadata: NativeEmbeddingMetadata,
    text: str,
    *,
    input_kind: NativeEmbeddingInputKind,
) -> str:
    kind_bucket = input_kind if metadata.input_kind_sensitive else "shared"
    payload = "\x1f".join(
        (
            metadata.cache_namespace,
            metadata.provider,
            metadata.model,
            str(metadata.dimensions),
            metadata.text_version,
            metadata.tokenizer_estimate_method,
            f"normalize={metadata.normalize}",
            f"input_kind_sensitive={metadata.input_kind_sensitive}",
            kind_bucket,
            text.strip(),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


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


def _increment_stat(
    stats: dict[str, int] | None,
    key: str,
    amount: int = 1,
) -> None:
    if stats is not None:
        stats[key] = stats.get(key, 0) + amount


def _set_future_exception(
    future: asyncio.Future[list[float]],
    exc: BaseException,
) -> None:
    if future.done():
        return
    future.set_exception(exc)
    with suppress(BaseException):
        future.exception()


__all__ = [
    "CachedNativeEmbeddingProvider",
    "DeterministicNativeEmbeddingProvider",
    "GeminiNativeEmbeddingProvider",
    "NativeEmbeddingInputKind",
    "NativeEmbeddingMetadata",
    "NativeEmbeddingProvider",
    "NativeEmbeddingProviderName",
    "OpenAINativeEmbeddingProvider",
    "configured_native_embedding_provider",
    "create_native_embedding_provider",
    "native_embedding_cache_key",
    "native_entity_embedding_text",
    "native_relationship_embedding_text",
]
