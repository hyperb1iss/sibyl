"""Provider refusals become bounded receipts that never carry request text."""

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior

from sibyl_core.ai.errors import (
    PROVIDER_ERROR_MESSAGE_LIMIT,
    LLMProviderError,
    LLMTimeoutError,
    classify_llm_exception,
    provider_error_detail,
)


def _provider_error(body):
    return LLMProviderError(
        "LLM provider request failed with HTTP 400",
        provider="openai",
        model="gpt-5",
        details={"status_code": 400, "body": body},
    )


def test_provider_error_detail_reads_structured_body():
    failure = _provider_error(
        {
            "error": {
                "type": "billing_hard_limit_reached",
                "message": "You have reached your specified API usage limits",
                "param": None,
                "code": None,
            }
        }
    )

    assert provider_error_detail(failure) == {
        "status_code": 400,
        "type": "billing_hard_limit_reached",
        "message": "You have reached your specified API usage limits",
    }


def test_provider_error_detail_keeps_only_type_and_message():
    failure = _provider_error(
        {
            "error": {
                "type": "invalid_request_error",
                "message": "context length exceeded",
                "prompt": "SECRET EVIDENCE TEXT",
                "request": {"messages": [{"content": "SECRET EVIDENCE TEXT"}]},
            },
            "prompt": "SECRET EVIDENCE TEXT",
            "input": "SECRET EVIDENCE TEXT",
        }
    )

    detail = provider_error_detail(failure)

    assert set(detail) == {"status_code", "type", "message"}
    assert "SECRET" not in repr(detail)


def test_provider_error_detail_truncates_long_messages():
    failure = _provider_error({"error": {"type": "server_error", "message": "x" * 5000}})

    detail = provider_error_detail(failure)

    assert detail["message"] == "x" * PROVIDER_ERROR_MESSAGE_LIMIT
    assert len(detail["message"]) == 200


def test_provider_error_detail_accepts_string_body():
    failure = _provider_error("Bad Request: quota exhausted " + "y" * 500)

    detail = provider_error_detail(failure)

    assert detail["type"] is None
    assert detail["message"] == ("Bad Request: quota exhausted " + "y" * 500)[:200]
    assert detail["status_code"] == 400


@pytest.mark.parametrize(
    "body",
    [None, {}, {"error": None}, {"error": []}, {"error": {}}, {"detail": "not an error object"}],
)
def test_provider_error_detail_falls_back_to_status_alone(body):
    assert provider_error_detail(_provider_error(body)) == {
        "status_code": 400,
        "type": None,
        "message": None,
    }


def test_provider_error_detail_absent_without_status_or_body():
    assert provider_error_detail(LLMTimeoutError("timed out")) is None
    assert provider_error_detail(RuntimeError("boom")) is None
    assert provider_error_detail(ValueError("no details attribute")) is None


def test_provider_error_detail_names_a_failure_the_provider_never_answered():
    failure = LLMTimeoutError(
        "LLM provider request timed out",
        provider="anthropic",
        model="claude-opus-5",
        details={"cause": "Request timed out." + "z" * 5000, "exception_type": "APITimeoutError"},
    )

    detail = provider_error_detail(failure)

    assert detail is not None
    assert set(detail) == {"status_code", "type", "message"}
    assert detail["status_code"] is None
    assert detail["type"] == "APITimeoutError"
    assert len(detail["message"]) == PROVIDER_ERROR_MESSAGE_LIMIT
    assert detail["message"].startswith("Request timed out.")


def test_provider_error_detail_prefers_the_body_over_the_cause():
    failure = LLMProviderError(
        "LLM provider request failed with HTTP 400",
        details={
            "status_code": 400,
            "body": {"error": {"type": "invalid_request_error", "message": "too long"}},
            "cause": "SECRET EVIDENCE TEXT",
            "exception_type": "ModelHTTPError",
        },
    )

    detail = provider_error_detail(failure)

    assert detail == {
        "status_code": 400,
        "type": "invalid_request_error",
        "message": "too long",
    }
    assert "SECRET" not in repr(detail)


def test_provider_error_detail_ignores_non_integer_status():
    assert provider_error_detail(LLMProviderError("failed", details={"status_code": "400"})) is None
    assert provider_error_detail(LLMProviderError("failed", details={"status_code": True})) is None


# ---------------------------------------------------------------------------
# SDK read timeouts stay inside the timeout taxonomy
# ---------------------------------------------------------------------------


def _request():
    import httpx

    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_sdk_timeout_wrapper_is_not_a_standard_timeout():
    import anthropic
    import httpx

    failure = anthropic.APITimeoutError(request=_request())

    assert not isinstance(failure, TimeoutError)
    assert not isinstance(failure, httpx.TimeoutException)
    assert isinstance(failure, anthropic.APIConnectionError)


def test_read_timeouts_classify_as_timeouts_with_a_named_cause():
    import anthropic
    import httpx
    import openai

    failures = [
        anthropic.APITimeoutError(request=_request()),
        openai.APITimeoutError(request=_request()),
        httpx.ReadTimeout("timed out", request=_request()),
        TimeoutError("timed out"),
    ]

    for failure in failures:
        classified = classify_llm_exception(
            failure, provider="anthropic", model="claude-opus-5", surface="memory"
        )

        assert isinstance(classified, LLMTimeoutError)
        assert classified.details["exception_type"] == type(failure).__name__
        assert provider_error_detail(classified) == {
            "status_code": None,
            "type": type(failure).__name__,
            "message": str(failure)[:PROVIDER_ERROR_MESSAGE_LIMIT],
        }


def test_timeout_wrapper_is_recognized_by_name_without_the_sdk():
    class APITimeoutError(Exception):
        """A provider SDK wrapper this module never imports."""

    class VendorReadTimeout(APITimeoutError):
        pass

    for failure in (APITimeoutError("timed out"), VendorReadTimeout("timed out")):
        assert isinstance(classify_llm_exception(failure), LLMTimeoutError)

    assert isinstance(classify_llm_exception(RuntimeError("boom")), LLMProviderError)


def test_unclassified_model_failures_report_a_type_but_never_their_body():
    """str(UnexpectedModelBehavior) embeds the response body, so it stays out."""
    failure = classify_llm_exception(
        UnexpectedModelBehavior("Invalid JSON", body='{"text": "SECRET EVIDENCE TEXT"}')
    )

    detail = provider_error_detail(failure)

    assert detail == {
        "status_code": None,
        "type": "UnexpectedModelBehavior",
        "message": None,
    }
    assert "SECRET" not in repr(detail)


def test_an_unknown_failure_reports_its_type_without_its_message():
    detail = provider_error_detail(classify_llm_exception(RuntimeError("boom SECRET")))

    assert detail == {"status_code": None, "type": "RuntimeError", "message": None}
