"""Cohere Embed v4 on Amazon Bedrock.

Requests go to bedrock-runtime ``InvokeModel`` over an async httpx client and
sign with the same settings and AWS credential chain as the Claude provider,
so embedding throughput scales with async concurrency instead of a thread
pool. Limits follow the Cohere Embed v4 model card
(https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-embed-v4.html):
at most 96 texts per call, about 20 MB per request, and up to about 128K
tokens per input.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import weakref
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx

from sibyl_core.ai.bedrock import (
    COHERE_EMBED_V4_DIMENSIONS,
    BedrockConfigError,
    BedrockSettings,
    arn_model_id,
    bedrock_embedding_model_id,
    is_arn,
    remove_geo_prefix,
    require_botocore,
    resolve_bedrock_settings,
    sigv4_headers,
)

if TYPE_CHECKING:
    from sibyl_core.embeddings.providers import EmbeddingInputKind, EmbeddingMetadata

COHERE_EMBED_V4_PREFIX = "cohere.embed-v4"
#: Per-call and per-request limits from the Cohere Embed v4 model card. The
#: byte budget leaves headroom under the documented ~20 MB payload cap.
COHERE_EMBED_MAX_TEXTS = 96
COHERE_EMBED_MAX_REQUEST_BYTES = 16_000_000
#: Cohere rejects an empty string with "Invalid parameter combination".
_EMPTY_TEXT = "[empty]"

#: Retries on throttling and transient faults, shaped like botocore's
#: ``standard`` mode (capped exponential backoff with full jitter). This is
#: recovery from a transient error, never a throughput limit.
BEDROCK_EMBED_MAX_ATTEMPTS = 5
_BACKOFF_CAP_SECONDS = 20.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_ERROR_TYPES = (
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
    "InternalServerException",
)
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=None)
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=100)

_INPUT_TYPES = {"query": "search_query", "document": "search_document"}


class BedrockEmbeddingError(RuntimeError):
    """Raised when Bedrock refuses or garbles an embedding request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_cohere_embedding(model: str, dimensions: int) -> None:
    """Fail at configuration time on a model or size Cohere v4 cannot serve.

    An inference-profile ARN is checked by the model it names. An opaque ARN
    (application inference profile or provisioned throughput) hides the model,
    so only its dimensions are checked.
    """
    named = arn_model_id(model) or (None if is_arn(model) else model)
    if named is not None and not remove_geo_prefix(named).startswith(COHERE_EMBED_V4_PREFIX):
        raise BedrockConfigError(
            f"Bedrock embeddings support Cohere Embed v4 (cohere.embed-v4:0), not {model!r}"
        )
    if dimensions not in COHERE_EMBED_V4_DIMENSIONS:
        supported = ", ".join(str(value) for value in COHERE_EMBED_V4_DIMENSIONS)
        raise BedrockConfigError(
            f"Cohere Embed v4 on Bedrock produces {supported} dimensions, not {dimensions}; "
            "set the embedding dimensions to one of those"
        )


