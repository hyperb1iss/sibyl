"""Tests for transactional email delivery helpers."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import resend
from pydantic import SecretStr
from resend.http_client import HTTPClient
from structlog.testing import capture_logs

from sibyl import config as config_module
from sibyl.email import client as email_client_module
from sibyl.email.client import EmailClient


@pytest.mark.asyncio
async def test_email_client_writes_jsonl_outbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outbox_path = tmp_path / "email-outbox.jsonl"
    monkeypatch.setattr(config_module.settings, "email_outbox_path", str(outbox_path))
    monkeypatch.setattr(config_module.settings, "resend_api_key", SecretStr(""))
    monkeypatch.setattr(config_module.settings, "smtp_host", "")

    client = EmailClient()
    delivery_id = await client.send(
        to="auth-flow@example.com",
        subject="Reset your Sibyl password",
        html="<a href='http://localhost/reset-password?token=reset-token'>Reset</a>",
        text="http://localhost/reset-password?token=reset-token",
    )

    assert delivery_id == "outbox"
    lines = outbox_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["to"] == ["auth-flow@example.com"]
    assert record["subject"] == "Reset your Sibyl password"
    assert "reset-token" in record["text"]


@pytest.mark.asyncio
async def test_email_client_sends_via_smtp_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = object()
    events: list[tuple[str, object]] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            assert host == "smtp.gmail.com"
            assert port == 587
            assert timeout == 20.0

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def starttls(self, *, context: object) -> None:
            events.append(("starttls", context))

        def login(self, username: str, password: str) -> None:
            events.append(("login", (username, password)))

        def send_message(self, message: object) -> None:
            events.append(("send", str(message)))

    monkeypatch.setattr(config_module.settings, "email_outbox_path", "")
    monkeypatch.setattr(config_module.settings, "resend_api_key", SecretStr(""))
    monkeypatch.setattr(config_module.settings, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(config_module.settings, "smtp_port", 587)
    monkeypatch.setattr(config_module.settings, "smtp_username", "sibyl@hyperbliss.tech")
    monkeypatch.setattr(config_module.settings, "smtp_password", SecretStr("app-password"))
    monkeypatch.setattr(config_module.settings, "smtp_starttls", True)
    monkeypatch.setattr(config_module.settings, "smtp_ssl", False)
    monkeypatch.setattr(config_module.settings, "smtp_timeout_seconds", 20.0)
    monkeypatch.setattr(email_client_module.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(email_client_module.smtplib, "SMTP", FakeSMTP)

    client = EmailClient()
    assert client.configured is True

    delivery_id = await client.send(
        to="auth-flow@example.com",
        subject="Reset your Sibyl password",
        html="<a href='http://localhost/reset-password?token=reset-token'>Reset</a>",
        text="http://localhost/reset-password?token=reset-token",
    )

    assert delivery_id == "smtp"
    assert ("starttls", context) in events
    assert ("login", ("sibyl@hyperbliss.tech", "app-password")) in events
    assert any(event == "send" and "Reset your Sibyl password" in value for event, value in events)


@pytest.mark.asyncio
async def test_email_client_smtp_ssl_uses_verified_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = object()
    ssl_contexts: list[object] = []

    class FakeSMTPSSL:
        def __init__(
            self,
            host: str,
            port: int,
            *,
            timeout: float,
            context: object,
        ) -> None:
            assert host == "smtp.gmail.com"
            assert port == 465
            assert timeout == 20.0
            ssl_contexts.append(context)

        def __enter__(self) -> FakeSMTPSSL:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def login(self, username: str, password: str) -> None:
            assert username == "sibyl@hyperbliss.tech"
            assert password == "app-password"

        def send_message(self, message: object) -> None:
            assert "Reset your Sibyl password" in str(message)

    monkeypatch.setattr(config_module.settings, "email_outbox_path", "")
    monkeypatch.setattr(config_module.settings, "resend_api_key", SecretStr(""))
    monkeypatch.setattr(config_module.settings, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(config_module.settings, "smtp_port", 465)
    monkeypatch.setattr(config_module.settings, "smtp_username", "sibyl@hyperbliss.tech")
    monkeypatch.setattr(config_module.settings, "smtp_password", SecretStr("app-password"))
    monkeypatch.setattr(config_module.settings, "smtp_starttls", False)
    monkeypatch.setattr(config_module.settings, "smtp_ssl", True)
    monkeypatch.setattr(config_module.settings, "smtp_timeout_seconds", 20.0)
    monkeypatch.setattr(email_client_module.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(email_client_module.smtplib, "SMTP_SSL", FakeSMTPSSL)

    client = EmailClient()
    delivery_id = await client.send(
        to="auth-flow@example.com",
        subject="Reset your Sibyl password",
        html="<a href='http://localhost/reset-password?token=reset-token'>Reset</a>",
        text="http://localhost/reset-password?token=reset-token",
    )

    assert delivery_id == "smtp"
    assert ssl_contexts == [context]


@pytest.mark.asyncio
async def test_email_client_returns_outbox_when_smtp_fails_after_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outbox_path = tmp_path / "email-outbox.jsonl"

    monkeypatch.setattr(config_module.settings, "email_outbox_path", str(outbox_path))
    monkeypatch.setattr(config_module.settings, "resend_api_key", SecretStr(""))
    monkeypatch.setattr(config_module.settings, "smtp_host", "smtp.gmail.com")
    monkeypatch.setattr(config_module.settings, "smtp_port", 587)
    monkeypatch.setattr(config_module.settings, "smtp_username", "sibyl@hyperbliss.tech")
    monkeypatch.setattr(config_module.settings, "smtp_password", SecretStr("app-password"))
    monkeypatch.setattr(config_module.settings, "smtp_starttls", True)
    monkeypatch.setattr(config_module.settings, "smtp_ssl", False)
    monkeypatch.setattr(config_module.settings, "smtp_timeout_seconds", 20.0)

    def fail_send_smtp_sync(self: EmailClient, **kwargs: object) -> None:
        raise OSError("relay down")

    monkeypatch.setattr(EmailClient, "_send_smtp_sync", fail_send_smtp_sync)

    client = EmailClient()
    delivery_id = await client.send(
        to="auth-flow@example.com",
        subject="Reset your Sibyl password",
        html="<a href='http://localhost/reset-password?token=reset-token'>Reset</a>",
        text="http://localhost/reset-password?token=reset-token",
    )

    assert delivery_id == "outbox"
    assert outbox_path.exists()


RESEND_TEST_KEY = "re_test_key"


@dataclass(frozen=True)
class _ResendCall:
    method: str
    url: str
    headers: dict[str, str]
    json: dict[str, object] | list[object] | None


class _RecordingResendHTTP(HTTPClient):
    """Stands in for the network under the Resend SDK and records each API call."""

    def __init__(self, *, status: int = 200, body: dict[str, object] | None = None) -> None:
        self.calls: list[_ResendCall] = []
        self._status = status
        self._content = json.dumps(body if body is not None else {"id": "resend-email-123"})

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json: dict[str, object] | list[object] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> tuple[bytes, int, Mapping[str, str]]:
        self.calls.append(_ResendCall(method=method, url=url, headers=dict(headers), json=json))
        return self._content.encode(), self._status, {"Content-Type": "application/json"}


def _configure_resend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    http: _RecordingResendHTTP,
) -> Path:
    outbox_path = tmp_path / "email-outbox.jsonl"
    monkeypatch.setattr(config_module.settings, "email_outbox_path", str(outbox_path))
    monkeypatch.setattr(config_module.settings, "resend_api_key", SecretStr(RESEND_TEST_KEY))
    monkeypatch.setattr(config_module.settings, "smtp_host", "")
    monkeypatch.setattr(config_module.settings, "email_from", "Sibyl <noreply@sibyl.dev>")
    # EmailClient writes the key onto the SDK module; restore it after the test.
    monkeypatch.setattr(resend, "api_key", resend.api_key)
    monkeypatch.setattr(resend, "default_http_client", http)
    return outbox_path


@pytest.mark.asyncio
async def test_email_client_sends_via_resend_when_key_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    http = _RecordingResendHTTP()
    _configure_resend(monkeypatch, tmp_path, http)

    client = EmailClient()
    assert client.configured is True

    delivery_id = await client.send(
        to="auth-flow@example.com",
        subject="Reset your Sibyl password",
        html="<a href='http://localhost/reset-password?token=reset-token'>Reset</a>",
        text="http://localhost/reset-password?token=reset-token",
        reply_to="support@example.com",
    )

    assert delivery_id == "resend-email-123"
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call.method == "post"
    assert call.url.endswith("/emails")
    assert call.headers["Authorization"] == f"Bearer {RESEND_TEST_KEY}"
    assert call.json == {
        "from": "Sibyl <noreply@sibyl.dev>",
        "to": ["auth-flow@example.com"],
        "subject": "Reset your Sibyl password",
        "html": "<a href='http://localhost/reset-password?token=reset-token'>Reset</a>",
        "text": "http://localhost/reset-password?token=reset-token",
        "reply_to": "support@example.com",
    }


@pytest.mark.asyncio
async def test_email_client_warns_when_resend_key_is_set_but_package_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    http = _RecordingResendHTTP()
    _configure_resend(monkeypatch, tmp_path, http)
    monkeypatch.setitem(sys.modules, "resend", None)

    with capture_logs() as entries:
        client = EmailClient()
        delivery_id = await client.send(
            to="auth-flow@example.com",
            subject="Reset your Sibyl password",
            html="<p>Reset</p>",
        )

    assert client.configured is False
    assert delivery_id == "outbox"
    assert http.calls == []
    warnings = [
        (entry["event"], entry.get("reason"))
        for entry in entries
        if entry["log_level"] == "warning"
    ]
    assert warnings == [
        ("email_provider_unavailable", "resend package is not installed"),
        ("email_skipped", "provider_unavailable"),
    ]


@pytest.mark.asyncio
async def test_email_client_logs_error_when_resend_rejects_the_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    http = _RecordingResendHTTP(
        status=422,
        body={"statusCode": 422, "name": "validation_error", "message": "Invalid `to` field."},
    )
    _configure_resend(monkeypatch, tmp_path, http)

    client = EmailClient()
    with capture_logs() as entries:
        delivery_id = await client.send(
            to="auth-flow@example.com",
            subject="Reset your Sibyl password",
            html="<p>Reset</p>",
        )

    assert delivery_id == "outbox"
    assert len(http.calls) == 1
    failures = [entry for entry in entries if entry["event"] == "email_failed"]
    assert [(entry["log_level"], entry["provider"]) for entry in failures] == [("error", "resend")]
