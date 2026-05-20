import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl_core.backends.surreal import SurrealDriver
from sibyl_core.config import CoreConfig
from sibyl_core.embeddings import native as native_module
from sibyl_core.embeddings.native import (
    CachedNativeEmbeddingProvider,
    DeterministicNativeEmbeddingProvider,
    NativeEmbeddingInputKind,
    NativeEmbeddingMetadata,
    OpenAINativeEmbeddingProvider,
    configured_native_embedding_provider,
    native_embedding_cache_key,
)
from sibyl_core.errors import GraphConnectionError
from sibyl_core.graph import client as _graph_client
from sibyl_core.graph.client import GraphClient


def test_core_config_uses_store_env(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_STORE", "surreal")

    settings = CoreConfig(_env_file=None)

    assert settings.store == "surreal"


def test_core_config_reads_surreal_token(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_SURREAL_TOKEN", "token-123")

    settings = CoreConfig(_env_file=None)

    assert settings.surreal_token.get_secret_value() == "token-123"


def test_core_config_ignores_removed_graph_backend_alias(monkeypatch) -> None:
    monkeypatch.delenv("SIBYL_STORE", raising=False)
    monkeypatch.setenv("SIBYL_GRAPH_BACKEND", "falkordb")

    settings = CoreConfig(_env_file=None)

    assert settings.store == "surreal"


@pytest.mark.asyncio
async def test_connect_dispatches_to_surreal_runtime(monkeypatch) -> None:
    client = GraphClient()
    connect_surreal = AsyncMock()

    monkeypatch.setattr(_graph_client.settings, "store", "surreal")
    monkeypatch.setattr(client, "_connect_surreal", connect_surreal)

    await client.connect()

    connect_surreal.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_connect_ignores_removed_legacy_runtime(monkeypatch) -> None:
    client = GraphClient()
    connect_surreal = AsyncMock()

    monkeypatch.setattr(_graph_client.settings, "store", "legacy")
    monkeypatch.setattr(client, "_connect_surreal", connect_surreal)

    await client.connect()

    connect_surreal.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_connect_surreal_constructs_surreal_driver(monkeypatch) -> None:
    client = GraphClient()
    llm_client = object()
    embedder = object()

    monkeypatch.setattr(_graph_client.settings, "surreal_url", "memory://")
    monkeypatch.setattr(_graph_client.settings, "surreal_data_dir", "")
    monkeypatch.setattr(_graph_client.settings, "surreal_username", "")
    monkeypatch.setattr(_graph_client.settings, "surreal_namespace_prefix", "tenant_")
    monkeypatch.setattr(_graph_client.settings, "surreal_database", "graph_test")
    monkeypatch.setattr(
        _graph_client.settings,
        "surreal_password",
        _graph_client.settings.surreal_password,
    )
    monkeypatch.setattr(
        _graph_client.settings,
        "surreal_token",
        _graph_client.settings.surreal_token,
    )
    monkeypatch.setattr(client, "_create_llm_client", lambda: llm_client)
    monkeypatch.setattr(client, "_create_embedder", lambda: embedder)
    await client._connect_surreal()

    assert isinstance(client.driver, SurrealDriver)
    assert client.driver.namespace_prefix == "tenant_"
    assert client.driver.default_database == "graph_test"
    assert client.client.llm_client is llm_client
    assert client.client.embedder is embedder
    assert client.is_connected is True


def test_create_embedder_uses_gemini_runtime_env(monkeypatch) -> None:
    client = GraphClient()

    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "gemini")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")

    embedder = client._create_embedder()

    assert embedder.config.embedding_model == "gemini-embedding-2"
    assert embedder.config.embedding_dim == 768
    assert embedder.config.api_key == "gemini-key"
    assert embedder.provider.metadata.provider == "gemini"
    assert embedder.provider.metadata.cache_namespace == "graph"


def test_create_embedder_uses_openai_native_provider(monkeypatch) -> None:
    client = GraphClient()

    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "1024")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    embedder = client._create_embedder()

    assert embedder.config.provider == "openai"
    assert embedder.config.embedding_model == "text-embedding-3-small"
    assert embedder.config.embedding_dim == 1024
    assert embedder.provider.metadata.provider == "openai"
    assert embedder.provider.metadata.input_kind_sensitive is False


