from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import typer

import sibyl_cli.client as client_module
import sibyl_cli.client_transport as client_transport_module
from sibyl_cli import config_store, pending_writes
from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.common import handle_client_error


def _client_with_transport(transport: httpx.MockTransport) -> SibylClient:
    client = SibylClient(base_url="http://testserver/api", auth_token="token")

    async def legacy_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(404, json={"detail": "Not Found"})
        return await transport.handle_async_request(request)

    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(legacy_transport),
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
    monkeypatch.setattr(client_transport_module.sys, "argv", ["sibyl", "add"])
    sleep = AsyncMock()
    monkeypatch.setattr(client_transport_module, "anyio_sleep", sleep)
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
async def test_successful_api_request_replays_buffered_writes_automatically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_GRACE_SECONDS", 0.0)
    requests: list[tuple[str, str, str | None]] = []

    def healthy_then_replay(request: httpx.Request) -> httpx.Response:
        requests.append(
            (
                request.method,
                request.url.path,
                request.headers.get("Idempotency-Key"),
            )
        )
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(httpx.MockTransport(healthy_then_replay))
    queued = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={"title": "Recover me", "raw_content": "Body"},
        params=None,
        replay_scope=client._replay_scope,
    )
    result = await client.get("/health")

    await client.close()
    assert result == {"ok": True}
    assert requests == [
        ("GET", "/api/health", None),
        ("POST", "/api/memory/raw", queued["idempotency_key"]),
    ]
    assert pending_writes.list_pending_writes() == []
    assert pending_writes.read_pending_metrics()["replayed"] == 1


@pytest.mark.asyncio
async def test_failed_automatic_replay_does_not_fail_the_triggering_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_GRACE_SECONDS", 0.0)

    def healthy_but_replay_fails(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(
            500,
            json={"error": "internal_error", "message": "SurrealDB unavailable"},
        )

    client = _client_with_transport(httpx.MockTransport(healthy_but_replay_fails))
    pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={"title": "Keep me", "raw_content": "Body"},
        params=None,
        replay_scope=client._replay_scope,
    )
    result = await client.get("/health")

    await client.close()
    queued = pending_writes.list_pending_writes()
    assert result == {"status": "healthy"}
    assert len(queued) == 1
    assert queued[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_automatic_replay_attempts_independent_writes_after_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(client_transport_module, "time", SimpleNamespace(monotonic=lambda: 100.0))
    client_transport_module._FAILURE_WINDOWS.clear()
    posts = 0
    monkeypatch.setattr(client_transport_module, "anyio_sleep", AsyncMock())

    def healthy_then_unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(200, json={"status": "healthy"})
        posts += 1
        return httpx.Response(
            503,
            json={"error": "unavailable", "message": "SurrealDB unavailable"},
        )

    client = _client_with_transport(httpx.MockTransport(healthy_then_unavailable))
    for index in range(8):
        pending_writes.create_pending_write(
            method="POST",
            path="/memory/raw",
            base_url=client.base_url,
            json_payload={"title": f"queued {index}", "raw_content": "Body"},
            params=None,
            replay_scope=client._replay_scope,
        )

    result = await client.get("/health")

    await client.close()
    attempts = sorted(int(item["attempts"]) for item in pending_writes.list_pending_writes())
    assert result == {"status": "healthy"}
    assert posts == 8
    assert attempts == [1] * 8
    key = client_transport_module._failure_key(client.base_url)
    assert len(client_transport_module._FAILURE_WINDOWS[key]) == 8
    client_transport_module._FAILURE_WINDOWS.clear()


@pytest.mark.asyncio
async def test_buffered_reflection_does_not_block_a_new_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    requests: list[httpx.Request] = []

    def healthy(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "new-task"})

    client = _client_with_transport(httpx.MockTransport(healthy))
    reflection = pending_writes.create_pending_write(
        method="POST",
        path="/context/reflect",
        base_url=client.base_url,
        json_payload={"content": "Preserve this reflection"},
        params=None,
        replay_scope=client._replay_scope,
    )
    pending_writes.record_pending_failure(reflection["id"], category="rejected", status_code=404)

    result = await client.post("/tasks", json={"name": "Independent task"})
    await client.close()

    assert result == {"id": "new-task"}
    assert [(request.method, request.url.path) for request in requests] == [("POST", "/api/tasks")]
    remaining = pending_writes.list_pending_writes()
    assert [item["id"] for item in remaining] == [reflection["id"]]
    assert remaining[0]["status"] == "attention"


