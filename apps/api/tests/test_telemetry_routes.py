from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from sibyl import main as main_module
from sibyl.api.routes import telemetry as telemetry_routes
from sibyl.jobs import worker as worker_module
from sibyl.services import telemetry as telemetry_service
from sibyl_core.observability import telemetry_registry


@pytest.mark.asyncio
async def test_telemetry_summary_includes_runtime_and_queue_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = telemetry_registry()
    registry.reset()
    registry.record_api_request(
        method="GET",
        route="/api/health",
        status_code=200,
        duration_ms=15,
    )

    async def fake_health() -> dict[str, object]:
        return {
            "backend": "local",
            "queue_backend": "local",
            "queue_depth": 2,
            "queue_healthy": True,
            "worker_healthy": True,
        }

    async def fake_maybe_persist_runtime_rollup(*, window_seconds: int) -> None:
        assert window_seconds == 60

    async def fake_list_runtime_rollups(*, limit: int) -> list[dict[str, object]]:
        assert limit == 1
        return [{"bucket_key": "202605162212"}]

    monkeypatch.setattr(telemetry_routes, "get_coordination_health", fake_health)
    monkeypatch.setattr(
        telemetry_routes,
        "maybe_persist_runtime_rollup",
        fake_maybe_persist_runtime_rollup,
    )
    monkeypatch.setattr(telemetry_routes, "list_runtime_rollups", fake_list_runtime_rollups)

    response = await telemetry_routes.telemetry_summary(window_seconds=60, rollup_limit=1)

    assert response.summaries["api"].count == 1
    assert response.summaries["queue"].count == 1
    assert response.rollups == [{"bucket_key": "202605162212"}]
    assert any(metric.name == "sibyl_queue_depth" for metric in response.metrics)


@pytest.mark.asyncio
async def test_telemetry_prometheus_returns_text_payload() -> None:
    registry = telemetry_registry()
    registry.reset()
    registry.record_api_request(
        method="GET",
        route="/api/health",
        status_code=200,
        duration_ms=15,
    )

    response = await telemetry_routes.telemetry_prometheus()

    assert response.media_type == "text/plain; version=0.0.4; charset=utf-8"
    assert "sibyl_api_requests_total" in response.body.decode()


def test_telemetry_prometheus_uses_global_admin_dependency() -> None:
    route = next(
        route for route in telemetry_routes.router.routes if route.path == "/telemetry/prometheus"
    )

    assert any(
        dependency.dependency is telemetry_routes.require_global_admin
        for dependency in route.dependencies
    )


def test_telemetry_summary_uses_global_admin_dependency() -> None:
    route = next(
        route for route in telemetry_routes.router.routes if route.path == "/telemetry/summary"
    )

    assert any(
        dependency.dependency is telemetry_routes.require_global_admin
        for dependency in route.dependencies
    )


@pytest.mark.asyncio
async def test_root_metrics_requires_scrape_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = telemetry_registry()
    registry.reset()
    registry.record_api_request(
        method="GET",
        route="/api/health",
        status_code=200,
        duration_ms=15,
    )
    monkeypatch.setattr(main_module.settings, "metrics_scrape_token", SecretStr("scrape-secret"))

    denied = await main_module._root_metrics(
        SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.10"))
    )
    allowed = await main_module._root_metrics(
        SimpleNamespace(
            headers={"authorization": "Bearer scrape-secret"},
            client=SimpleNamespace(host="203.0.113.10"),
        )
    )

    assert denied.status_code == 404
    assert allowed.status_code == 200
    assert "sibyl_api_requests_total" in allowed.body.decode()


@pytest.mark.asyncio
async def test_redis_worker_job_end_records_result_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = telemetry_registry()
    registry.reset()

    class FakeJob:
        def __init__(self, job_id: str, redis: object) -> None:
            assert job_id == "crawl:abc"
            assert redis is not None

        async def result_info(self) -> object:
            return SimpleNamespace(function="crawl_source", success=False)

    monkeypatch.setattr(worker_module, "Job", FakeJob)

    await worker_module.job_end(
        {
            "telemetry_started_at": time.perf_counter() - 0.001,
            "job_id": "crawl:abc",
            "redis": object(),
        }
    )

    snapshot = registry.snapshot(window_seconds=60)
    assert snapshot["summaries"]["jobs"]["errors"] == 1
    assert snapshot["recent_events"][-1]["labels"]["function"] == "crawl_source"


@pytest.mark.asyncio
async def test_runtime_rollup_scheduler_coalesces_inflight_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_maybe_persist_runtime_rollup(*, window_seconds: int) -> None:
        nonlocal calls
        assert window_seconds == 30
        calls += 1
        started.set()
        await release.wait()
        telemetry_service._last_persisted_bucket = telemetry_service._current_bucket()

    monkeypatch.setattr(
        telemetry_service,
        "maybe_persist_runtime_rollup",
        fake_maybe_persist_runtime_rollup,
    )
    monkeypatch.setattr(telemetry_service, "_last_persisted_bucket", None)
    monkeypatch.setattr(telemetry_service, "_next_scheduled_rollup_at", 0.0)
    monkeypatch.setattr(telemetry_service, "_scheduled_rollup_task", None)

    telemetry_service.schedule_runtime_rollup_persist(window_seconds=30)
    await started.wait()
    task = telemetry_service._scheduled_rollup_task
    assert task is not None

    telemetry_service.schedule_runtime_rollup_persist(window_seconds=30)

    assert calls == 1
    release.set()
    await task
    assert telemetry_service._scheduled_rollup_task is None