def test_native_embedding_cache_key_includes_provider_shape() -> None:
    metadata = NativeEmbeddingMetadata(
        provider="openai",
        model="text-embedding-3-small",
        dimensions=1024,
        cache_namespace="graph",
        tokenizer_estimate_method="provider-default",
    )
    base_key = native_embedding_cache_key(metadata, "same text", input_kind="query")

    assert base_key != native_embedding_cache_key(
        metadata,
        "same text",
        input_kind="document",
    )
    assert base_key != native_embedding_cache_key(
        NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-large",
            dimensions=1024,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        ),
        "same text",
        input_kind="query",
    )
    shared_kind_metadata = NativeEmbeddingMetadata(
        provider="openai",
        model="text-embedding-3-small",
        dimensions=1024,
        cache_namespace="graph",
        tokenizer_estimate_method="provider-default",
        input_kind_sensitive=False,
    )
    assert native_embedding_cache_key(
        shared_kind_metadata,
        "same text",
        input_kind="query",
    ) == native_embedding_cache_key(
        shared_kind_metadata,
        "same text",
        input_kind="document",
    )
    assert base_key != native_embedding_cache_key(
        NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=1536,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        ),
        "same text",
        input_kind="query",
    )
    assert base_key != native_embedding_cache_key(
        NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=1024,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
            normalize=False,
        ),
        "same text",
        input_kind="query",
    )


def test_configured_native_embedding_provider_uses_mock_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_MOCK_LLM", "true")
    monkeypatch.setenv("SIBYL_OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "16")

    provider = configured_native_embedding_provider()

    assert isinstance(provider, DeterministicNativeEmbeddingProvider)
    assert provider.metadata.provider == "deterministic"
    assert provider.metadata.dimensions == 16


class _FakeOpenAIEmbeddingClient:
    def __init__(self) -> None:
        self.embeddings = self
        self.inputs: list[list[str]] = []

    async def create(
        self,
        *,
        model: str,
        input: list[str],
        dimensions: int,
    ) -> SimpleNamespace:
        del model
        self.inputs.append(list(input))
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1] * dimensions) for _text in input]
        )


@pytest.mark.asyncio
async def test_openai_native_provider_truncates_oversized_inputs() -> None:
    client = _FakeOpenAIEmbeddingClient()
    provider = OpenAINativeEmbeddingProvider(
        metadata=NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=3,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        ),
        client=client,
    )
    long_text = "head fact " + ("middle fact " * 12_000) + "tail fact"

    vectors = await provider.embed_texts([long_text])

    sent = client.inputs[0][0]
    assert len(sent) < len(long_text)
    assert "head fact" in sent
    assert "tail fact" in sent
    assert "truncated for embedding" in sent
    encoding = native_module._openai_embedding_encoding(provider.metadata.model)
    if encoding is not None:
        assert (
            len(native_module._encode_openai_embedding_text(encoding, sent))
            <= native_module._OPENAI_EMBEDDING_INPUT_MAX_TOKENS
        )
    assert vectors == [[0.1, 0.1, 0.1]]


@pytest.mark.asyncio
async def test_openai_native_provider_truncates_without_tokenizer(monkeypatch) -> None:
    client = _FakeOpenAIEmbeddingClient()
    provider = OpenAINativeEmbeddingProvider(
        metadata=NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=3,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        ),
        client=client,
    )
    monkeypatch.setattr(native_module, "_openai_embedding_encoding", lambda _model: None)
    long_text = "head fact " + ("middle fact " * 12_000) + "tail fact"

    await provider.embed_texts([long_text])

    sent = client.inputs[0][0]
    assert len(sent) <= native_module._OPENAI_EMBEDDING_FALLBACK_MAX_CHARS
    assert "head fact" in sent
    assert "tail fact" in sent
    assert "truncated for embedding" in sent


@pytest.mark.asyncio
async def test_openai_native_provider_batches_by_item_limit(monkeypatch) -> None:
    client = _FakeOpenAIEmbeddingClient()
    provider = OpenAINativeEmbeddingProvider(
        metadata=NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=2,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        ),
        client=client,
    )
    monkeypatch.setattr(native_module, "_OPENAI_EMBEDDING_REQUEST_MAX_ITEMS", 2)

    vectors = await provider.embed_texts(["one", "two", "three"])

    assert client.inputs == [["one", "two"], ["three"]]
    assert vectors == [[0.1, 0.1], [0.1, 0.1], [0.1, 0.1]]


@pytest.mark.asyncio
async def test_openai_native_provider_replaces_blank_inputs() -> None:
    client = _FakeOpenAIEmbeddingClient()
    provider = OpenAINativeEmbeddingProvider(
        metadata=NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=2,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        ),
        client=client,
    )

    vectors = await provider.embed_texts(["   "])

    assert client.inputs == [[native_module._OPENAI_EMBEDDING_EMPTY_TEXT]]
    assert vectors == [[0.1, 0.1]]


