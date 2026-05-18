"""Multi-user load harness for live Sibyl API performance checks."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

JsonObject = dict[str, Any]

DEFAULT_API_BASE_URL = "http://localhost:3334/api"
DEFAULT_EMAIL_DOMAIN = "perf.sibyl.dev"
DEFAULT_ERROR_RATE = 0.0
DEFAULT_ITERATIONS = 3
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_USERS = 4
DEFAULT_WARMUP_ITERATIONS = 1
HTTP_CONFLICT = 409
HTTP_OK_MAX = 399
HTTP_OK_MIN = 200
MILLISECONDS_PER_SECOND = 1000.0
PERCENTILE_50 = 0.50
PERCENTILE_95 = 0.95
PERCENTILE_99 = 0.99
SUMMARY_PREVIEW_OPERATIONS = 8

OPERATION_NAMES = (
    "remember_raw",
    "entity_create",
    "raw_recall",
    "search",
    "context_pack",
)


@dataclass(frozen=True, slots=True)
class PerfConfig:
    api_base_url: str
    users: int
    iterations: int
    warmup_iterations: int
    request_timeout: float
    output_path: Path
    run_id: str
    email_domain: str
    max_error_rate: float
    max_p95_ms: float | None


@dataclass(frozen=True, slots=True)
class PerfUser:
    index: int
    email: str
    access_token: str
    user_id: str | None
    organization_id: str | None


@dataclass(frozen=True, slots=True)
class RequestSample:
    operation: str
    user_index: int
    iteration: int
    latency_ms: float
    ok: bool
    status_code: int | None = None
    error: str | None = None


def generate_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def default_output_path(run_id: str) -> Path:
    return Path(".moon/cache/perf-results") / f"multi_user_{run_id}.json"


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]

    lower = ordered[lower_index]
    upper = ordered[upper_index]
    return lower + (upper - lower) * (position - lower_index)


def summarize_samples(samples: Sequence[RequestSample], duration_seconds: float) -> JsonObject:
    latencies = [sample.latency_ms for sample in samples]
    failed = [sample for sample in samples if not sample.ok]
    status_counts: dict[str, int] = {}
    for sample in samples:
        key = "exception" if sample.status_code is None else str(sample.status_code)
        status_counts[key] = status_counts.get(key, 0) + 1

    total = len(samples)
    error_rate = len(failed) / total if total else 0.0
    throughput = total / duration_seconds if duration_seconds > 0 else 0.0
    return {
        "total_requests": total,
        "successful_requests": total - len(failed),
        "failed_requests": len(failed),
        "error_rate": error_rate,
        "throughput_rps": throughput,
        "status_counts": status_counts,
        "latency_ms": {
            "min": min(latencies) if latencies else None,
            "p50": percentile(latencies, PERCENTILE_50),
            "p95": percentile(latencies, PERCENTILE_95),
            "p99": percentile(latencies, PERCENTILE_99),
            "max": max(latencies) if latencies else None,
            "avg": sum(latencies) / len(latencies) if latencies else None,
        },
    }


def _group_samples(
    samples: Sequence[RequestSample],
    duration_seconds: float,
    key_name: str,
) -> dict[str, JsonObject]:
    grouped: dict[str, list[RequestSample]] = {}
    for sample in samples:
        key = str(getattr(sample, key_name))
        grouped.setdefault(key, []).append(sample)
    return {
        key: summarize_samples(group, duration_seconds)
        for key, group in sorted(grouped.items(), key=lambda item: item[0])
    }


def build_report(
    *,
    config: PerfConfig,
    samples: Sequence[RequestSample],
    auth_samples: Sequence[RequestSample],
    duration_seconds: float,
    setup_duration_seconds: float,
) -> JsonObject:
    return {
        "schema_version": "sibyl-multi-user-perf-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": config.run_id,
        "api_base_url": config.api_base_url,
        "config": {
            "users": config.users,
            "iterations": config.iterations,
            "warmup_iterations": config.warmup_iterations,
            "request_timeout": config.request_timeout,
            "max_error_rate": config.max_error_rate,
            "max_p95_ms": config.max_p95_ms,
        },
        "duration_seconds": duration_seconds,
        "setup_duration_seconds": setup_duration_seconds,
        "summary": summarize_samples(samples, duration_seconds),
        "operations": _group_samples(samples, duration_seconds, "operation"),
        "users": _group_samples(samples, duration_seconds, "user_index"),
        "auth": summarize_samples(auth_samples, setup_duration_seconds),
        "samples": [asdict(sample) for sample in samples],
    }


def check_thresholds(report: JsonObject, config: PerfConfig) -> list[str]:
    failures: list[str] = []
    summary = report["summary"]
    latency = summary["latency_ms"]
    error_rate = float(summary["error_rate"])
    p95_ms = latency["p95"]

    if error_rate > config.max_error_rate:
        failures.append(
            f"error rate {error_rate:.3f} exceeded threshold {config.max_error_rate:.3f}"
        )
    if config.max_p95_ms is not None and p95_ms is not None and p95_ms > config.max_p95_ms:
        failures.append(
            f"p95 latency {p95_ms:.1f} ms exceeded threshold {config.max_p95_ms:.1f} ms"
        )
    return failures


def format_summary(report: JsonObject, output_path: Path) -> str:
    summary = report["summary"]
    latency = summary["latency_ms"]
    lines = [
        "Sibyl multi-user performance",
        f"  run_id: {report['run_id']}",
        f"  output: {output_path}",
        f"  requests: {summary['total_requests']}",
        f"  error_rate: {summary['error_rate']:.3f}",
        f"  throughput_rps: {summary['throughput_rps']:.2f}",
        f"  p50_ms: {_format_latency(latency['p50'])}",
        f"  p95_ms: {_format_latency(latency['p95'])}",
        f"  p99_ms: {_format_latency(latency['p99'])}",
        "  operations:",
    ]
    for name, operation in list(report["operations"].items())[:SUMMARY_PREVIEW_OPERATIONS]:
        operation_latency = operation["latency_ms"]
        lines.append(
            "    "
            f"{name}: count={operation['total_requests']} "
            f"errors={operation['failed_requests']} "
            f"p95_ms={_format_latency(operation_latency['p95'])}"
        )
    return "\n".join(lines) + "\n"


async def run_multi_user_performance(config: PerfConfig) -> JsonObject:
    setup_started = time.perf_counter()
    users, auth_samples = await _create_users(config=config)
    setup_duration_seconds = time.perf_counter() - setup_started

    async with AsyncExitStack() as stack:
        clients = []
        for user in users:
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=config.api_base_url,
                    headers={"Authorization": f"Bearer {user.access_token}"},
                    timeout=config.request_timeout,
                )
            )
            clients.append((user, client))

        if config.warmup_iterations > 0:
            await asyncio.gather(
                *(
                    _run_user_operations(
                        client=client,
                        user=user,
                        config=config,
                        iterations=config.warmup_iterations,
                    )
                    for user, client in clients
                )
            )

        started = time.perf_counter()
        user_samples = await asyncio.gather(
            *(
                _run_user_operations(
                    client=client,
                    user=user,
                    config=config,
                    iterations=config.iterations,
                )
                for user, client in clients
            )
        )
        duration_seconds = time.perf_counter() - started

    samples = [sample for group in user_samples for sample in group]
    return build_report(
        config=config,
        samples=samples,
        auth_samples=auth_samples,
        duration_seconds=duration_seconds,
        setup_duration_seconds=setup_duration_seconds,
    )


def write_report(report: JsonObject, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def config_from_args(argv: Sequence[str] | None = None) -> PerfConfig:
    run_id = os.getenv("SIBYL_PERF_RUN_ID") or generate_run_id()
    parser = argparse.ArgumentParser(description="Run a live multi-user Sibyl performance test.")
    parser.add_argument("--api-base-url", default=os.getenv("SIBYL_API_URL", DEFAULT_API_BASE_URL))
    parser.add_argument("--users", type=int, default=_env_int("SIBYL_PERF_USERS", DEFAULT_USERS))
    parser.add_argument(
        "--iterations",
        type=int,
        default=_env_int("SIBYL_PERF_ITERATIONS", DEFAULT_ITERATIONS),
    )
    parser.add_argument(
        "--warmup-iterations",
        type=int,
        default=_env_int("SIBYL_PERF_WARMUP_ITERATIONS", DEFAULT_WARMUP_ITERATIONS),
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=_env_float("SIBYL_PERF_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT),
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=Path(os.getenv("SIBYL_PERF_OUTPUT_PATH", str(default_output_path(run_id)))),
    )
    parser.add_argument("--run-id", default=run_id)
    parser.add_argument(
        "--email-domain",
        default=os.getenv("SIBYL_PERF_EMAIL_DOMAIN", DEFAULT_EMAIL_DOMAIN),
    )
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=_env_float("SIBYL_PERF_MAX_ERROR_RATE", DEFAULT_ERROR_RATE),
    )
    parser.add_argument(
        "--max-p95-ms",
        type=float,
        default=_env_optional_float("SIBYL_PERF_MAX_P95_MS"),
    )
    args = parser.parse_args(argv)
    return PerfConfig(
        api_base_url=args.api_base_url,
        users=_positive_int(args.users, "users"),
        iterations=_positive_int(args.iterations, "iterations"),
        warmup_iterations=_non_negative_int(args.warmup_iterations, "warmup_iterations"),
        request_timeout=_positive_float(args.request_timeout, "request_timeout"),
        output_path=args.output_path,
        run_id=args.run_id,
        email_domain=args.email_domain,
        max_error_rate=_non_negative_float(args.max_error_rate, "max_error_rate"),
        max_p95_ms=(
            None if args.max_p95_ms is None else _positive_float(args.max_p95_ms, "max_p95_ms")
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = config_from_args(argv)
    report = asyncio.run(run_multi_user_performance(config))
    write_report(report, config.output_path)
    sys.stdout.write(format_summary(report, config.output_path))
    failures = check_thresholds(report, config)
    if failures:
        sys.stderr.write("\n".join(failures) + "\n")
        return 1
    return 0


async def _create_users(
    *,
    config: PerfConfig,
) -> tuple[list[PerfUser], list[RequestSample]]:
    async with httpx.AsyncClient(
        base_url=config.api_base_url,
        timeout=config.request_timeout,
    ) as client:
        results = await asyncio.gather(
            *(
                _create_user(client=client, config=config, index=index)
                for index in range(config.users)
            )
        )
    users = [user for user, _ in results]
    samples = [sample for _, sample in results]
    return users, samples


async def _create_user(
    *,
    client: Any,
    config: PerfConfig,
    index: int,
) -> tuple[PerfUser, RequestSample]:
    email = f"sibyl-perf-{config.run_id}-{index}@{config.email_domain}"
    credential = f"SibylPerf-{config.run_id}-{index}-local"
    payload = {"email": email, "password": credential, "name": f"Sibyl Perf User {index}"}
    sample, data = await _timed_json_request(
        client=client,
        operation="signup",
        user_index=index,
        iteration=0,
        method="POST",
        path="/auth/local/signup",
        payload=payload,
    )
    if sample.status_code == HTTP_CONFLICT:
        sample, data = await _timed_json_request(
            client=client,
            operation="login",
            user_index=index,
            iteration=0,
            method="POST",
            path="/auth/local/login",
            payload={"email": email, "password": credential},
        )
    if not sample.ok:
        message = sample.error or "authentication failed"
        raise RuntimeError(f"failed to create perf user {index}: {message}")

    return (
        PerfUser(
            index=index,
            email=email,
            access_token=str(data["access_token"]),
            user_id=_nested_str(data, "user", "id"),
            organization_id=_nested_str(data, "organization", "id"),
        ),
        sample,
    )


async def _run_user_operations(
    *,
    client: Any,
    user: PerfUser,
    config: PerfConfig,
    iterations: int,
) -> list[RequestSample]:
    samples: list[RequestSample] = []
    for iteration in range(iterations):
        for operation in _rotated_operations(user.index + iteration):
            samples.append(
                await _run_operation(
                    client=client,
                    user=user,
                    config=config,
                    operation=operation,
                    iteration=iteration,
                )
            )
    return samples


async def _run_operation(
    *,
    client: Any,
    user: PerfUser,
    config: PerfConfig,
    operation: str,
    iteration: int,
) -> RequestSample:
    if operation == "remember_raw":
        return await _request_remember_raw(client, user, config, iteration)
    if operation == "entity_create":
        return await _request_entity_create(client, user, config, iteration)
    if operation == "raw_recall":
        return await _request_raw_recall(client, user, config, iteration)
    if operation == "search":
        return await _request_search(client, user, config, iteration)
    if operation == "context_pack":
        return await _request_context_pack(client, user, config, iteration)
    raise ValueError(f"unknown operation: {operation}")


async def _request_remember_raw(
    client: Any,
    user: PerfUser,
    config: PerfConfig,
    iteration: int,
) -> RequestSample:
    source_id = f"perf:{config.run_id}:user:{user.index}:raw:{iteration}"
    payload = {
        "title": f"Perf raw memory {config.run_id} user {user.index} iteration {iteration}",
        "raw_content": (
            f"Multi-user performance memory for run {config.run_id}, "
            f"user {user.index}, iteration {iteration}."
        ),
        "source_id": source_id,
        "memory_scope": "private",
        "tags": ["perf", f"perf-run-{config.run_id}", f"perf-user-{user.index}"],
        "metadata": {"run_id": config.run_id, "iteration": iteration},
        "capture_surface": "multi_user_perf",
    }
    sample, _ = await _timed_json_request(
        client=client,
        operation="remember_raw",
        user_index=user.index,
        iteration=iteration,
        method="POST",
        path="/memory/raw",
        payload=payload,
    )
    return sample


async def _request_entity_create(
    client: Any,
    user: PerfUser,
    config: PerfConfig,
    iteration: int,
) -> RequestSample:
    payload = {
        "name": f"Perf graph episode {config.run_id} user {user.index} iteration {iteration}",
        "description": "Synthetic graph write for multi-user performance testing.",
        "content": (
            f"Graph episode created during multi-user performance run {config.run_id} "
            f"for user {user.index} iteration {iteration}."
        ),
        "entity_type": "episode",
        "skip_conflicts": True,
        "tags": ["perf", f"perf-run-{config.run_id}", f"perf-user-{user.index}"],
        "metadata": {"run_id": config.run_id, "iteration": iteration},
    }
    sample, _ = await _timed_json_request(
        client=client,
        operation="entity_create",
        user_index=user.index,
        iteration=iteration,
        method="POST",
        path="/entities",
        payload=payload,
    )
    return sample


async def _request_raw_recall(
    client: Any,
    user: PerfUser,
    config: PerfConfig,
    iteration: int,
) -> RequestSample:
    payload = {
        "query": f"perf-run-{config.run_id} perf-user-{user.index}",
        "memory_scope": "private",
        "limit": 5,
    }
    sample, _ = await _timed_json_request(
        client=client,
        operation="raw_recall",
        user_index=user.index,
        iteration=iteration,
        method="POST",
        path="/memory/raw/recall",
        payload=payload,
    )
    return sample


async def _request_search(
    client: Any,
    user: PerfUser,
    config: PerfConfig,
    iteration: int,
) -> RequestSample:
    payload = {
        "query": f"perf-run-{config.run_id} perf-user-{user.index}",
        "limit": 5,
        "include_content": False,
        "include_documents": False,
        "include_graph": True,
    }
    sample, _ = await _timed_json_request(
        client=client,
        operation="search",
        user_index=user.index,
        iteration=iteration,
        method="POST",
        path="/search",
        payload=payload,
    )
    return sample


async def _request_context_pack(
    client: Any,
    user: PerfUser,
    config: PerfConfig,
    iteration: int,
) -> RequestSample:
    payload = {
        "goal": f"Analyze perf run {config.run_id} user {user.index} iteration {iteration}",
        "intent": "build",
        "layer": "recall",
        "limit": 8,
        "include_related": False,
        "related_limit": 0,
        "agent_id": f"perf-user-{user.index}",
    }
    sample, _ = await _timed_json_request(
        client=client,
        operation="context_pack",
        user_index=user.index,
        iteration=iteration,
        method="POST",
        path="/context/pack",
        payload=payload,
    )
    return sample


async def _timed_json_request(
    *,
    client: Any,
    operation: str,
    user_index: int,
    iteration: int,
    method: str,
    path: str,
    payload: JsonObject,
) -> tuple[RequestSample, JsonObject]:
    started = time.perf_counter()
    try:
        response = await client.request(method, path, json=payload)
        latency_ms = (time.perf_counter() - started) * MILLISECONDS_PER_SECOND
        ok = HTTP_OK_MIN <= response.status_code <= HTTP_OK_MAX
        data = response.json() if ok else {}
        error = None if ok else _truncate(response.text)
        return (
            RequestSample(
                operation=operation,
                user_index=user_index,
                iteration=iteration,
                latency_ms=latency_ms,
                ok=ok,
                status_code=response.status_code,
                error=error,
            ),
            data,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * MILLISECONDS_PER_SECOND
        return (
            RequestSample(
                operation=operation,
                user_index=user_index,
                iteration=iteration,
                latency_ms=latency_ms,
                ok=False,
                error=f"{exc.__class__.__name__}: {_truncate(str(exc))}",
            ),
            {},
        )


def _rotated_operations(offset: int) -> tuple[str, ...]:
    normalized = offset % len(OPERATION_NAMES)
    return (*OPERATION_NAMES[normalized:], *OPERATION_NAMES[:normalized])


def _nested_str(data: JsonObject, first: str, second: str) -> str | None:
    value = data.get(first)
    if not isinstance(value, dict):
        return None
    nested = value.get(second)
    return str(nested) if nested is not None else None


def _format_latency(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f}"


def _truncate(value: str, limit: int = 500) -> str:
    return value if len(value) <= limit else value[:limit]


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or value == "" else float(value)


def _env_optional_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or value in {"", "0"}:
        return None
    return float(value)


def _positive_int(value: int, name: str) -> int:
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _non_negative_int(value: int, name: str) -> int:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _positive_float(value: float, name: str) -> float:
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


def _non_negative_float(value: float, name: str) -> float:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