@pytest.mark.asyncio
async def test_runtime_rollup_scheduler_backs_off_after_noop_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_maybe_persist_runtime_rollup(*, window_seconds: int) -> None:
        nonlocal calls
        assert window_seconds == 45
        calls += 1

    monkeypatch.setattr(
        telemetry_service,
        "maybe_persist_runtime_rollup",
        fake_maybe_persist_runtime_rollup,
    )
    monkeypatch.setattr(telemetry_service, "_last_persisted_bucket", None)
    monkeypatch.setattr(telemetry_service, "_next_scheduled_rollup_at", 0.0)
    monkeypatch.setattr(telemetry_service, "_scheduled_rollup_task", None)

    telemetry_service.schedule_runtime_rollup_persist(window_seconds=45)
    task = telemetry_service._scheduled_rollup_task
    assert task is not None
    await task

    assert calls == 1
    assert telemetry_service._next_scheduled_rollup_at > time.monotonic()

    telemetry_service.schedule_runtime_rollup_persist(window_seconds=45)

    assert calls == 1


@pytest.mark.asyncio
async def test_rollup_persistence_failure_warns_once_per_class_and_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    class FailingClient:
        async def execute_query(self, query: str, **params: object) -> object:
            raise RuntimeError("upsert rejected")

    async def failing_client() -> FailingClient:
        return FailingClient()

    monkeypatch.setattr(telemetry_service, "get_shared_surreal_content_client", failing_client)
    monkeypatch.setattr(telemetry_service, "_last_persisted_bucket", None)
    monkeypatch.setattr(telemetry_service, "_logged_failure_buckets", {})

    with capture_logs() as entries:
        first = await telemetry_service.persist_runtime_rollup(bucket=60)
        monkeypatch.setattr(telemetry_service, "_last_persisted_bucket", None)
        second = await telemetry_service.persist_runtime_rollup(bucket=60)

    assert first is None
    assert second is None
    failures = [entry for entry in entries if entry["event"] == "runtime_telemetry_rollup_failed"]
    assert len(failures) == 2
    # The failure is named the first time in an interval and throttled after.
    assert [entry["log_level"] for entry in failures] == ["warning", "debug"]
    assert {entry["error_type"] for entry in failures} == {"RuntimeError"}

    counters = [
        metric
        for metric in telemetry_registry().snapshot(window_seconds=60)["metrics"]
        if metric["name"] == "sibyl_telemetry_rollup_failures_total"
    ]
    assert counters
    assert counters[0]["labels"]["error_type"] == "RuntimeError"
    assert counters[0]["value"] >= 2


@pytest.mark.asyncio
async def test_rollup_failure_warns_again_in_a_new_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    class FailingClient:
        async def execute_query(self, query: str, **params: object) -> object:
            raise TimeoutError("connect budget spent")

    async def failing_client() -> FailingClient:
        return FailingClient()

    monkeypatch.setattr(telemetry_service, "get_shared_surreal_content_client", failing_client)
    monkeypatch.setattr(telemetry_service, "_last_persisted_bucket", None)
    monkeypatch.setattr(telemetry_service, "_logged_failure_buckets", {})

    with capture_logs() as entries:
        await telemetry_service.persist_runtime_rollup(bucket=120)
        monkeypatch.setattr(telemetry_service, "_last_persisted_bucket", None)
        await telemetry_service.persist_runtime_rollup(bucket=180)

    levels = [
        entry["log_level"]
        for entry in entries
        if entry["event"] == "runtime_telemetry_rollup_failed"
    ]
    assert levels == ["warning", "warning"]


@pytest.mark.asyncio
async def test_rollup_failure_throttle_does_not_accumulate_error_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors = [RuntimeError("one"), TimeoutError("two"), ValueError("three")]

    class FailingClient:
        async def execute_query(self, query: str, **params: object) -> object:
            raise errors.pop(0)

    async def failing_client() -> FailingClient:
        return FailingClient()

    monkeypatch.setattr(telemetry_service, "get_shared_surreal_content_client", failing_client)
    monkeypatch.setattr(telemetry_service, "_logged_failure_buckets", {})

    for index in range(3):
        monkeypatch.setattr(telemetry_service, "_last_persisted_bucket", None)
        await telemetry_service.persist_runtime_rollup(bucket=240 + index * 60)

    # One entry per interval, not one per error class ever seen.
    assert len(telemetry_service._logged_failure_buckets) == 1