@pytest.mark.asyncio
async def test_automatic_replay_does_not_pass_an_older_write_in_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_BACKOFF_BASE_SECONDS", 60.0)
    requests: list[str] = []

    def healthy(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json={"status": "healthy"})

    client = _client_with_transport(httpx.MockTransport(healthy))
    older = pending_writes.create_pending_write(
        method="POST",
        path="/entities",
        base_url=client.base_url,
        json_payload={"id": "entity_123", "name": "Create first"},
        params=None,
        replay_scope=client._replay_scope,
    )
    pending_writes.increment_attempts(str(older["id"]))
    newer = pending_writes.create_pending_write(
        method="PATCH",
        path="/entities/entity_123",
        base_url=client.base_url,
        json_payload={"name": "Update second"},
        params=None,
        replay_scope=client._replay_scope,
    )

    result = await client.get("/health")

    await client.close()
    queued = pending_writes.list_pending_writes()
    attempts = {str(item["id"]): int(item["attempts"]) for item in queued}
    assert result == {"status": "healthy"}
    assert requests == ["GET"]
    assert attempts == {str(older["id"]): 1, str(newer["id"]): 0}


@pytest.mark.asyncio
async def test_automatic_replay_never_crosses_credential_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_GRACE_SECONDS", 0.0)
    requests: list[str] = []

    def healthy(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json={"status": "healthy"})

    client = _client_with_transport(httpx.MockTransport(healthy))
    pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=client.base_url,
        json_payload={"title": "Org alpha", "raw_content": "Body"},
        params=None,
        replay_scope=client_transport_module._auth_replay_scope(None, "token-for-org-alpha"),
    )

    result = await client.get("/health")

    await client.close()
    assert result == {"status": "healthy"}
    assert requests == ["GET"]
    assert len(pending_writes.list_pending_writes()) == 1


@pytest.mark.asyncio
async def test_environment_token_scope_overrides_active_context_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_GRACE_SECONDS", 0.0)
    monkeypatch.delenv("SIBYL_API_URL", raising=False)
    monkeypatch.setenv("SIBYL_AUTH_TOKEN", "automation-token")
    monkeypatch.setattr(
        client_module,
        "_auth_credential_scope",
        lambda _context_name: "context:alpha:org:alpha-org",
    )
    monkeypatch.setattr(
        SibylClient,
        "_get_insecure_from_context",
        lambda _self, _context_name: False,
    )
    requests: list[str] = []

    def healthy(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json={"status": "healthy"})

    client = SibylClient(base_url="http://testserver/api", context_name="alpha")
    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(healthy),
        headers=client._default_headers(),
    )
    pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=client.base_url,
        json_payload={"title": "Alpha only", "raw_content": "Body"},
        params=None,
        replay_scope="context:alpha:org:alpha-org",
    )

    result = await client.get("/health")

    await client.close()
    assert result == {"status": "healthy"}
    assert client._replay_scope == client_transport_module._auth_replay_scope(
        None, "automation-token"
    )
    assert requests == ["GET"]
    assert len(pending_writes.list_pending_writes()) == 1


