from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from tools.perf.multi_user import (
    DEFAULT_API_BASE_URL,
    PerfConfig,
    RequestSample,
    build_report,
    check_thresholds,
    config_from_args,
    format_summary,
    percentile,
    write_report,
)

RUN_ID = "20260518120000-test"
TOTAL_REQUESTS = 4
SUCCESSFUL_REQUESTS = 3
FAILED_REQUESTS = 1
ERROR_RATE = 0.25
PERCENTILE_EXAMPLE_RESULT = 25.0
P95_LATENCY_MS = 355.0
MAX_PASSING_P95_MS = 500.0
MAX_FAILING_P95_MS = 100.0
SEARCH_FAILURES = 0
CONTEXT_PACK_FAILURES = 1
USER_REQUESTS = 2
CLI_USERS = 3
CLI_ITERATIONS = 2
CLI_REQUEST_TIMEOUT = 5.0
CLI_MAX_ERROR_RATE = 0.1
CLI_MAX_P95_MS = 1000.0


def _config(tmp_path: Path, *, max_error_rate: float = 0.0) -> PerfConfig:
    return PerfConfig(
        api_base_url=DEFAULT_API_BASE_URL,
        users=2,
        iterations=1,
        warmup_iterations=0,
        request_timeout=30.0,
        output_path=tmp_path / "perf.json",
        run_id=RUN_ID,
        email_domain="perf.sibyl.dev",
        max_error_rate=max_error_rate,
        max_p95_ms=MAX_PASSING_P95_MS,
    )


def _samples() -> list[RequestSample]:
    return [
        RequestSample("search", 0, 0, 10.0, True, 200),
        RequestSample("search", 1, 0, 20.0, True, 200),
        RequestSample("context_pack", 0, 0, 100.0, True, 200),
        RequestSample("context_pack", 1, 0, 400.0, False, 500, "boom"),
    ]


def test_percentile_interpolates_sorted_latency() -> None:
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.5) == PERCENTILE_EXAMPLE_RESULT
    assert percentile([], 0.95) is None


def test_build_report_groups_samples_by_operation_and_user(tmp_path: Path) -> None:
    config = _config(tmp_path, max_error_rate=ERROR_RATE)

    report = build_report(
        config=config,
        samples=_samples(),
        auth_samples=[],
        duration_seconds=2.0,
        setup_duration_seconds=0.5,
    )

    assert report["summary"]["total_requests"] == TOTAL_REQUESTS
    assert report["summary"]["successful_requests"] == SUCCESSFUL_REQUESTS
    assert report["summary"]["failed_requests"] == FAILED_REQUESTS
    assert report["summary"]["error_rate"] == ERROR_RATE
    assert report["summary"]["latency_ms"]["p95"] == pytest.approx(P95_LATENCY_MS)
    assert report["operations"]["search"]["failed_requests"] == SEARCH_FAILURES
    assert report["operations"]["context_pack"]["failed_requests"] == CONTEXT_PACK_FAILURES
    assert report["users"]["0"]["total_requests"] == USER_REQUESTS


def test_threshold_check_reports_error_rate_and_latency_failures(tmp_path: Path) -> None:
    config = _config(tmp_path)
    report = build_report(
        config=config,
        samples=_samples(),
        auth_samples=[],
        duration_seconds=1.0,
        setup_duration_seconds=0.1,
    )

    failures = check_thresholds(report, config)

    assert failures == ["error rate 0.250 exceeded threshold 0.000"]

    strict_config = replace(
        config,
        max_error_rate=ERROR_RATE,
        max_p95_ms=MAX_FAILING_P95_MS,
    )
    strict_failures = check_thresholds(report, strict_config)

    assert strict_failures == ["p95 latency 355.0 ms exceeded threshold 100.0 ms"]


def test_format_and_write_report_include_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path, max_error_rate=ERROR_RATE)
    report = build_report(
        config=config,
        samples=_samples(),
        auth_samples=[],
        duration_seconds=1.0,
        setup_duration_seconds=0.1,
    )

    write_report(report, config.output_path)
    summary = format_summary(report, config.output_path)

    assert config.output_path.exists()
    assert "Sibyl multi-user performance" in summary
    assert f"run_id: {RUN_ID}" in summary
    assert "context_pack" in summary


def test_config_from_args_uses_cli_values(tmp_path: Path) -> None:
    output_path = tmp_path / "perf.json"

    config = config_from_args(
        [
            "--users",
            str(CLI_USERS),
            "--iterations",
            str(CLI_ITERATIONS),
            "--warmup-iterations",
            "0",
            "--request-timeout",
            str(CLI_REQUEST_TIMEOUT),
            "--output-path",
            str(output_path),
            "--run-id",
            RUN_ID,
            "--max-error-rate",
            str(CLI_MAX_ERROR_RATE),
            "--max-p95-ms",
            str(CLI_MAX_P95_MS),
        ]
    )

    assert config.users == CLI_USERS
    assert config.iterations == CLI_ITERATIONS
    assert config.warmup_iterations == 0
    assert config.request_timeout == CLI_REQUEST_TIMEOUT
    assert config.output_path == output_path
    assert config.run_id == RUN_ID
    assert config.max_error_rate == CLI_MAX_ERROR_RATE
    assert config.max_p95_ms == CLI_MAX_P95_MS
