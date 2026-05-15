from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import typer

import sibyl_cli.client as client_module
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