@pytest.mark.asyncio
async def test_automatic_replay_uses_backoff_without_abandoning_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(client_transport_module, "AUTO_REPLAY_BACKOFF_BASE_SECONDS", 0.0)
    posts = 0

    def recovered(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
        return httpx.Response(200, json={"status": "healthy"})

    client = _client_with_transport(httpx.MockTransport(recovered))
    queued = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=client.base_url,
        json_payload={"title": "Eventually", "raw_content": "Body"},
        params=None,
        replay_scope=client._replay_scope,
    )
    for _ in range(5):
        pending_writes.increment_attempts(str(queued["id"]))

    result = await client.get("/health")

    await client.close()
    assert result == {"status": "healthy"}
    assert posts == 1
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
async def test_expired_token_refresh_failure_skips_expired_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_requests = 0

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        nonlocal sent_requests
        sent_requests += 1
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(httpx.MockTransport(unexpected_request))
    client._uses_stored_auth = True
    refresh = AsyncMock(return_value=(False, "Authentication storage temporarily unavailable"))
    monkeypatch.setattr(
        client_transport_module,
        "is_access_token_expired",
        lambda _api_url, **_kwargs: True,
    )
    monkeypatch.setattr(client, "_refresh_token", refresh)

    with pytest.raises(SibylClientError) as exc:
        await client.get("/entities")

    await client.close()
    refresh.assert_awaited_once()
    assert sent_requests == 0
    assert exc.value.status_code == 503
    assert exc.value.error_code == "token_refresh_failed"
    assert exc.value.detail == "Authentication storage temporarily unavailable"


@pytest.mark.asyncio
async def test_expired_token_refresh_failure_keeps_pending_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    sent_requests = 0

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        nonlocal sent_requests
        sent_requests += 1
        return httpx.Response(200, json={"ok": True})

    client = _client_with_transport(httpx.MockTransport(unexpected_request))
    client._uses_stored_auth = True
    refresh = AsyncMock(return_value=(False, "Authentication storage temporarily unavailable"))
    monkeypatch.setattr(
        client_transport_module,
        "is_access_token_expired",
        lambda _api_url, **_kwargs: True,
    )
    monkeypatch.setattr(client, "_refresh_token", refresh)

    with pytest.raises(SibylClientError) as exc:
        await client.post("/memory/raw", json={"title": "Keep me", "raw_content": "Body"})

    await client.close()
    pending = pending_writes.list_pending_writes()
    assert sent_requests == 0
    assert len(pending) == 1
    assert pending[0]["path"] == "/memory/raw"
    assert exc.value.remediation == client_module.PENDING_WRITE_REMEDIATION


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
async def test_mutating_request_parks_pending_write_on_validation_error(
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
    item = pending_writes.list_pending_writes()[0]
    assert item["status"] == "attention"
    assert item["last_failure"]["category"] == "rejected"
    assert pending_writes.read_pending_metrics()["dropped"] == 0


@pytest.mark.parametrize("status_code", [400, 422])
@pytest.mark.asyncio
async def test_mutating_request_preserves_rejected_payload_for_inspection(
    status_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            status_code,
            json={"error": "validation_error", "message": "Bad payload"},
        )
    )
    client = _client_with_transport(transport)

    with pytest.raises(SibylClientError):
        await client.post("/memory/raw", json={"title": "Doomed", "raw_content": "Body"})

    await client.close()
    item = pending_writes.list_pending_writes()[0]
    assert item["status"] == "attention"
    assert item["last_failure"]["category"] == "rejected"
    assert pending_writes.read_pending_metrics()["dropped"] == 0


@pytest.mark.asyncio
async def test_mutating_request_keeps_pending_write_on_idempotency_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            409,
            json={
                "error": "conflict",
                "message": (
                    "Idempotent operation is still in progress or was interrupted "
                    "before its receipt completed"
                ),
            },
        )
    )
    client = _client_with_transport(transport)

    with pytest.raises(SibylClientError):
        await client.post("/memory/raw", json={"title": "Keep me", "raw_content": "Body"})

    await client.close()
    pending = pending_writes.list_pending_writes()
    assert len(pending) == 1
    assert pending[0]["json"] == {"title": "Keep me", "raw_content": "Body"}
    assert pending_writes.read_pending_metrics()["dropped"] == 0


@pytest.mark.parametrize("status_code", [401, 403, 404, 405, 408, 409, 410, 413, 429, 500, 503])
@pytest.mark.asyncio
async def test_mutating_request_keeps_pending_write_unless_payload_is_unusable(
    status_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            status_code,
            json={"error": "failed", "message": "Not applied, or unknowable"},
        )
    )
    client = _client_with_transport(transport)

    with pytest.raises(SibylClientError):
        await client.post("/memory/raw", json={"title": "Keep me", "raw_content": "Body"})

    await client.close()
    assert len(pending_writes.list_pending_writes()) == 1
    assert pending_writes.read_pending_metrics()["dropped"] == 0