def cohere_embedding_batches(texts: Sequence[str]) -> list[list[str]]:
    """Split texts into requests inside Cohere v4's item and payload limits."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for text in texts:
        size = len(json.dumps(text)) + 2
        if current and (
            len(current) >= COHERE_EMBED_MAX_TEXTS
            or current_bytes + size > COHERE_EMBED_MAX_REQUEST_BYTES
        ):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(text)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


class BedrockEmbeddingProvider:
    """Embeds through Cohere Embed v4 on Bedrock, tagging vectors ``bedrock``.

    ``metadata.model`` records the scope-free model ID, so moving between
    ``us.`` and ``global.`` profiles does not look like a model change to the
    re-embed sweep; the vectors come from the same model either way.
    """

    def __init__(
        self,
        *,
        metadata: EmbeddingMetadata,
        settings: BedrockSettings | None = None,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = BEDROCK_EMBED_MAX_ATTEMPTS,
    ) -> None:
        validate_cohere_embedding(metadata.model, metadata.dimensions)
        self._wire_model = metadata.model
        self._metadata = replace(
            metadata,
            provider="bedrock",
            model=remove_geo_prefix(arn_model_id(metadata.model) or metadata.model),
            input_kind_sensitive=True,
        )
        self._settings = settings or resolve_bedrock_settings()
        if not self._settings.api_key:
            require_botocore()
        self._client = client
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
            weakref.WeakKeyDictionary()
        )
        self._max_attempts = max(1, max_attempts)
        self._usage_lock = threading.Lock()
        self._usage: dict[str, int | float] = {
            "requests": 0,
            "inputs": 0,
            "prompt_tokens": 0,
            "total_tokens": 0,
            "cost_reported_requests": 0,
            "cost_usd": 0.0,
        }

    @property
    def metadata(self) -> EmbeddingMetadata:
        return self._metadata

    @property
    def wire_model_id(self) -> str:
        return bedrock_embedding_model_id(self._wire_model, self._settings)

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: EmbeddingInputKind = "document",
    ) -> list[list[float]]:
        if not texts:
            return []
        input_type = _INPUT_TYPES[input_kind]
        prepared = [text.strip() or _EMPTY_TEXT for text in texts]
        batches = cohere_embedding_batches(prepared)
        results = await asyncio.gather(*(self._embed_batch(batch, input_type) for batch in batches))
        return [vector for batch in results for vector in batch]

    def usage_snapshot(self) -> dict[str, str | int | float]:
        with self._usage_lock:
            usage = dict(self._usage)
        return {"provider": self.metadata.provider, "model": self.metadata.model, **usage}

    async def _embed_batch(self, texts: list[str], input_type: str) -> list[list[float]]:
        body = json.dumps(
            {
                "texts": texts,
                "input_type": input_type,
                "embedding_types": ["float"],
                "output_dimension": self.metadata.dimensions,
                "truncate": "RIGHT",
            }
        ).encode()
        response = await self._invoke(body)
        payload = response.json()
        vectors = _float_embeddings(payload)
        if len(vectors) != len(texts):
            raise BedrockEmbeddingError(
                f"Bedrock returned {len(vectors)} embeddings for {len(texts)} texts"
            )
        for vector in vectors:
            if len(vector) != self.metadata.dimensions:
                raise BedrockEmbeddingError(
                    f"Bedrock returned a {len(vector)}-dimension embedding, "
                    f"expected {self.metadata.dimensions}"
                )
        self._record_usage(response, input_count=len(texts))
        return [[float(value) for value in vector] for vector in vectors]

    async def _invoke(self, body: bytes) -> httpx.Response:
        url = (
            f"https://bedrock-runtime.{self._settings.region}.amazonaws.com"
            f"/model/{quote(self.wire_model_id, safe=':')}/invoke"
        )
        client = self._http_client()
        for attempt in range(1, self._max_attempts + 1):
            headers = await self._headers(url, body)
            try:
                response = await client.post(url, content=body, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == self._max_attempts:
                    raise
                await asyncio.sleep(_backoff(attempt))
                continue
            if response.is_success:
                return response
            if attempt < self._max_attempts and _retryable(response):
                await asyncio.sleep(_backoff(attempt))
                continue
            raise BedrockEmbeddingError(
                f"Bedrock embedding request failed with HTTP {response.status_code}: "
                f"{_error_message(response)}",
                status_code=response.status_code,
            )
        raise AssertionError("unreachable")

    async def _headers(self, url: str, body: bytes) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self._settings.api_key:
            return {**headers, "authorization": f"Bearer {self._settings.api_key}"}
        # Refreshing credentials can hit STS or instance metadata; keep it off the loop.
        return await asyncio.to_thread(
            sigv4_headers,
            self._settings,
            method="POST",
            url=url,
            headers=headers,
            body=body,
        )

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(timeout=_TIMEOUT, limits=_LIMITS)
            self._clients[loop] = client
        return client

    def _record_usage(self, response: httpx.Response, *, input_count: int) -> None:
        from sibyl_core.embeddings.providers import (
            _embedding_usage_collector,
            _record_usage_totals,
        )

        raw_tokens = response.headers.get("x-amzn-bedrock-input-token-count", "")
        tokens = int(raw_tokens) if raw_tokens.isdigit() else 0
        with self._usage_lock:
            self._usage["requests"] += 1
            self._usage["inputs"] += input_count
            self._usage["prompt_tokens"] += tokens
            self._usage["total_tokens"] += tokens
        collector = _embedding_usage_collector.get()
        if collector is not None:
            _record_usage_totals(
                collector,
                provider=self.metadata.provider,
                model=self.metadata.model,
                input_count=input_count,
                prompt_tokens=tokens,
                total_tokens=tokens,
                cost=None,
            )


def _float_embeddings(payload: Any) -> list[list[float]]:
    """Read float vectors from either Cohere v4 response shape."""
    embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
    if isinstance(embeddings, dict):
        embeddings = embeddings.get("float")
    if not isinstance(embeddings, list):
        raise BedrockEmbeddingError("Bedrock returned no float embeddings")
    return cast(list[list[float]], embeddings)


def _retryable(response: httpx.Response) -> bool:
    if response.status_code in _RETRYABLE_STATUS:
        return True
    error_type = response.headers.get("x-amzn-errortype", "")
    return error_type.startswith(_RETRYABLE_ERROR_TYPES)


def _backoff(attempt: int) -> float:
    return random.uniform(0.0, min(_BACKOFF_CAP_SECONDS, 2.0 ** (attempt - 1)))


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.reason_phrase or "no message"
    message = payload.get("message") if isinstance(payload, dict) else None
    return str(message or response.reason_phrase or "no message")[:300]


__all__ = [
    "BEDROCK_EMBED_MAX_ATTEMPTS",
    "COHERE_EMBED_MAX_REQUEST_BYTES",
    "COHERE_EMBED_MAX_TEXTS",
    "COHERE_EMBED_V4_DIMENSIONS",
    "BedrockEmbeddingError",
    "BedrockEmbeddingProvider",
    "cohere_embedding_batches",
    "validate_cohere_embedding",
]