@pytest.mark.asyncio
async def test_configured_native_embedding_provider_reuses_loop_cache(monkeypatch) -> None:
    native_module._configured_provider_cache.clear()
    created: list[dict[str, object]] = []

    class FakeProvider:
        metadata = NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=1024,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        )

        async def embed_texts(
            self,
            texts: Sequence[str],
            *,
            input_kind: NativeEmbeddingInputKind = "document",
        ) -> list[list[float]]:
            del input_kind
            return [[0.1] * 1024 for _text in texts]

    def fake_create_native_embedding_provider(**kwargs: object) -> FakeProvider:
        created.append(kwargs)
        return FakeProvider()

    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "1024")
    monkeypatch.setenv("SIBYL_OPENAI_API_KEY", "secret")
    monkeypatch.setattr(
        native_module,
        "create_native_embedding_provider",
        fake_create_native_embedding_provider,
    )

    try:
        first = configured_native_embedding_provider()
        second = configured_native_embedding_provider()
    finally:
        native_module._configured_provider_cache.clear()

    assert first is second
    assert len(created) == 1


def test_configured_native_embedding_provider_scopes_cache_to_event_loop(monkeypatch) -> None:
    native_module._configured_provider_cache.clear()
    created: list[dict[str, object]] = []

    class FakeProvider:
        metadata = NativeEmbeddingMetadata(
            provider="openai",
            model="text-embedding-3-small",
            dimensions=1024,
            cache_namespace="graph",
            tokenizer_estimate_method="provider-default",
        )

        async def embed_texts(
            self,
            texts: Sequence[str],
            *,
            input_kind: NativeEmbeddingInputKind = "document",
        ) -> list[list[float]]:
            del input_kind
            return [[0.1] * 1024 for _text in texts]

    def fake_create_native_embedding_provider(**kwargs: object) -> FakeProvider:
        created.append(kwargs)
        return FakeProvider()

    async def configured_provider() -> FakeProvider | None:
        return configured_native_embedding_provider()

    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_DIMENSIONS", "1024")
    monkeypatch.setenv("SIBYL_OPENAI_API_KEY", "secret")
    monkeypatch.setattr(
        native_module,
        "create_native_embedding_provider",
        fake_create_native_embedding_provider,
    )

    try:
        first = asyncio.run(configured_provider())
        second = asyncio.run(configured_provider())
    finally:
        native_module._configured_provider_cache.clear()

    assert first is not second
    assert len(created) == 2


class _CountingNativeProvider:
    metadata = NativeEmbeddingMetadata(
        provider="deterministic",
        model="unit",
        dimensions=2,
        cache_namespace="graph",
        tokenizer_estimate_method="test",
    )

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: NativeEmbeddingInputKind = "document",
    ) -> list[list[float]]:
        self.calls.append((tuple(texts), input_kind))
        return [[float(len(self.calls)), float(index)] for index, _ in enumerate(texts)]


@pytest.mark.asyncio
async def test_cached_native_provider_caches_by_input_kind() -> None:
    wrapped = _CountingNativeProvider()
    provider = CachedNativeEmbeddingProvider(wrapped)

    first = await provider.embed_texts(["same text"], input_kind="query")
    second = await provider.embed_texts(["same text"], input_kind="query")
    document = await provider.embed_texts(["same text"], input_kind="document")

    assert first == second
    assert document != first
    assert wrapped.calls == [
        (("same text",), "query"),
        (("same text",), "document"),
    ]


@pytest.mark.asyncio
async def test_cached_native_provider_shares_input_kind_when_metadata_allows() -> None:
    wrapped = _CountingNativeProvider()
    wrapped.metadata = NativeEmbeddingMetadata(
        provider="openai",
        model="unit",
        dimensions=2,
        cache_namespace="graph",
        tokenizer_estimate_method="test",
        input_kind_sensitive=False,
    )
    provider = CachedNativeEmbeddingProvider(wrapped)

    first = await provider.embed_texts(["same text"], input_kind="query")
    document = await provider.embed_texts(["same text"], input_kind="document")

    assert first == document
    assert wrapped.calls == [(("same text",), "query")]


class _BlockingNativeProvider:
    metadata = NativeEmbeddingMetadata(
        provider="deterministic",
        model="unit",
        dimensions=2,
        cache_namespace="graph",
        tokenizer_estimate_method="test",
    )

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        input_kind: NativeEmbeddingInputKind = "document",
    ) -> list[list[float]]:
        self.calls.append((tuple(texts), input_kind))
        self.started.set()
        await self.release.wait()
        return [[1.0, float(index)] for index, _ in enumerate(texts)]


@pytest.mark.asyncio
async def test_cached_native_provider_dedupes_concurrent_misses() -> None:
    wrapped = _BlockingNativeProvider()
    provider = CachedNativeEmbeddingProvider(wrapped)

    first_task = asyncio.create_task(provider.embed_texts(["same text"], input_kind="query"))
    await wrapped.started.wait()
    second_task = asyncio.create_task(provider.embed_texts(["same text"], input_kind="query"))

    await asyncio.sleep(0)
    wrapped.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first == second
    assert wrapped.calls == [(("same text",), "query")]