@pytest.mark.asyncio
async def test_mutating_request_records_completed_pending_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"id": "entity_1"}))
    client = _client_with_transport(transport)

    await client.post("/memory/raw", json={"title": "Landed", "raw_content": "Body"})

    await client.close()
    metrics = pending_writes.read_pending_metrics()
    assert pending_writes.list_pending_writes() == []
    assert metrics["attempted"] == 1
    assert metrics["completed"] == 1
    assert metrics["dropped"] == 0


def test_read_pending_metrics_folds_retired_expired_into_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    path = pending_writes.pending_metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"attempted": 10, "discarded": 3, "expired": 4, "replayed": 3}),
        encoding="utf-8",
    )

    metrics = pending_writes.read_pending_metrics()

    assert metrics["discarded"] == 7
    assert "expired" not in metrics
    outcomes = (
        metrics["completed"] + metrics["replayed"] + metrics["dropped"] + metrics["discarded"]
    )
    assert outcomes == metrics["attempted"]


@pytest.mark.parametrize("status_code", [500, 403, 404, 422])
@pytest.mark.asyncio
async def test_a_failed_correction_preview_queues_nothing_and_blocks_nothing(
    status_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preview writes nothing, so a failed one has nothing to replay.

    Buffered, it used to sit under one key for every /memory/inspect write, so
    each later correction on any memory failed with a pending dependency.
    """
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    sent: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        if request.url.path.endswith("/mem-a/corrections/preview"):
            return httpx.Response(status_code, json={"error": "x", "message": "boom"})
        return httpx.Response(200, json={"allowed": True, "applied": False})

    client = _client_with_transport(httpx.MockTransport(respond))

    with pytest.raises(SibylClientError):
        await client.correct_memory("mem-a", action="hide", reason="r", preview=True)
    other = await client.correct_memory("mem-b", action="hide", reason="r", preview=True)
    await client.close()

    assert other["allowed"] is True
    assert pending_writes.list_pending_writes() == []
    assert sent == [
        "/api/memory/inspect/mem-a/corrections/preview",
        "/api/memory/inspect/mem-b/corrections/preview",
    ]


@pytest.mark.asyncio
async def test_a_preview_queued_by_an_older_client_blocks_no_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    stale = pending_writes.create_pending_write(
        method="POST",
        path="/memory/inspect/mem-a/corrections/preview",
        base_url="http://testserver/api",
        json_payload={"action": "hide", "reason": "r"},
        params=None,
    )
    pending_writes.record_pending_failure(str(stale["id"]), category="rejected", status_code=404)

    client = _client_with_transport(
        httpx.MockTransport(lambda _request: httpx.Response(200, json={"applied": True}))
    )
    applied = await client.correct_memory("mem-a", action="hide", reason="r")
    await client.close()

    assert applied["applied"] is True


@pytest.mark.asyncio
async def test_a_first_delete_answered_404_reports_it_and_queues_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    client = _client_with_transport(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                404, json={"error": "not_found", "message": "Entity not found: x"}
            )
        )
    )

    with pytest.raises(SibylClientError) as exc:
        await client.delete_entity("3f1c2a9e-0000-4000-8000-000000000000")
    await client.close()

    assert exc.value.status_code == 404
    assert pending_writes.list_pending_writes() == []
    assert pending_writes.read_pending_metrics()["dropped"] == 1


@pytest.mark.asyncio
async def test_a_replayed_delete_answered_404_is_done_not_parked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued delete that already landed replays to a 404; the resource is gone."""
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    answers = [
        httpx.Response(503, json={"error": "service_unavailable", "message": "down"}),
        httpx.Response(404, json={"error": "not_found", "message": "Entity not found: note_gone"}),
    ]
    client = _client_with_transport(httpx.MockTransport(lambda _request: answers.pop(0)))

    with pytest.raises(SibylClientError):
        await client.delete_entity("note_gone")
    (queued,) = pending_writes.list_pending_writes()
    assert queued["status"] == "pending"

    result = await client._request(
        "DELETE",
        "/entities/note_gone",
        _buffer_pending=False,
        _pending_write_id=str(queued["id"]),
        _idempotency_key=str(queued["idempotency_key"]),
    )
    await client.close()

    assert result == {}
    assert pending_writes.list_pending_writes() == []


_REVISION_CONFLICT = {
    "error": "revision_conflict",
    "message": (
        "The memory changed since revision 3 and is now at revision 4; "
        "rerun to see the current state."
    ),
    "remediation": "Rerun the command to preview the current state, then apply again.",
    "details": {"expected": "3", "actual": "4"},
}


@pytest.mark.asyncio
async def test_a_first_write_that_meets_a_revision_conflict_is_not_queued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned to a revision the memory has left, the write can never apply.

    Queued, it parked under the memory's key and blocked the very rerun that
    would draw a fresh plan, and the rerun queued a second pinned write.
    """
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    client = _client_with_transport(
        httpx.MockTransport(lambda _request: httpx.Response(409, json=_REVISION_CONFLICT))
    )

    with pytest.raises(SibylClientError) as exc:
        await client.correct_memory("mem-a", action="hide", reason="r", expected_revision=3)
    await client.close()

    assert exc.value.error_code == "revision_conflict"
    assert exc.value.details == {"expected": "3", "actual": "4"}
    assert pending_writes.list_pending_writes() == []
    assert pending_writes.read_pending_metrics()["dropped"] == 1


@pytest.mark.asyncio
async def test_a_replayed_write_that_meets_a_revision_conflict_parks_with_the_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    answers = [
        httpx.Response(503, json={"error": "service_unavailable", "message": "down"}),
        httpx.Response(409, json=_REVISION_CONFLICT),
    ]
    client = _client_with_transport(httpx.MockTransport(lambda _request: answers.pop(0)))

    with pytest.raises(SibylClientError):
        await client.correct_memory("mem-a", action="hide", reason="r", expected_revision=3)
    (queued,) = pending_writes.list_pending_writes()
    with pytest.raises(SibylClientError):
        await client._request(
            "POST",
            str(queued["path"]),
            json=queued.get("json"),
            _buffer_pending=False,
            _pending_write_id=str(queued["id"]),
            _idempotency_key=str(queued["idempotency_key"]),
        )
    await client.close()

    (parked,) = pending_writes.list_pending_writes()
    assert parked["status"] == "attention"
    assert parked["last_failure"]["error_code"] == "revision_conflict"
    assert "revision 3 and is now at revision 4" in parked["last_failure"]["message"]


@pytest.mark.parametrize(
    ("path", "options"),
    [("/auth/api-keys/k1", {}), ("/entities/e1", {"_buffer_pending": False})],
)
@pytest.mark.asyncio
async def test_an_unbuffered_delete_answered_404_still_raises(
    path: str,
    options: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a queued delete treats 404 as done; an unbuffered caller needs the answer."""
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    client = _client_with_transport(
        httpx.MockTransport(
            lambda _request: httpx.Response(404, json={"error": "not_found", "message": "gone"})
        )
    )

    with pytest.raises(SibylClientError) as exc:
        await client._request("DELETE", path, **options)
    await client.close()

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_supersede_waits_for_an_earlier_queued_write_to_its_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supersede of A by B reads B, so it cannot overtake a queued delete of B."""
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_module._FAILURE_WINDOWS.clear()
    sent: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        if request.url.path.endswith("/mem-b/corrections"):
            return httpx.Response(503, json={"error": "service_unavailable", "message": "down"})
        return httpx.Response(200, json={"applied": True})

    client = _client_with_transport(httpx.MockTransport(respond))
    with pytest.raises(SibylClientError):
        await client.correct_memory("mem-b", action="delete", reason="gone")

    with pytest.raises(SibylClientError) as blocked:
        await client.correct_memory(
            "mem-a", action="supersede", reason="replaced", replacement_source_id="mem-b"
        )
    unrelated = await client.correct_memory("mem-c", action="hide", reason="noise")
    await client.close()

    assert blocked.value.error_code == "pending_dependency"
    # The supersede never left the machine; a memory neither write names is free.
    assert "/api/memory/inspect/mem-a/corrections" not in sent
    assert unrelated["applied"] is True
