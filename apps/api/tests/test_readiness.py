"""Readiness probe contract.

`/health` stays cheap liveness; `/health/ready` reports whether the serving
dependencies (SurrealDB reachability) are healthy. The dependency probe is
mocked so these tests never need a live runtime.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from sibyl.api.app import create_api_app
from sibyl.api.readiness import (
    DependencyStatus,
    ReadinessReport,
    check_readiness,
)
from sibyl.surreal_runtime_startup import (
    RuntimeSchemaBootstrapStatus,
    SchemaBootstrapFailure,
)


def test_liveness_health_stays_cheap_and_unauthenticated() -> None:
    client = TestClient(create_api_app(), raise_server_exceptions=False)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert "version" in body
    assert set(body["runtime"]) == {
        "commit",
        "commit_source",
        "git_dirty",
        "git_status",
        "dirty_source",
    }


def test_readiness_returns_200_when_dependencies_ready() -> None:
    client = TestClient(create_api_app(), raise_server_exceptions=False)
    ready = DependencyStatus(name="surrealdb", ready=True, latency_ms=1.5)

    with patch(
        "sibyl.api.readiness.check_surreal_ready",
        AsyncMock(return_value=ready),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["dependencies"] == [{"name": "surrealdb", "ready": True, "latency_ms": 1.5}]


def test_readiness_returns_503_when_dependency_unreachable() -> None:
    client = TestClient(create_api_app(), raise_server_exceptions=False)
    down = DependencyStatus(
        name="surrealdb",
        ready=False,
        detail="SurrealDB runtime unreachable",
    )

    with patch(
        "sibyl.api.readiness.check_surreal_ready",
        AsyncMock(return_value=down),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"][0]["name"] == "surrealdb"
    assert body["dependencies"][0]["ready"] is False
    assert body["dependencies"][0]["detail"] == "SurrealDB runtime unreachable"


@pytest.mark.asyncio
async def test_check_surreal_ready_probes_connect_only() -> None:
    """The probe connects (handshake) and closes without running a query."""
    fake_client = AsyncMock()

    with patch(
        "sibyl.persistence.surreal.auth.build_surreal_auth_client",
        return_value=fake_client,
    ):
        from sibyl.api.readiness import check_surreal_ready

        status = await check_surreal_ready()

    assert status.ready is True
    fake_client.connect.assert_awaited_once()
    fake_client.close.assert_awaited_once()
    assert not fake_client.execute_query.await_count


@pytest.mark.asyncio
async def test_check_surreal_ready_reports_unreachable_on_connect_failure() -> None:
    fake_client = AsyncMock()
    fake_client.connect.side_effect = ConnectionError("boom")

    with patch(
        "sibyl.persistence.surreal.auth.build_surreal_auth_client",
        return_value=fake_client,
    ):
        from sibyl.api.readiness import check_surreal_ready

        status = await check_surreal_ready()

    assert status.ready is False
    assert status.detail == "SurrealDB runtime unreachable"
    fake_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_readiness_aggregates_dependency_status() -> None:
    down = DependencyStatus(name="surrealdb", ready=False, detail="nope")

    with patch(
        "sibyl.api.readiness.check_surreal_ready",
        AsyncMock(return_value=down),
    ):
        report = await check_readiness()

    assert isinstance(report, ReadinessReport)
    assert report.ready is False
    assert report.as_payload()["status"] == "not_ready"


@pytest.mark.asyncio
async def test_check_readiness_reports_schema_bootstrap_failure() -> None:
    ready = DependencyStatus(name="surrealdb", ready=True, latency_ms=1.5)
    schema_status = RuntimeSchemaBootstrapStatus(
        attempted=True,
        auth_ready=False,
        content_ready=True,
        failures=(
            SchemaBootstrapFailure(
                plane="auth",
                target_version=1,
                error="auth offline",
            ),
        ),
    )

    with (
        patch("sibyl.api.readiness.check_surreal_ready", AsyncMock(return_value=ready)),
        patch(
            "sibyl.surreal_runtime_startup.get_runtime_schema_bootstrap_status",
            return_value=schema_status,
        ),
    ):
        report = await check_readiness()

    assert report.ready is False
    payload = report.as_payload()
    assert payload["status"] == "not_ready"
    assert payload["dependencies"][1] == {
        "name": "schemas",
        "ready": False,
        "detail": "auth v1: auth offline",
    }
