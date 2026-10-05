"""Health must prove serving readiness before declaring the server healthy."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from sibyl_cli.client import SibylClient
from sibyl_cli.main import app


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
@pytest.mark.parametrize("failure", ["storage", "timeout", "missing"])
def test_health_rejects_live_server_without_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, json_flag: list[str], failure: str
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "healthy", "version": "1.5.0"})
        assert request.url.path == "/api/health/ready"
        if failure == "timeout":
            raise httpx.ReadTimeout("Storage probe timed out", request=request)
        if failure == "missing":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(
            503,
            json={
                "status": "not_ready",
                "dependencies": [
                    {"name": "surrealdb", "ready": False, "detail": "runtime unreachable"}
                ],
            },
        )

    client = SibylClient(base_url="http://health-test.invalid/api", auth_token="test-health")
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr("sibyl_cli.main.get_client", lambda: client)

    result = CliRunner().invoke(app, ["health", *json_flag])

    assert result.exit_code == 1, result.output
    assert "is healthy" not in result.output
    assert requests == ["/api/health/ready"]
    if failure == "storage":
        assert "surrealdb" in result.output
    if json_flag:
        assert json.loads(result.stdout)["status"] == "unhealthy"


@pytest.mark.parametrize("readiness_status", ["ready", "not_ready", "unknown"])
@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_health_combines_readiness_with_server_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    readiness_status: str,
    json_flag: list[str],
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    requests: list[str] = []
    ready = readiness_status == "ready"
    dependencies = [{"name": "surrealdb", "ready": ready}]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/health/ready":
            return httpx.Response(
                200, json={"status": readiness_status, "dependencies": dependencies}
            )
        assert request.url.path == "/api/health"
        return httpx.Response(200, json={"status": "healthy", "version": "1.5.0"})

    client = SibylClient(base_url="http://health-test.invalid/api", auth_token="test-health")
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr("sibyl_cli.main.get_client", lambda: client)

    result = CliRunner().invoke(app, ["health", *json_flag])

    assert result.exit_code == (0 if ready else 1), result.output
    assert requests == ["/api/health/ready", "/api/health"]
    if json_flag:
        payload = json.loads(result.stdout)
        assert payload["status"] == ("healthy" if ready else "not_ready")
        assert payload["version"] == "1.5.0"
        assert payload["dependencies"] == dependencies
    else:
        assert ("is healthy" in result.output) is ready
