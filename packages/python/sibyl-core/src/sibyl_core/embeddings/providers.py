"""Native embedding contracts for Surreal-backed graph paths."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import threading
import weakref
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Any, Literal, Protocol, cast

import structlog

from sibyl_core.embeddings.gemini import (
    build_gemini_contents,
    format_gemini_embedding_text,
)
from sibyl_core.models.entities import Entity, Relationship

log = structlog.get_logger()

type EmbeddingInputKind = Literal["query", "document"]
type EmbeddingProviderName = Literal["openai", "gemini"]
type _ConfiguredProviderCacheKey = tuple[EmbeddingProviderName, str, int, str]

_OPENAI_EMBEDDING_INPUT_MAX_TOKENS = 6000
_OPENAI_EMBEDDING_REQUEST_MAX_TOKENS = 240_000
_OPENAI_EMBEDDING_REQUEST_MAX_ITEMS = 2000
_OPENAI_EMBEDDING_FALLBACK_MAX_CHARS = 12_000
_OPENAI_EMBEDDING_EMPTY_TEXT = "[empty]"
_OPENAI_EMBEDDING_TRUNCATION_MARKER = "\n...[truncated for embedding]...\n"
_configured_provider_cache: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[_ConfiguredProviderCacheKey, EmbeddingProvider],
] = weakref.WeakKeyDictionary()
_configured_provider_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class EmbeddingMetadata:
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


class EmbeddingProvider(Protocol):
    @property
    def metadata(self) -> EmbeddingMetadata: ...

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: EmbeddingInputKind = "document",
    ) -> list[list[float]]: ...


class DeterministicEmbeddingProvider:
    def __init__(self, metadata: EmbeddingMetadata | None = None) -> None:
        self._metadata = metadata or EmbeddingMetadata(
            provider="deterministic",
            model="sha256-v1",
            dimensions=8,
            cache_namespace="native-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
        if self._metadata.dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: EmbeddingInputKind = "document",
    ) -> list[list[float]]:
        return [
            _deterministic_vector(text, input_kind=input_kind, metadata=self.metadata)
            for text in texts
        ]


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        *,
        metadata: EmbeddingMetadata,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._metadata = replace(metadata, input_kind_sensitive=False)
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=api_key or None)
        self._client = client

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: EmbeddingInputKind = "document",
    ) -> list[list[float]]:
        del input_kind
        if not texts:
            return []
        prepared = [
            _prepare_openai_embedding_text(text, model=self.metadata.model) for text in texts
        ]
        embeddings: list[list[float]] = []
        for input_batch in _openai_embedding_batches(
            [text for text, _token_count in prepared],
            [token_count for _text, token_count in prepared],
        ):
            result = await self._client.embeddings.create(
                model=self.metadata.model,
                input=input_batch,
                dimensions=self.metadata.dimensions,
            )
            embeddings.extend([list(item.embedding) for item in result.data])
        return embeddings


class GeminiEmbeddingProvider:
    def __init__(
        self,
        *,
        metadata: EmbeddingMetadata,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._metadata = metadata
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self._client = client

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: EmbeddingInputKind = "document",
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


class CachedEmbeddingProvider:
    def __init__(
        self,
        provider: EmbeddingProvider,
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
    def metadata(self) -> EmbeddingMetadata:
        return self._provider.metadata

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: EmbeddingInputKind = "document",
    ) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[tuple[int, str, str, asyncio.Future[list[float]]]] = []
        pending: list[tuple[int, asyncio.Future[list[float]]]] = []

        async with self._lock:
            for index, text in enumerate(texts):
                cache_key = embedding_cache_key(
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


def create_embedding_provider(
    *,
    provider: EmbeddingProviderName,
    model: str,
    dimensions: int,
    cache_namespace: str,
    api_key: str | None = None,
    client: Any | None = None,
    max_cache_size: int = 1000,
    tokenizer_estimate_method: str = "provider-default",
) -> EmbeddingProvider:
    metadata = EmbeddingMetadata(
        provider=provider,
        model=model,
        dimensions=dimensions,
        cache_namespace=cache_namespace,
        tokenizer_estimate_method=tokenizer_estimate_method,
    )
    if provider == "gemini":
        return CachedEmbeddingProvider(
            GeminiEmbeddingProvider(
                metadata=metadata,
                api_key=api_key,
                client=client,
            ),
            max_size=max_cache_size,
        )
    return CachedEmbeddingProvider(
        OpenAIEmbeddingProvider(
            metadata=metadata,
            api_key=api_key,
            client=client,
        ),
        max_size=max_cache_size,
    )


def configured_embedding_provider() -> EmbeddingProvider | None:
    from sibyl_core.config import settings

    dimensions_raw = os.getenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "").strip()
    dimensions = int(dimensions_raw) if dimensions_raw else settings.graph_embedding_dimensions

    if os.getenv("SIBYL_MOCK_LLM", "").strip().lower() in {"1", "true", "yes", "on"}:
        return DeterministicEmbeddingProvider(
            EmbeddingMetadata(
                provider="deterministic",
                model="mock-llm-v1",
                dimensions=dimensions,
                cache_namespace="graph-mock",
                tokenizer_estimate_method="sha256",
            )
        )

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
        log.info("graph_embeddings_disabled", provider=provider, reason="missing_key")
        return None

    provider_name = cast(EmbeddingProviderName, provider)
    cache_entry = _configured_provider_cache_entry(
        provider=provider_name,
        model=model,
        dimensions=dimensions,
        api_key=api_key,
    )
    if cache_entry is None:
        return create_embedding_provider(
            provider=provider_name,
            model=model,
            dimensions=dimensions,
            cache_namespace="graph",
            api_key=api_key,
            max_cache_size=2000,
        )

    loop, cache_key = cache_entry
    with _configured_provider_lock:
        loop_cache = _configured_provider_cache.setdefault(loop, {})
        cached = loop_cache.get(cache_key)
        if cached is not None:
            return cached
        created = create_embedding_provider(
            provider=provider_name,
            model=model,
            dimensions=dimensions,
            cache_namespace="graph",
            api_key=api_key,
            max_cache_size=2000,
        )
        loop_cache[cache_key] = created
        return created


def _configured_provider_cache_entry(
    *,
    provider: EmbeddingProviderName,
    model: str,
    dimensions: int,
    api_key: str,
) -> tuple[asyncio.AbstractEventLoop, _ConfiguredProviderCacheKey] | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    api_key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
    return loop, (provider, model, dimensions, api_key_fingerprint)


def _prepare_openai_embedding_text(text: str, *, model: str) -> tuple[str, int]:
    cleaned = text.strip()
    if not cleaned:
        return _OPENAI_EMBEDDING_EMPTY_TEXT, 1

    encoding = _openai_embedding_encoding(model)
    if encoding is None:
        prepared = _truncate_openai_embedding_text_chars(cleaned)
        return prepared, _estimate_openai_token_count(prepared)

    tokens = _encode_openai_embedding_text(encoding, cleaned)
    if len(tokens) <= _OPENAI_EMBEDDING_INPUT_MAX_TOKENS:
        return cleaned, len(tokens)

    marker_tokens = _encode_openai_embedding_text(
        encoding,
        _OPENAI_EMBEDDING_TRUNCATION_MARKER,
    )
    budget = max(_OPENAI_EMBEDDING_INPUT_MAX_TOKENS - len(marker_tokens), 1)
    head_count = max(1, budget * 3 // 4)
    tail_count = max(1, budget - head_count)
    truncated_tokens = [
        *tokens[:head_count],
        *marker_tokens,
        *tokens[-tail_count:],
    ]
    return encoding.decode(truncated_tokens), len(truncated_tokens)


@lru_cache(maxsize=16)
def _openai_embedding_encoding(model: str) -> Any | None:
    try:
        import tiktoken
    except ImportError:
        return None

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def _encode_openai_embedding_text(encoding: Any, text: str) -> list[int]:
    return list(encoding.encode(text, disallowed_special=()))


def _truncate_openai_embedding_text_chars(text: str) -> str:
    if len(text) <= _OPENAI_EMBEDDING_FALLBACK_MAX_CHARS:
        return text
    marker = _OPENAI_EMBEDDING_TRUNCATION_MARKER
    budget = max(_OPENAI_EMBEDDING_FALLBACK_MAX_CHARS - len(marker), 1)
    head_chars = max(1, budget * 3 // 4)
    tail_chars = max(1, budget - head_chars)
    return f"{text[:head_chars].rstrip()}{marker}{text[-tail_chars:].lstrip()}"


def _estimate_openai_token_count(text: str) -> int:
    return max(math.ceil(len(text) / 4), 1)


def _openai_embedding_batches(
    texts: Sequence[str],
    token_counts: Sequence[int],
) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for text, token_count in zip(texts, token_counts, strict=True):
        next_count = max(token_count, 1)
        if current and (
            current_tokens + next_count > _OPENAI_EMBEDDING_REQUEST_MAX_TOKENS
            or len(current) >= _OPENAI_EMBEDDING_REQUEST_MAX_ITEMS
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(text)
        current_tokens += next_count
    if current:
        batches.append(current)
    return batches


def embedding_cache_key(
    metadata: EmbeddingMetadata,
    text: str,
    *,
    input_kind: EmbeddingInputKind,
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


def entity_embedding_text(entity: Entity) -> str:
    parts = [
        entity.entity_type.value,
        entity.name,
        entity.description,
        entity.content,
        str(entity.metadata.get("summary") or ""),
    ]
    return "\n".join(part for part in parts if part)


def relationship_embedding_text(relationship: Relationship) -> str:
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
    input_kind: EmbeddingInputKind,
    metadata: EmbeddingMetadata,
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
    "CachedEmbeddingProvider",
    "DeterministicEmbeddingProvider",
    "EmbeddingInputKind",
    "EmbeddingMetadata",
    "EmbeddingProvider",
    "EmbeddingProviderName",
    "GeminiEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "configured_embedding_provider",
    "create_embedding_provider",
    "embedding_cache_key",
    "entity_embedding_text",
    "relationship_embedding_text",
]
