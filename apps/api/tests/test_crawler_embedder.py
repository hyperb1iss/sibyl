from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl.crawler import embedder as embedder_module
from sibyl.crawler.chunker import Chunk
from sibyl.crawler.embedder import EmbeddingService


class FakeSettingsService:
    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def get_gemini_key(self) -> str | None:
        return self.values.get("gemini_api_key")

    async def get_openai_key(self) -> str | None:
        return self.values.get("openai_api_key")


@pytest.mark.asyncio
async def test_gemini_embed_text_formats_query_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                embed_content=AsyncMock(
                    return_value=SimpleNamespace(
                        embeddings=[SimpleNamespace(values=[0.1, 0.2, 0.3])]
                    )
                )
            )
        )
    )
    service = FakeSettingsService(
        {
            "embedding_provider": "gemini",
            "embedding_model": "gemini-embedding-2",
            "embedding_dimensions": "768",
            "gemini_api_key": "gemini-key",
        }
    )

    monkeypatch.setattr(embedder_module, "get_settings_service", lambda: service)
    monkeypatch.setattr(embedder_module.genai, "Client", lambda api_key: fake_client)

    embedding = await EmbeddingService().embed_text("find vector search docs")

    assert embedding == [0.1, 0.2, 0.3]
    call = fake_client.aio.models.embed_content.await_args.kwargs
    assert call["model"] == "gemini-embedding-2"
    assert call["contents"][0].parts[0].text == (
        "task: search result | query: find vector search docs"
    )
    assert call["config"].output_dimensionality == 768


@pytest.mark.asyncio
async def test_gemini_embed_chunks_formats_document_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                embed_content=AsyncMock(
                    return_value=SimpleNamespace(
                        embeddings=[SimpleNamespace(values=[0.4, 0.5, 0.6])]
                    )
                )
            )
        )
    )
    service = FakeSettingsService(
        {
            "embedding_provider": "gemini",
            "embedding_model": "gemini-embedding-2",
            "embedding_dimensions": "1536",
            "gemini_api_key": "gemini-key",
        }
    )

    monkeypatch.setattr(embedder_module, "get_settings_service", lambda: service)
    monkeypatch.setattr(embedder_module.genai, "Client", lambda api_key: fake_client)

    chunks = [
        Chunk(
            content="Chunk body",
            context="Surrounding context",
            heading_path=["Guide", "Embeddings"],
        )
    ]

    embeddings = await EmbeddingService().embed_chunks(chunks)

    assert embeddings == [[0.4, 0.5, 0.6]]
    call = fake_client.aio.models.embed_content.await_args.kwargs
    assert call["contents"][0].parts[0].text == (
        "title: Guide / Embeddings | text: Surrounding context\n\nChunk body"
    )


@pytest.mark.asyncio
async def test_bedrock_embeds_chunks_through_cohere(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    import httpx

    from sibyl_core.embeddings import bedrock as cohere

    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        vectors = [[0.5] * body["output_dimension"] for _ in body["texts"]]
        return httpx.Response(200, json={"embeddings": {"float": vectors}})

    for name in ("SIBYL_BEDROCK_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "SIBYL_BEDROCK_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SIBYL_BEDROCK_REGION", "us-west-2")
    monkeypatch.setenv("SIBYL_BEDROCK_API_KEY", "bedrock-key")
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(cohere.BedrockEmbeddingProvider, "_http_client", lambda self: client)
    service = FakeSettingsService({"embedding_provider": "bedrock", "embedding_dimensions": "1536"})
    monkeypatch.setattr(embedder_module, "get_settings_service", lambda: service)

    embedder = EmbeddingService()
    vectors = await embedder.embed_texts(["alpha", "beta"])
    query = await embedder.embed_text("alpha?")

    assert [len(vector) for vector in vectors] == [1536, 1536]
    assert len(query) == 1536
    assert [body["input_type"] for body in requests] == ["search_document", "search_query"]
    assert all(body["output_dimension"] == 1536 for body in requests)


@pytest.mark.asyncio
async def test_switching_back_to_bedrock_rebuilds_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.embeddings.bedrock import BedrockEmbeddingProvider

    monkeypatch.setenv("SIBYL_BEDROCK_REGION", "us-west-2")
    monkeypatch.setenv("SIBYL_BEDROCK_API_KEY", "bedrock-key")
    monkeypatch.delenv("SIBYL_BEDROCK_PROFILE", raising=False)
    service = FakeSettingsService({"openai_api_key": "sk-test"})
    service.get_openai_key = AsyncMock(return_value="sk-test")  # type: ignore[attr-defined]
    monkeypatch.setattr(embedder_module, "get_settings_service", lambda: service)
    embedder = EmbeddingService()
    bedrock = embedder_module.ResolvedEmbeddingConfig("bedrock", "cohere.embed-v4:0", 1536)
    openai = embedder_module.ResolvedEmbeddingConfig("openai", "text-embedding-3-small", 1536)

    first = await embedder._get_client(bedrock)
    assert isinstance(first, BedrockEmbeddingProvider)
    assert not isinstance(await embedder._get_client(openai), BedrockEmbeddingProvider)
    assert isinstance(await embedder._get_client(bedrock), BedrockEmbeddingProvider)
    assert embedder._batch_size(bedrock, 250) == 250
    assert embedder._batch_size(openai, 250) == embedder.batch_size


@pytest.mark.asyncio
async def test_chunk_vectors_come_back_with_the_model_that_made_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                embed_content=AsyncMock(
                    return_value=SimpleNamespace(
                        embeddings=[SimpleNamespace(values=[0.4, 0.5, 0.6])]
                    )
                )
            )
        )
    )
    service = FakeSettingsService(
        {
            "embedding_provider": "gemini",
            "embedding_model": "gemini-embedding-2",
            "embedding_dimensions": "1536",
            "gemini_api_key": "gemini-key",
        }
    )
    monkeypatch.setattr(embedder_module, "get_settings_service", lambda: service)
    monkeypatch.setattr(embedder_module.genai, "Client", lambda api_key: fake_client)

    embeddings, metadata = await EmbeddingService().embed_chunks_with_metadata(
        [Chunk(content="Chunk body")]
    )
    _query, query_metadata = await EmbeddingService().embed_query_with_metadata("find it")

    expected = {
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 1536,
        "text_version": "document-chunk-v1",
    }
    assert embeddings == [[0.4, 0.5, 0.6]]
    assert metadata == expected
    # A query is scored only against chunks stamped with the space it was embedded in.
    assert query_metadata == expected
    assert await EmbeddingService().chunk_embedding_metadata() == expected


@pytest.mark.asyncio
async def test_chunk_embedding_metadata_is_none_without_a_usable_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeSettingsService({"embedding_provider": "openai"})
    monkeypatch.setattr(embedder_module, "get_settings_service", lambda: service)

    assert await EmbeddingService().chunk_embedding_metadata() is None
