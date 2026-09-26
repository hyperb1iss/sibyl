"""One classifier decides what a provider error says about the input that failed.

Only an input rejection may blame rows. Quota, throttling, model and service
failures say "not now", and credential or access failures say nothing about
the input at all, so neither may ever leave a row refused.
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from sibyl_core.ai.bedrock import BedrockSettings
from sibyl_core.embeddings import bedrock as bedrock_module
from sibyl_core.embeddings.bedrock import BedrockEmbeddingError, BedrockEmbeddingProvider
from sibyl_core.embeddings.provenance import (
    PROVIDER_ERROR_FAULT,
    PROVIDER_ERROR_INPUT,
    PROVIDER_ERROR_TRANSIENT,
    is_transient_provider_error,
    provider_error_kind,
    provider_error_status,
)
from sibyl_core.embeddings.providers import EmbeddingMetadata

_CORAL = ":http://internal.amazon.com/coral/com.amazon.bedrock/"

# The shapes Bedrock InvokeModel answers with: HTTP status, x-amzn-ErrorType, message.
BEDROCK_ERRORS = [
    (
        "ServiceQuotaExceededException",
        400,
        "Your request exceeds the service quota.",
        PROVIDER_ERROR_TRANSIENT,
    ),
    ("ThrottlingException", 429, "Too many requests.", PROVIDER_ERROR_TRANSIENT),
    (
        "ModelErrorException",
        424,
        "The request failed while processing the model.",
        PROVIDER_ERROR_TRANSIENT,
    ),
    ("ModelNotReadyException", 429, "The model is not ready.", PROVIDER_ERROR_TRANSIENT),
    (
        "ModelTimeoutException",
        408,
        "Processing time exceeded the model timeout.",
        PROVIDER_ERROR_TRANSIENT,
    ),
    (
        "InternalServerException",
        500,
        "An internal server error occurred.",
        PROVIDER_ERROR_TRANSIENT,
    ),
    (
        "ServiceUnavailableException",
        503,
        "The service isn't currently available.",
        PROVIDER_ERROR_TRANSIENT,
    ),
    ("AccessDeniedException", 403, "You don't have access to the model.", PROVIDER_ERROR_FAULT),
    ("ResourceNotFoundException", 404, "The model was not found.", PROVIDER_ERROR_FAULT),
    (
        "ValidationException",
        400,
        "Malformed input request: expected maxLength: 2048.",
        PROVIDER_ERROR_INPUT,
    ),
    ("ValidationException", 400, "Input is too long for requested model.", PROVIDER_ERROR_INPUT),
]


def _bedrock(respond) -> BedrockEmbeddingProvider:
    return BedrockEmbeddingProvider(
        metadata=EmbeddingMetadata(
            provider="bedrock",
            model="us.cohere.embed-v4:0",
            dimensions=1536,
            cache_namespace="raw-memory",
            tokenizer_estimate_method="provider-default",
        ),
        settings=BedrockSettings(region="us-east-1", api_key="test-key"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


async def _bedrock_error(response: httpx.Response, monkeypatch) -> BedrockEmbeddingError:
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    provider = _bedrock(lambda _request: response)
    with pytest.raises(BedrockEmbeddingError) as caught:
        await provider.embed_texts(["row"])
    return caught.value


@pytest.mark.parametrize(
    ("error_type", "status", "message", "kind"),
    BEDROCK_ERRORS,
    ids=[f"{row[0]}-{row[1]}-{index}" for index, row in enumerate(BEDROCK_ERRORS)],
)
async def test_bedrock_errors_carry_their_aws_type_into_the_classifier(
    monkeypatch, error_type, status, message, kind
) -> None:
    error = await _bedrock_error(
        httpx.Response(
            status, headers={"x-amzn-ErrorType": error_type + _CORAL}, json={"message": message}
        ),
        monkeypatch,
    )

    assert (error.status_code, error.error_type) == (status, error_type)
    assert provider_error_kind(error) == kind
    assert provider_error_status(error) == status


async def test_a_bedrock_error_without_the_header_is_read_from_its_json_type(monkeypatch) -> None:
    error = await _bedrock_error(
        httpx.Response(
            400,
            json={"__type": "com.amazon.bedrock#ServiceQuotaExceededException", "message": "quota"},
        ),
        monkeypatch,
    )

    assert error.error_type == "ServiceQuotaExceededException"
    assert provider_error_kind(error) == PROVIDER_ERROR_TRANSIENT


async def test_bedrock_retries_throttling_before_surfacing_it(monkeypatch) -> None:
    monkeypatch.setattr(bedrock_module, "_backoff", lambda _attempt: 0.0)
    calls: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(len(json.loads(request.content)["texts"]))
        return httpx.Response(
            429,
            headers={"x-amzn-ErrorType": "ThrottlingException" + _CORAL},
            json={"message": "slow"},
        )

    with pytest.raises(BedrockEmbeddingError) as caught:
        await _bedrock(respond).embed_texts(["row"])

    assert len(calls) == bedrock_module.BEDROCK_EMBED_MAX_ATTEMPTS
    assert is_transient_provider_error(caught.value)


_OPENAI_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/embeddings")


def _openai(cls, status: int, code: str | None):
    return cls(
        "provider message",
        response=httpx.Response(status, request=_OPENAI_REQUEST),
        body={"code": code} if code else None,
    )


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_openai(openai.BadRequestError, 400, "context_length_exceeded"), PROVIDER_ERROR_INPUT),
        (_openai(openai.BadRequestError, 400, None), PROVIDER_ERROR_INPUT),
        (_openai(openai.RateLimitError, 429, "rate_limit_exceeded"), PROVIDER_ERROR_TRANSIENT),
        (_openai(openai.RateLimitError, 429, "insufficient_quota"), PROVIDER_ERROR_TRANSIENT),
        (_openai(openai.InternalServerError, 500, None), PROVIDER_ERROR_TRANSIENT),
        (_openai(openai.AuthenticationError, 401, "invalid_api_key"), PROVIDER_ERROR_FAULT),
        (_openai(openai.NotFoundError, 404, "model_not_found"), PROVIDER_ERROR_FAULT),
        (openai.APITimeoutError(request=_OPENAI_REQUEST), PROVIDER_ERROR_TRANSIENT),
        (openai.APIConnectionError(request=_OPENAI_REQUEST), PROVIDER_ERROR_TRANSIENT),
    ],
    ids=lambda value: getattr(value, "__name__", None) or type(value).__name__,
)
def test_openai_errors_are_classified_by_code_then_status(error, kind) -> None:
    assert provider_error_kind(error) == kind


def _genai(cls, status: int, name: str):
    return cls(status, {"error": {"code": status, "status": name, "message": "provider message"}})


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_genai(genai_errors.ClientError, 400, "INVALID_ARGUMENT"), PROVIDER_ERROR_INPUT),
        (_genai(genai_errors.ClientError, 429, "RESOURCE_EXHAUSTED"), PROVIDER_ERROR_TRANSIENT),
        (_genai(genai_errors.ClientError, 403, "PERMISSION_DENIED"), PROVIDER_ERROR_FAULT),
        (_genai(genai_errors.ClientError, 404, "NOT_FOUND"), PROVIDER_ERROR_FAULT),
        (_genai(genai_errors.ServerError, 500, "INTERNAL"), PROVIDER_ERROR_TRANSIENT),
        (_genai(genai_errors.ServerError, 503, "UNAVAILABLE"), PROVIDER_ERROR_TRANSIENT),
    ],
)
def test_gemini_errors_are_classified_by_status_name(error, kind) -> None:
    assert provider_error_kind(error) == kind


def test_botocore_errors_are_read_from_their_response_mapping() -> None:
    class ClientErrorLike(Exception):
        def __init__(self, code: str, status: int) -> None:
            super().__init__(code)
            self.response = {
                "Error": {"Code": code},
                "ResponseMetadata": {"HTTPStatusCode": status},
            }

    assert provider_error_kind(ClientErrorLike("ServiceQuotaExceededException", 400)) == (
        PROVIDER_ERROR_TRANSIENT
    )
    assert provider_error_kind(ClientErrorLike("ValidationException", 400)) == PROVIDER_ERROR_INPUT
    assert (
        provider_error_kind(ClientErrorLike("AccessDeniedException", 403)) == PROVIDER_ERROR_FAULT
    )
    assert provider_error_status(ClientErrorLike("ThrottlingException", 429)) == 429


def test_an_error_that_says_nothing_about_the_input_never_blames_it() -> None:
    class NoCredentialsError(Exception):
        pass

    wrapped = RuntimeError("embedding call failed")
    wrapped.__cause__ = BedrockEmbeddingError(
        "quota", status_code=400, error_type="ServiceQuotaExceededException"
    )

    assert (
        provider_error_kind(NoCredentialsError("unable to locate credentials"))
        == PROVIDER_ERROR_FAULT
    )
    assert (
        provider_error_kind(ValueError("provider returned 3 vectors for 4 texts"))
        == PROVIDER_ERROR_FAULT
    )
    assert provider_error_kind(wrapped) == PROVIDER_ERROR_TRANSIENT


def test_only_model_processing_failures_may_be_split_to_find_a_row() -> None:
    from sibyl_core.embeddings.provenance import is_model_processing_error

    def bedrock(error_type: str, status: int) -> BedrockEmbeddingError:
        return BedrockEmbeddingError("failed", status_code=status, error_type=error_type)

    assert is_model_processing_error(bedrock("ModelErrorException", 424))
    assert is_model_processing_error(bedrock("ModelTimeoutException", 408))
    assert is_model_processing_error(bedrock("InternalServerException", 500))
    assert is_model_processing_error(_openai(openai.InternalServerError, 500, None))
    assert is_model_processing_error(_genai(genai_errors.ServerError, 500, "INTERNAL"))
    for capacity in (
        bedrock("ThrottlingException", 429),
        bedrock("ServiceQuotaExceededException", 400),
        bedrock("ModelNotReadyException", 429),
        bedrock("ServiceUnavailableException", 503),
        _genai(genai_errors.ServerError, 503, "UNAVAILABLE"),
    ):
        assert not is_model_processing_error(capacity)
    assert not is_model_processing_error(bedrock("ValidationException", 400))
    assert not is_model_processing_error(bedrock("AccessDeniedException", 403))
