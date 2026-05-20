from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import typer

import sibyl_cli.client as client_module
from sibyl_cli import config_store
from sibyl_cli import pending_writes
from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.common import handle_client_error


def _client_with_transport(transport: httpx.MockTransport) -> SibylClient:
    client = SibylClient(base_url="http://testserver/api", auth_token="token")
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=transport,
        headers=client._default_headers(),
    )
    return client


@pytest.mark.asyncio
async def test_client_parses_structured_error_envelope() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            409,
            json={
                "error": "constraint_violation",
                "message": "duplicate entity name in scope",
                "request_id": "req_duplicate",
                "remediation": "Use a different title.",
                "details": {"field": "name"},
            },
        )
    )
    client = _client_with_transport(transport)

    with pytest.raises(SibylClientError) as exc:
        await client.get("/entities")

    await client.close()
    assert exc.value.status_code == 409
    assert exc.value.error_code == "constraint_violation"
    assert exc.value.detail == "duplicate entity name in scope"
    assert exc.value.request_id == "req_duplicate"
    assert exc.value.remediation == "Use a different title."
    assert exc.value.details == {"field": "name"}


def test_handle_client_error_renders_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    error = SibylClientError(
        "API error",
        status_code=409,
        detail="duplicate entity name in scope",
        error_code="constraint_violation",
        request_id="req_duplicate",
        remediation="Use a different title.",
    )

    with pytest.raises(typer.Exit):
        handle_client_error(error)

    output = capsys.readouterr().out
    assert "constraint_violation: duplicate entity name in scope" in output
    assert "request_id: req_duplicate" in output
    assert "Use a different title." in output


@pytest.mark.asyncio
async def test_client_circuit_breaker_sleeps_after_repeated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_module._FAILURE_WINDOWS.clear()
    monkeypatch.setattr(client_module.sys, "argv", ["sibyl", "add"])
    sleep = AsyncMock()
    monkeypatch.setattr(client_module, "anyio_sleep", sleep)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            500,
            json={
                "error": "internal_error",
                "message": "An internal error occurred. Please try again later.",
                "request_id": "req_failure",
            },
        )
    )
    client = _client_with_transport(transport)

    for _ in range(3):
        with pytest.raises(SibylClientError):
            await client.get("/entities")

    with pytest.raises(SibylClientError):
        await client.get("/entities")

    await client.close()
    sleep.assert_awaited_once()
    client_module._FAILURE_WINDOWS.clear()


@pytest.mark.asyncio
async def test_mutating_request_buffers_and_deletes_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    seen_headers: list[str] = []
    transport = httpx.MockTransport(
        lambda request: (
            seen_headers.append(request.headers["Idempotency-Key"])
            or httpx.Response(200, json={"ok": True})
        )
    )
    client = _client_with_transport(transport)

    data = await client.post("/entities", json={"name": "Buffered", "content": "Body"})

    await client.close()
    assert data == {"ok": True}
    assert seen_headers
    assert pending_writes.list_pending_writes() == []


@pytest.mark.asyncio
async def test_mutating_request_requires_explicit_context_before_first_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client = SibylClient(auth_token="token")

    with pytest.raises(SibylClientError) as exc:
        await client.post("/entities", json={"name": "No context", "content": "Body"})

    await client.close()
    assert "No Sibyl context is configured" in str(exc.value)
    assert exc.value.remediation == client_module.INIT_REMEDIATION
    assert pending_writes.list_pending_writes() == []


@pytest.mark.asyncio
async def test_mutating_request_survives_connect_drop_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)

    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("api down", request=request)

    client = _client_with_transport(httpx.MockTransport(dropped))

    with pytest.raises(SibylClientError):
        await client.post("/entities", json={"name": "Replay me", "content": "Body"})

    await client.close()
    pending = pending_writes.list_pending_writes()
    assert len(pending) == 1
    assert pending[0]["path"] == "/entities"

    seen_headers: list[str] = []

    def restored(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers["Idempotency-Key"])
        return httpx.Response(200, json={"ok": True})

    replay_client = _client_with_transport(httpx.MockTransport(restored))
    result = await replay_client._request(
        str(pending[0]["method"]),
        str(pending[0]["path"]),
        json=pending[0]["json"],
        params=pending[0]["params"],
        _buffer_pending=False,
        _pending_write_id=str(pending[0]["id"]),
        _idempotency_key=str(pending[0]["idempotency_key"]),
    )

    await replay_client.close()
    assert result == {"ok": True}
    assert seen_headers == [pending[0]["idempotency_key"]]
    assert pending_writes.list_pending_writes() == []


@pytest.mark.asyncio
async def test_mutating_request_refreshes_401_and_retries_same_pending_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    seen_headers: list[str] = []

    def stale_then_ok(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers["Idempotency-Key"])
        if len(seen_headers) == 1:
            return httpx.Response(
                401,
                json={"error": "unauthorized", "message": "expired access token"},
            )
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(httpx.MockTransport(stale_then_ok))
    refresh = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(client, "_refresh_token", refresh)

    result = await client.post("/entities", json={"name": "Retry me", "content": "Body"})

    await client.close()
    assert result == {"ok": True}
    refresh.assert_awaited_once()
    assert len(seen_headers) == 2
    assert seen_headers[0] == seen_headers[1]
    assert pending_writes.list_pending_writes() == []


@pytest.mark.asyncio
async def test_mutating_request_keeps_pending_write_on_auth_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            401,
            json={"error": "unauthorized", "message": "Session not found or revoked"},
        )
    )
    client = _client_with_transport(transport)

    with pytest.raises(SibylClientError) as exc:
        await client.post("/memory/raw", json={"title": "Keep me", "raw_content": "Body"})

    await client.close()
    pending = pending_writes.list_pending_writes()
    assert len(pending) == 1
    assert pending[0]["path"] == "/memory/raw"
    assert exc.value.remediation == client_module.PENDING_WRITE_REMEDIATION


@pytest.mark.asyncio
async def test_mutating_request_deletes_pending_write_on_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            400,
            json={"error": "validation_error", "message": "Bad payload"},
        )
    )
    client = _client_with_transport(transport)

    with pytest.raises(SibylClientError):
        await client.post("/entities", json={"name": "Invalid"})

    await client.close()
    assert pending_writes.list_pending_writes() == []
