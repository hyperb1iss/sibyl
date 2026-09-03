"""Readiness probe contract.

`/health` stays cheap liveness; `/health/ready` reports whether the serving
database and coordination dependencies are healthy. The dependency probes
are mocked so these tests never need a live runtime.
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
    coordination = DependencyStatus(
        name="coordination",
        ready=True,
        latency_ms=0.4,
        backend="local",
    )

    schemas = DependencyStatus(name="schemas", ready=True)

    with (
        patch(
            "sibyl.api.readiness.check_surreal_ready",
            AsyncMock(return_value=ready),
        ),
        patch(
            "sibyl.api.readiness.check_coordination_ready",
            AsyncMock(return_value=coordination),
        ),
        patch(
            "sibyl.api.readiness.check_schema_bootstrap_ready",
            return_value=schemas,
        ),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["dependencies"] == [
        {"name": "surrealdb", "ready": True, "latency_ms": 1.5},
        {
            "name": "coordination",
            "ready": True,
            "latency_ms": 0.4,
            "backend": "local",
        },
        {"name": "schemas", "ready": True},
    ]


def test_readiness_returns_503_when_dependency_unreachable() -> None:
    client = TestClient(create_api_app(), raise_server_exceptions=False)
    down = DependencyStatus(
        name="surrealdb",
        ready=False,
        detail="SurrealDB runtime unreachable",
    )

    coordination = DependencyStatus(name="coordination", ready=True, backend="local")
    with (
        patch(
            "sibyl.api.readiness.check_surreal_ready",
            AsyncMock(return_value=down),
        ),
        patch(
            "sibyl.api.readiness.check_coordination_ready",
            AsyncMock(return_value=coordination),
        ),
    ):
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"][0]["name"] == "surrealdb"
    assert body["dependencies"][0]["ready"] is False
    assert body["dependencies"][0]["detail"] == "SurrealDB runtime unreachable"


def test_dead_required_broker_fails_readiness_but_not_liveness() -> None:
    client = TestClient(create_api_app(), raise_server_exceptions=False)
    surreal = DependencyStatus(name="surrealdb", ready=True, latency_ms=1.5)
    coordination = DependencyStatus(
        name="coordination",
        ready=False,
        detail="redis coordination broker unavailable",
        latency_ms=2.0,
        backend="redis",
    )

    with (
        patch(
            "sibyl.api.readiness.check_surreal_ready",
            AsyncMock(return_value=surreal),
        ),
        patch(
            "sibyl.api.readiness.check_coordination_ready",
            AsyncMock(return_value=coordination),
        ),
    ):
        ready_response = client.get("/health/ready")
        live_response = client.get("/health")

    assert ready_response.status_code == 503
    assert ready_response.json()["dependencies"][1] == {
        "name": "coordination",
        "ready": False,
        "detail": "redis coordination broker unavailable",
        "latency_ms": 2.0,
        "backend": "redis",
    }
    assert live_response.status_code == 200
    assert live_response.json()["status"] == "healthy"


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
    coordination = DependencyStatus(name="coordination", ready=True, backend="local")

    with (
        patch(
            "sibyl.api.readiness.check_surreal_ready",
            AsyncMock(return_value=down),
        ),
        patch(
            "sibyl.api.readiness.check_coordination_ready",
            AsyncMock(return_value=coordination),
        ),
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
            "sibyl.api.readiness.check_coordination_ready",
            AsyncMock(
                return_value=DependencyStatus(
                    name="coordination",
                    ready=True,
                    backend="local",
                )
            ),
        ),
        patch(
            "sibyl.surreal_runtime_startup.get_runtime_schema_bootstrap_status",
            return_value=schema_status,
        ),
    ):
        report = await check_readiness()

    assert report.ready is False
    payload = report.as_payload()
    assert payload["status"] == "not_ready"
    assert payload["dependencies"][2] == {
        "name": "schemas",
        "ready": False,
        "detail": "auth v1: auth offline",
    }


@pytest.mark.asyncio
async def test_coordination_readiness_accepts_healthy_local_mode() -> None:
    health = {
        "status": "healthy",
        "backend": "local",
        "queue_healthy": True,
        "worker_healthy": True,
    }
    with (
        patch("sibyl.coordination.get_coordination_backend", return_value="local"),
        patch(
            "sibyl.coordination.get_coordination_health",
            AsyncMock(return_value=health),
        ),
    ):
        from sibyl.api.readiness import check_coordination_ready

        status = await check_coordination_ready()

    assert status.ready is True
    assert status.backend == "local"
    assert status.detail is None
    assert status.latency_ms is not None


@pytest.mark.asyncio
async def test_coordination_readiness_rejects_dead_required_redis() -> None:
    health = {
        "status": "unhealthy",
        "backend": "redis",
        "queue_healthy": False,
        "worker_healthy": False,
    }
    with (
        patch("sibyl.coordination.get_coordination_backend", return_value="redis"),
        patch(
            "sibyl.coordination.get_coordination_health",
            AsyncMock(return_value=health),
        ),
    ):
        from sibyl.api.readiness import check_coordination_ready

        status = await check_coordination_ready()

    assert status.ready is False
    assert status.backend == "redis"
    assert status.detail == "redis coordination broker unavailable"
    assert status.latency_ms is not None


def test_schema_bootstrap_unattempted_reports_not_ready() -> None:
    """Before startup marks the bootstrap attempted, readiness must say
    NOT ready rather than dropping the dependency: the silent-drop
    behavior opened a first-boot window where /health/ready answered 200
    while the auth schema did not exist and signups 500ed (#461)."""
    from sibyl.api.readiness import check_schema_bootstrap_ready
    from sibyl.surreal_runtime_startup import reset_runtime_schema_bootstrap_status

    reset_runtime_schema_bootstrap_status()
    status = check_schema_bootstrap_ready()

    assert status is not None
    assert status.ready is False
    assert "not started" in (status.detail or "")
