from __future__ import annotations

from sibyl_core.observability import TelemetryRegistry, safe_label


def test_api_request_metrics_include_summary_and_prometheus_output() -> None:
    registry = TelemetryRegistry(started_at=1.0)

    registry.record_api_request(
        method="get",
        route="/api/tasks/{task_id}",
        status_code=200,
        duration_ms=12.5,
    )
    registry.record_api_request(
        method="post",
        route="/api/tasks",
        status_code=503,
        duration_ms=25.0,
    )

    snapshot = registry.snapshot()
    api_summary = snapshot["summaries"]["api"]

    assert api_summary["count"] == 2
    assert api_summary["errors"] == 1
    assert api_summary["p95_ms"] > 0

    prometheus = registry.prometheus_text()
    assert "sibyl_api_requests_total" in prometheus
    assert 'route="/api/tasks/{task_id}"' in prometheus
    assert "sibyl_api_request_duration_ms{" in prometheus
    assert 'quantile="0.5"' in prometheus
    assert prometheus.count("# TYPE sibyl_api_requests_total counter") == 1


def test_surreal_query_metrics_do_not_expose_query_text_or_params() -> None:
    registry = TelemetryRegistry()

    registry.record_surreal_query(
        client="graph",
        database="graph",
        statement="select",
        query_hash="abc123",
        elapsed_ms=900.0,
        status="ok",
        slow=True,
    )

    payload = str(registry.snapshot())

    assert "abc123" in payload
    assert "SELECT" not in payload
    assert "secret" not in payload
    assert registry.snapshot()["summaries"]["surreal"]["slow"] == 1


def test_safe_label_bounds_cardinality_shape() -> None:
    assert (
        safe_label(" /api/memory/raw?token=secret value ") == "/api/memory/raw_token_secret_value"
    )
    assert len(safe_label("x" * 500)) == 120
