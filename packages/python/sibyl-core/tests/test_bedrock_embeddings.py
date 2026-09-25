"""Cohere Embed v4 on Amazon Bedrock with the HTTP transport stubbed: no AWS calls."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from sibyl_core.ai import bedrock
from sibyl_core.ai.bedrock import BedrockConfigError, resolve_bedrock_settings
from sibyl_core.embeddings import bedrock as cohere
from sibyl_core.embeddings import content as content_embeddings
from sibyl_core.embeddings import providers
from sibyl_core.embeddings.bedrock import (
    COHERE_EMBED_MAX_TEXTS,
    BedrockEmbeddingError,
    BedrockEmbeddingProvider,
    cohere_embedding_batches,
)
from sibyl_core.embeddings.providers import (
    CachedEmbeddingProvider,
    EmbeddingMetadata,
    capture_embedding_usage,
    create_embedding_provider,
)

pytest.importorskip("botocore")


@pytest.fixture(autouse=True)
def aws_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(
            ("AWS_", "SIBYL_BEDROCK_", "SIBYL_EMBEDDING_", "SIBYL_GRAPH_EMBEDDING_")
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SIBYL_MOCK_LLM", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLEFIXTURE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-secret")
    bedrock._boto_session.cache_clear()
    monkeypatch.setattr(cohere.asyncio, "sleep", _no_sleep)
    yield
    bedrock._boto_session.cache_clear()


async def _no_sleep(_seconds: float) -> None:
    return None


def metadata(model: str = "cohere.embed-v4:0", dimensions: int = 1536) -> EmbeddingMetadata:
    return EmbeddingMetadata(
        provider="bedrock",
        model=model,
        dimensions=dimensions,
        cache_namespace="document",
        tokenizer_estimate_method="provider-default",
    )


class FakeBedrock:
    """Answers InvokeModel like Cohere v4 with embedding_types=["float"]."""

    def __init__(self, *, fail_first: int = 0, status: int = 429) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_first = fail_first
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) <= self.fail_first:
            error_type = "ThrottlingException" if self.status == 429 else "ValidationException"
            return httpx.Response(
                self.status,
                headers={"x-amzn-errortype": f"{error_type}:http://internal"},
                json={"message": "Too many requests, please wait before trying again."},
            )
        body = json.loads(request.content)
        size = body["output_dimension"]
        vectors = [
            [float(len(text)) + (1.0 if body["input_type"] == "search_query" else 0.0)] * size
            for text in body["texts"]
        ]
        return httpx.Response(
            200,
            headers={
                "x-amzn-requestid": "embed-request",
                "x-amzn-bedrock-input-token-count": str(len(body["texts"]) * 3),
            },
            json={
                "id": "e",
                "embeddings": {"float": vectors},
                "response_type": "embeddings_by_type",
                "texts": body["texts"],
            },
        )


def provider_with(fake: FakeBedrock, **kwargs) -> BedrockEmbeddingProvider:
    return BedrockEmbeddingProvider(
        metadata=kwargs.pop("meta", metadata()),
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        **kwargs,
    )


async def test_query_and_document_kinds_map_to_cohere_input_types():
    fake = FakeBedrock()
    provider = provider_with(fake)

    await provider.embed_texts(["find the runbook"], input_kind="query")
    await provider.embed_texts(["the runbook"], input_kind="document")

    bodies = [json.loads(request.content) for request in fake.requests]
    assert [body["input_type"] for body in bodies] == ["search_query", "search_document"]
    assert all(body["output_dimension"] == 1536 for body in bodies)
    assert all(body["embedding_types"] == ["float"] for body in bodies)
    assert all(body["truncate"] == "RIGHT" for body in bodies)


async def test_requests_sign_for_bedrock_and_route_through_the_scope():
    fake = FakeBedrock()
    await provider_with(fake).embed_texts(["hello"])

    request = fake.requests[0]
    assert str(request.url) == (
        "https://bedrock-runtime.us-west-2.amazonaws.com/model/us.cohere.embed-v4:0/invoke"
    )
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256")
    assert "/us-west-2/bedrock/aws4_request" in request.headers["authorization"]


async def test_global_scope_and_bearer_keys(monkeypatch):
    monkeypatch.setenv("SIBYL_BEDROCK_INFERENCE_SCOPE", "global")
    monkeypatch.setenv("SIBYL_BEDROCK_API_KEY", "bedrock-api-key")
    fake = FakeBedrock()
    await provider_with(fake).embed_texts(["hello"])

    request = fake.requests[0]
    assert request.url.path == "/model/global.cohere.embed-v4:0/invoke"
    assert request.headers["authorization"] == "Bearer bedrock-api-key"


async def test_batches_follow_cohere_limits_run_concurrently_and_keep_order():
    fake = FakeBedrock()
    provider = provider_with(fake)
    texts = [f"text {index:03d}" + "x" * index for index in range(200)]

    vectors = await provider.embed_texts(texts)

    sizes = sorted(len(json.loads(r.content)["texts"]) for r in fake.requests)
    assert sizes == [8, 96, 96]
    assert [vector[0] for vector in vectors] == [float(len(text)) for text in texts]
    assert all(len(vector) == 1536 for vector in vectors)


def test_batches_split_on_payload_size_before_item_count():
    big = "y" * 6_000_000
    batches = cohere_embedding_batches([big, big, big, "small"])
    assert [len(batch) for batch in batches] == [2, 2]
    assert len(cohere_embedding_batches(["t"] * (COHERE_EMBED_MAX_TEXTS * 2 + 1))) == 3


async def test_empty_text_is_replaced_because_cohere_rejects_it():
    fake = FakeBedrock()
    await provider_with(fake).embed_texts(["", "  "])
    assert json.loads(fake.requests[0].content)["texts"] == ["[empty]", "[empty]"]


@pytest.mark.parametrize("dimensions", [256, 512, 1024, 1536])
def test_supported_dimensions(dimensions):
    provider = provider_with(FakeBedrock(), meta=metadata(dimensions=dimensions))
    assert provider.metadata.dimensions == dimensions


@pytest.mark.parametrize(
    ("model", "dimensions", "match"),
    [
        ("cohere.embed-v4:0", 768, "256, 512, 1024, 1536"),
        ("cohere.embed-v4:0", 3072, "not 3072"),
        ("amazon.titan-embed-text-v2:0", 1024, "Cohere Embed v4"),
    ],
)
def test_unsupported_models_and_dimensions_fail_clearly(model, dimensions, match):
    with pytest.raises(BedrockConfigError, match=match):
        provider_with(FakeBedrock(), meta=metadata(model, dimensions))


async def test_arn_models_pass_through_with_only_dimensions_checked():
    arn = "arn:aws:bedrock:us-west-2:123456789012:application-inference-profile/embed"
    fake = FakeBedrock()
    provider = provider_with(fake, meta=metadata(arn, 1024))
    await provider.embed_texts(["hello"])
    assert fake.requests[0].url.path.endswith("/invoke")
    assert arn.split("/")[-1] in fake.requests[0].url.path
    with pytest.raises(BedrockConfigError, match="not 768"):
        provider_with(FakeBedrock(), meta=metadata(arn, 768))


def test_inference_profile_arns_record_the_model_they_name():
    arn = "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.cohere.embed-v4:0"
    provider = provider_with(FakeBedrock(), meta=metadata(arn, 1536))
    assert provider.metadata.model == "cohere.embed-v4:0"
    assert provider.wire_model_id == arn
    titan = "arn:aws:bedrock:us-west-2::foundation-model/amazon.titan-embed-text-v2:0"
    with pytest.raises(BedrockConfigError, match="Cohere Embed v4"):
        provider_with(FakeBedrock(), meta=metadata(titan, 1024))


async def test_the_mantle_only_key_never_signs_cohere_calls(monkeypatch):
    monkeypatch.setenv("SIBYL_BEDROCK_API", "mantle")
    monkeypatch.setenv("ANTHROPIC_AWS_API_KEY", "claude-platform-key")
    fake = FakeBedrock()
    await provider_with(fake).embed_texts(["hello"])
    assert fake.requests[0].headers["authorization"].startswith("AWS4-HMAC-SHA256")


def test_metadata_tags_bedrock_with_the_scope_free_model():
    provider = provider_with(FakeBedrock(), meta=metadata("us.cohere.embed-v4:0", 1024))
    assert provider.metadata.provider == "bedrock"
    assert provider.metadata.model == "cohere.embed-v4:0"
    assert provider.metadata.dimensions == 1024
    assert provider.metadata.input_kind_sensitive is True
    # An explicit profile ID is still what goes on the wire.
    assert provider.wire_model_id == "us.cohere.embed-v4:0"


async def test_throttling_retries_without_giving_up_the_batch():
    fake = FakeBedrock(fail_first=2)
    vectors = await provider_with(fake).embed_texts(["hello"])
    assert len(fake.requests) == 3
    assert len(vectors) == 1


async def test_throttling_past_the_attempt_budget_raises():
    fake = FakeBedrock(fail_first=10)
    with pytest.raises(BedrockEmbeddingError, match="HTTP 429") as caught:
        await provider_with(fake, max_attempts=3).embed_texts(["hello"])
    assert caught.value.status_code == 429
    assert len(fake.requests) == 3


async def test_validation_errors_do_not_retry():
    fake = FakeBedrock(fail_first=10, status=400)
    with pytest.raises(BedrockEmbeddingError, match="HTTP 400"):
        await provider_with(fake).embed_texts(["hello"])
    assert len(fake.requests) == 1


async def test_wrong_vector_size_is_rejected():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": {"float": [[0.1] * 12]}})

    provider = BedrockEmbeddingProvider(
        metadata=metadata(), client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    with pytest.raises(BedrockEmbeddingError, match="12-dimension"):
        await provider.embed_texts(["hello"])


async def test_usage_comes_from_the_bedrock_token_header():
    fake = FakeBedrock()
    provider = provider_with(fake)
    with capture_embedding_usage(provider) as usage:
        await provider.embed_texts(["a", "b"])
    assert usage["prompt_tokens"] == 6
    assert usage["provider"] == "bedrock"
    assert provider.usage_snapshot()["requests"] == 1


def test_missing_region_fails_when_the_provider_is_built(monkeypatch):
    monkeypatch.delenv("AWS_REGION")
    with pytest.raises(BedrockConfigError, match="region"):
        BedrockEmbeddingProvider(metadata=metadata())


# -----------------------------------------------------------------------------
# Graph and content configuration
# -----------------------------------------------------------------------------


async def test_graph_embeddings_default_to_cohere_at_the_graph_index_size(monkeypatch):
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "bedrock")
    provider = providers.configured_embedding_provider()

    assert isinstance(provider, CachedEmbeddingProvider)
    assert provider.metadata.provider == "bedrock"
    assert provider.metadata.model == "cohere.embed-v4:0"
    assert provider.metadata.dimensions == 1024
    assert provider.metadata.cache_namespace == "graph"
    assert providers.configured_embedding_provider() is provider

    monkeypatch.setenv("SIBYL_BEDROCK_INFERENCE_SCOPE", "global")
    assert providers.configured_embedding_provider() is not provider


def test_a_bad_bedrock_setting_fails_loudly_instead_of_disabling(monkeypatch):
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "bedrock")
    monkeypatch.setenv("SIBYL_EMBEDDING_PROVIDER", "bedrock")
    monkeypatch.setenv("SIBYL_BEDROCK_INFERENCE_SCOPE", "mars")
    with pytest.raises(BedrockConfigError, match="SIBYL_BEDROCK_INFERENCE_SCOPE"):
        asyncio.run(_configured())
    with pytest.raises(BedrockConfigError, match="SIBYL_BEDROCK_INFERENCE_SCOPE"):
        content_embeddings.configured_content_embedding()


@pytest.mark.parametrize("field", ["embedding", "graph_embedding"])
def test_startup_refuses_sizes_cohere_cannot_produce(field):
    from sibyl_core.config import CoreConfig

    with pytest.raises(ValueError, match="cannot be served by Cohere"):
        CoreConfig(**{f"{field}_provider": "bedrock", f"{field}_dimensions": 768})
    assert CoreConfig(**{f"{field}_provider": "bedrock", f"{field}_dimensions": 1024})


def test_graph_embeddings_disable_without_a_region(monkeypatch):
    monkeypatch.setenv("SIBYL_GRAPH_EMBEDDING_PROVIDER", "bedrock")
    monkeypatch.delenv("AWS_REGION")
    assert asyncio.run(_configured()) is None


async def _configured():
    return providers.configured_embedding_provider()


def test_content_embeddings_default_to_cohere_at_the_content_index_size(monkeypatch):
    monkeypatch.setenv("SIBYL_EMBEDDING_PROVIDER", "bedrock")
    config = content_embeddings.configured_content_embedding()

    assert (config.provider, config.model, config.dimensions) == (
        "bedrock",
        "cohere.embed-v4:0",
        1536,
    )
    assert config.api_key is None
    assert config.ready
    fingerprint = config.fingerprint
    monkeypatch.setenv("SIBYL_BEDROCK_REGION", "us-east-1")
    assert content_embeddings.configured_content_embedding().fingerprint != fingerprint

    monkeypatch.delenv("SIBYL_BEDROCK_REGION")
    monkeypatch.delenv("AWS_REGION")
    assert not content_embeddings.configured_content_embedding().ready


def test_create_embedding_provider_builds_a_cached_bedrock_provider():
    provider = create_embedding_provider(
        provider="bedrock",
        model="cohere.embed-v4:0",
        dimensions=1536,
        cache_namespace="document",
    )
    assert isinstance(provider, CachedEmbeddingProvider)
    assert provider.metadata.provider == "bedrock"
    assert resolve_bedrock_settings().inference_scope == "us"