@pytest.mark.asyncio
async def test_cached_native_provider_cleans_pending_on_cancellation() -> None:
    wrapped = _BlockingNativeProvider()
    provider = CachedNativeEmbeddingProvider(wrapped)

    first_task = asyncio.create_task(provider.embed_texts(["same text"], input_kind="query"))
    await wrapped.started.wait()
    second_task = asyncio.create_task(provider.embed_texts(["same text"], input_kind="query"))

    await asyncio.sleep(0)
    first_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first_task
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(second_task, timeout=1)

    wrapped.release.set()
    result = await asyncio.wait_for(
        provider.embed_texts(["same text"], input_kind="query"),
        timeout=1,
    )

    assert result == [[1.0, 0.0]]
    assert wrapped.calls == [
        (("same text",), "query"),
        (("same text",), "query"),
    ]


def test_get_org_driver_reuses_same_clone_for_same_org() -> None:
    client = GraphClient()
    base_driver = MagicMock()
    org_driver = MagicMock()
    client._client = SimpleNamespace(driver=base_driver)
    client._connected = True
    base_driver.clone.return_value = org_driver

    first = client.get_org_driver("org-123")
    second = client.get_org_driver("org-123")

    assert first is org_driver
    assert second is org_driver
    base_driver.clone.assert_called_once_with("org-123")


@pytest.mark.asyncio
async def test_default_execute_read_refuses_surreal_store() -> None:
    client = GraphClient()
    client._store = "surreal"
    client._client = SimpleNamespace(driver=MagicMock())
    client._connected = True

    with pytest.raises(GraphConnectionError, match="org-scoped"):
        await client.execute_read("SELECT * FROM entity")


@pytest.mark.asyncio
async def test_default_execute_write_refuses_surreal_store() -> None:
    client = GraphClient()
    client._store = "surreal"
    client._client = SimpleNamespace(driver=MagicMock())
    client._connected = True

    with pytest.raises(GraphConnectionError, match="org-scoped"):
        await client.execute_write("DELETE FROM entity")


@pytest.mark.asyncio
async def test_default_execute_read_refuses_removed_legacy_store() -> None:
    driver = MagicMock()
    driver.execute_query = AsyncMock(return_value=([{"ok": True}], None, None))
    client = GraphClient()
    client._store = "legacy"
    client._client = SimpleNamespace(driver=driver)
    client._connected = True

    with pytest.raises(GraphConnectionError, match="org-scoped"):
        await client.execute_read("MATCH (n) RETURN n")

    driver.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_org_execute_read_refuses_surreal_store_by_default() -> None:
    client = GraphClient()
    client._store = "surreal"
    client._client = SimpleNamespace(driver=MagicMock())
    client._connected = True

    with pytest.raises(GraphConnectionError, match="org-scoped"):
        await client.execute_read_org("SELECT * FROM entity", "org-123")


@pytest.mark.asyncio
async def test_org_execute_write_refuses_surreal_store_by_default() -> None:
    client = GraphClient()
    client._store = "surreal"
    client._client = SimpleNamespace(driver=MagicMock())
    client._connected = True

    with pytest.raises(GraphConnectionError, match="org-scoped"):
        await client.execute_write_org("DELETE FROM entity", "org-123")


@pytest.mark.asyncio
async def test_org_execute_read_allows_explicit_surreal_debug_escape_hatch() -> None:
    base_driver = MagicMock()
    org_driver = MagicMock()
    org_driver.execute_query = AsyncMock(return_value=[{"ok": True}])
    base_driver.clone.return_value = org_driver
    client = GraphClient()
    client._store = "surreal"
    client._client = SimpleNamespace(driver=base_driver)
    client._connected = True

    rows = await client.execute_read_org(
        "SELECT * FROM entity",
        "org-123",
        allow_surreal=True,
    )

    assert rows == [{"ok": True}]
    org_driver.execute_query.assert_awaited_once_with("SELECT * FROM entity")


@pytest.mark.asyncio
async def test_disconnect_closes_cached_org_drivers_once() -> None:
    client = GraphClient()
    base_runtime = SimpleNamespace(close=AsyncMock())
    org_driver = MagicMock()
    org_driver.close = AsyncMock()
    client._client = base_runtime
    client._connected = True
    client._org_drivers["org-123"] = org_driver
    client._org_drivers["org-456"] = org_driver

    await client.disconnect()

    org_driver.close.assert_awaited_once_with()
    base_runtime.close.assert_awaited_once_with()
    assert client._org_drivers == {}
