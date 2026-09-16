"""Provider refusals become bounded receipts that never carry request text."""

import pytest

from sibyl_core.ai.errors import (
    PROVIDER_ERROR_MESSAGE_LIMIT,
    LLMProviderError,
    LLMTimeoutError,
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
    assert provider_error_detail(LLMTimeoutError("timed out", details={"cause": "read"})) is None
    assert provider_error_detail(RuntimeError("boom")) is None
    assert provider_error_detail(ValueError("no details attribute")) is None


def test_provider_error_detail_ignores_non_integer_status():
    assert provider_error_detail(LLMProviderError("failed", details={"status_code": "400"})) is None
    assert provider_error_detail(LLMProviderError("failed", details={"status_code": True})) is None
