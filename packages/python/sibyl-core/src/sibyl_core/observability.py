"""Runtime telemetry primitives for Sibyl services."""

from __future__ import annotations

import math
import os
import re
import resource
import threading
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_LABEL_PATTERN = re.compile(r"[^A-Za-z0-9_.:/{}-]+")
_METRIC_PATTERN = re.compile(r"[^A-Za-z0-9_:]+")
_DEFAULT_WINDOW_SECONDS = 15 * 60
_MAX_LABEL_LENGTH = 120
_MAX_EVENTS = 20_000
_MAX_SAMPLES = 2_000
_NON_ERROR_STATUSES = {
    "broadcast",
    "connections",
    "created",
    "duplicate",
    "ended",
    "ok",
    "partial",
    "slow",
    "sync",
    "valid",
}


@dataclass(slots=True)
class HistogramState:
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES))

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.samples.append(value)

    def snapshot(self) -> dict[str, float | int | None]:
        values = list(self.samples)
        return {
            "count": self.count,
            "sum": round(self.total, 4),
            "min": self.minimum,
            "max": self.maximum,
            "avg": round(self.total / self.count, 4) if self.count else 0.0,
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
            "p99": _percentile(values, 0.99),
        }


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    timestamp: float
    category: str
    status: str
    duration_ms: float | None = None
    value: float = 1.0
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def minute(self) -> int:
        return int(self.timestamp // 60) * 60

    def snapshot(self) -> dict[str, Any]:
        return {
            "timestamp": datetime.fromtimestamp(self.timestamp, UTC).isoformat(),
            "category": self.category,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "value": round(self.value, 4),
            "labels": self.labels,
        }


class TelemetryRegistry:
    """Thread-safe in-process telemetry registry with bounded recent events."""

    def __init__(
        self,
        *,
        max_events: int = _MAX_EVENTS,
        max_samples: int = _MAX_SAMPLES,
        started_at: float | None = None,
    ) -> None:
        self._started_at = started_at or time.time()
        self._max_samples = max_samples
        self._lock = threading.RLock()
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(
            float
        )
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], HistogramState] = {}
        self._events: deque[TelemetryEvent] = deque(maxlen=max_events)

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self._started_at)

    def increment(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        key = (_metric_name(name), _label_tuple(labels))
        with self._lock:
            self._counters[key] += value

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        key = (_metric_name(name), _label_tuple(labels))
        with self._lock:
            self._gauges[key] = float(value)

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        key = (_metric_name(name), _label_tuple(labels))
        with self._lock:
            state = self._histograms.get(key)
            if state is None:
                state = HistogramState(samples=deque(maxlen=self._max_samples))
                self._histograms[key] = state
            state.observe(float(value))

    def record_event(
        self,
        category: str,
        *,
        status: str = "ok",
        duration_ms: float | None = None,
        value: float = 1.0,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        event = TelemetryEvent(
            timestamp=time.time(),
            category=safe_label(category),
            status=safe_label(status),
            duration_ms=round(float(duration_ms), 4) if duration_ms is not None else None,
            value=float(value),
            labels={key: value for key, value in _label_tuple(labels)},
        )
        with self._lock:
            self._events.append(event)

    @contextmanager
    def timed(
        self,
        category: str,
        metric_name: str,
        labels: Mapping[str, object] | None = None,
    ):
        started_at = time.perf_counter()
        status = "ok"
        try:
            yield
        except Exception:
            status = "error"
            raise
        finally:
            duration_ms = (time.perf_counter() - started_at) * 1000
            merged_labels = {**dict(labels or {}), "status": status}
            self.observe(metric_name, duration_ms, merged_labels)
            self.record_event(
                category,
                status=status,
                duration_ms=duration_ms,
                labels=merged_labels,
            )

    def record_api_request(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        status_family = f"{int(status_code / 100)}xx" if status_code else "unknown"
        status = "error" if status_code >= 400 else "ok"
        labels = {
            "method": method.upper(),
            "route": route,
            "status": str(status_code),
            "status_family": status_family,
        }
        self.increment("sibyl_api_requests_total", labels=labels)
        self.observe("sibyl_api_request_duration_ms", duration_ms, labels=labels)
        self.record_event("api", status=status, duration_ms=duration_ms, labels=labels)

    def record_surreal_query(
        self,
        *,
        client: str,
        database: str,
        statement: str,
        query_hash: str,
        elapsed_ms: float,
        retry_count: int = 0,
        status: str = "ok",
        slow: bool = False,
    ) -> None:
        event_status = "slow" if slow and status == "ok" else status
        labels = {
            "client": client,
            "database": database,
            "statement": statement,
            "status": event_status,
        }
        self.increment("sibyl_surreal_queries_total", labels=labels)
        self.observe("sibyl_surreal_query_duration_ms", elapsed_ms, labels=labels)
        if retry_count:
            self.increment("sibyl_surreal_query_retries_total", retry_count, labels=labels)
        self.record_event(
            "surreal",
            status=event_status,
            duration_ms=elapsed_ms,
            labels={**labels, "query_hash": query_hash},
        )

    def record_memory_operation(
        self,
        *,
        operation: str,
        status: str,
        duration_ms: float,
        result_count: int | None = None,
    ) -> None:
        labels = {"operation": operation, "status": status}
        self.increment("sibyl_memory_operations_total", labels=labels)
        self.observe("sibyl_memory_operation_duration_ms", duration_ms, labels=labels)
        if result_count is not None:
            self.observe("sibyl_memory_operation_results", result_count, labels=labels)
        self.record_event("memory", status=status, duration_ms=duration_ms, labels=labels)

    def record_search_operation(
        self,
        *,
        surface: str,
        status: str,
        duration_ms: float,
        result_count: int | None = None,
    ) -> None:
        labels = {"surface": surface, "status": status}
        self.increment("sibyl_search_operations_total", labels=labels)
        self.observe("sibyl_search_operation_duration_ms", duration_ms, labels=labels)
        if result_count is not None:
            self.observe("sibyl_search_operation_results", result_count, labels=labels)
        self.record_event("search", status=status, duration_ms=duration_ms, labels=labels)

    def record_job_enqueued(self, *, function: str, created: bool) -> None:
        labels = {"function": function, "status": "created" if created else "duplicate"}
        self.increment("sibyl_jobs_enqueued_total", labels=labels)
        self.record_event("jobs", status=labels["status"], labels=labels)

    def record_job_finished(self, *, function: str, status: str, duration_ms: float) -> None:
        labels = {"function": function, "status": status}
        self.increment("sibyl_jobs_finished_total", labels=labels)
        self.observe("sibyl_job_duration_ms", duration_ms, labels=labels)
        self.record_event("jobs", status=status, duration_ms=duration_ms, labels=labels)

    def record_crawler_run(
        self,
        *,
        status: str,
        duration_ms: float,
        documents: int = 0,
        chunks: int = 0,
        errors: int = 0,
    ) -> None:
        labels = {"status": status}
        self.increment("sibyl_crawler_runs_total", labels=labels)
        self.observe("sibyl_crawler_run_duration_ms", duration_ms, labels=labels)
        self.increment("sibyl_crawler_documents_total", documents, labels=labels)
        self.increment("sibyl_crawler_chunks_total", chunks, labels=labels)
        self.increment("sibyl_crawler_errors_total", errors, labels=labels)
        self.record_event("crawler", status=status, duration_ms=duration_ms, labels=labels)

    def record_llm_call(
        self,
        *,
        surface: str,
        provider: str,
        model: str,
        status: str,
        duration_ms: float,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        labels = {
            "surface": surface,
            "provider": provider,
            "model": model,
            "status": status,
        }
        self.increment("sibyl_llm_calls_total", labels=labels)
        self.observe("sibyl_llm_call_duration_ms", duration_ms, labels=labels)
        if input_tokens is not None:
            self.increment("sibyl_llm_input_tokens_total", input_tokens, labels=labels)
        if output_tokens is not None:
            self.increment("sibyl_llm_output_tokens_total", output_tokens, labels=labels)
        self.record_event("llm", status=status, duration_ms=duration_ms, labels=labels)

    def record_queue_health(
        self,
        *,
        backend: str,
        queue_depth: int,
        queue_healthy: bool,
        worker_healthy: bool,
    ) -> None:
        labels = {"backend": backend}
        self.set_gauge("sibyl_queue_depth", queue_depth, labels=labels)
        self.set_gauge("sibyl_queue_healthy", 1 if queue_healthy else 0, labels=labels)
        self.set_gauge("sibyl_worker_healthy", 1 if worker_healthy else 0, labels=labels)
        self.record_event(
            "queue",
            status="ok" if queue_healthy and worker_healthy else "degraded",
            value=queue_depth,
            labels=labels,
        )

    def record_websocket_connections(self, *, active: int) -> None:
        self.set_gauge("sibyl_websocket_active_connections", active)
        self.record_event("websocket", status="connections", value=active)

    def record_websocket_broadcast(self, *, event: str, recipients: int) -> None:
        labels = {"event": event}
        self.increment("sibyl_websocket_broadcasts_total", labels=labels)
        self.observe("sibyl_websocket_broadcast_recipients", recipients, labels=labels)
        self.record_event("websocket", status="broadcast", value=recipients, labels=labels)

    def record_process_snapshot(self) -> None:
        snapshot = process_snapshot(self.uptime_seconds)
        for name, value in snapshot.items():
            self.set_gauge(f"sibyl_process_{name}", value)

    def snapshot(self, *, window_seconds: int = _DEFAULT_WINDOW_SECONDS) -> dict[str, Any]:
        now = time.time()
        self.record_process_snapshot()
        with self._lock:
            counters = [
                _metric_value_dict("counter", name, labels, value)
                for (name, labels), value in sorted(self._counters.items())
            ]
            gauges = [
                _metric_value_dict("gauge", name, labels, value)
                for (name, labels), value in sorted(self._gauges.items())
            ]
            histograms = [
                _histogram_value_dict(name, labels, state.snapshot())
                for (name, labels), state in sorted(self._histograms.items())
            ]
            events = [event for event in self._events if now - event.timestamp <= window_seconds]

        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "window_seconds": window_seconds,
            "uptime_seconds": round(self.uptime_seconds, 3),
            "summaries": _summaries(events),
            "trends": _trends(events),
            "recent_events": [event.snapshot() for event in events[-200:]],
            "metrics": [*counters, *gauges, *histograms],
        }

    def prometheus_text(self) -> str:
        self.record_process_snapshot()
        lines = [
            "# HELP sibyl_build_info Sibyl telemetry registry is active",
            "# TYPE sibyl_build_info gauge",
            "sibyl_build_info 1",
        ]
        emitted_types = {"sibyl_build_info"}
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                if name not in emitted_types:
                    lines.append(f"# TYPE {name} counter")
                    emitted_types.add(name)
                lines.append(_prometheus_line(name, labels, value))
            for (name, labels), value in sorted(self._gauges.items()):
                if name not in emitted_types:
                    lines.append(f"# TYPE {name} gauge")
                    emitted_types.add(name)
                lines.append(_prometheus_line(name, labels, value))
            for (name, labels), state in sorted(self._histograms.items()):
                snapshot = state.snapshot()
                if name not in emitted_types:
                    lines.append(f"# TYPE {name} summary")
                    emitted_types.add(name)
                lines.append(_prometheus_line(f"{name}_count", labels, snapshot["count"]))
                lines.append(_prometheus_line(f"{name}_sum", labels, snapshot["sum"]))
                for quantile in ("0.5", "0.95", "0.99"):
                    value = snapshot[{"0.5": "p50", "0.95": "p95", "0.99": "p99"}[quantile]]
                    q_labels = (*labels, ("quantile", quantile))
                    lines.append(_prometheus_line(name, q_labels, value or 0.0))
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._events.clear()
            self._started_at = time.time()


_registry = TelemetryRegistry()


def telemetry_registry() -> TelemetryRegistry:
    return _registry


def safe_label(value: object) -> str:
    if value is None:
        return "unknown"
    text = str(value).strip() or "unknown"
    text = _LABEL_PATTERN.sub("_", text)
    return text[:_MAX_LABEL_LENGTH]


def elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 4)


def process_snapshot(uptime_seconds: float) -> dict[str, float]:
    snapshot: dict[str, float] = {
        "uptime_seconds": round(uptime_seconds, 3),
        "threads": float(threading.active_count()),
    }
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        snapshot["max_rss_bytes"] = float(_maxrss_bytes(usage.ru_maxrss))
    except Exception:
        pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        with open("/proc/self/statm", encoding="utf-8") as statm:
            parts = statm.read().split()
        if len(parts) >= 2:
            snapshot["rss_bytes"] = float(int(parts[1]) * page_size)
    except Exception:
        pass
    return snapshot


def _metric_name(name: str) -> str:
    return _METRIC_PATTERN.sub("_", name).strip("_") or "sibyl_metric"


def _label_tuple(labels: Mapping[str, object] | None) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (_metric_name(str(key)), safe_label(value))
            for key, value in (labels or {}).items()
            if value is not None
        )
    )


def _metric_value_dict(
    kind: str,
    name: str,
    labels: tuple[tuple[str, str], ...],
    value: float,
) -> dict[str, Any]:
    return {"kind": kind, "name": name, "labels": dict(labels), "value": round(value, 4)}


def _histogram_value_dict(
    name: str,
    labels: tuple[tuple[str, str], ...],
    snapshot: dict[str, float | int | None],
) -> dict[str, Any]:
    return {
        "kind": "histogram",
        "name": name,
        "labels": dict(labels),
        **snapshot,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[int(index)], 4)
    weight = index - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)


def _duration_summary(events: list[TelemetryEvent]) -> dict[str, float | int]:
    durations = [event.duration_ms for event in events if event.duration_ms is not None]
    total = len(events)
    errors = sum(1 for event in events if event.status not in _NON_ERROR_STATUSES)
    slow = sum(1 for event in events if event.status == "slow")
    return {
        "count": total,
        "errors": errors,
        "slow": slow,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "avg_ms": round(sum(durations) / len(durations), 4) if durations else 0.0,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "p99_ms": _percentile(durations, 0.99),
        "max_ms": round(max(durations), 4) if durations else 0.0,
    }


def _summaries(events: list[TelemetryEvent]) -> dict[str, dict[str, float | int]]:
    by_category: defaultdict[str, list[TelemetryEvent]] = defaultdict(list)
    for event in events:
        by_category[event.category].append(event)
    categories = (
        "api",
        "surreal",
        "memory",
        "search",
        "jobs",
        "crawler",
        "llm",
        "queue",
        "websocket",
    )
    return {category: _duration_summary(by_category.get(category, [])) for category in categories}


def _trends(events: list[TelemetryEvent]) -> list[dict[str, Any]]:
    buckets: defaultdict[int, list[TelemetryEvent]] = defaultdict(list)
    for event in events:
        buckets[event.minute].append(event)
    points: list[dict[str, Any]] = []
    for minute in sorted(buckets):
        bucket_events = buckets[minute]
        api = _duration_summary([event for event in bucket_events if event.category == "api"])
        surreal = _duration_summary(
            [event for event in bucket_events if event.category == "surreal"]
        )
        memory = _duration_summary([event for event in bucket_events if event.category == "memory"])
        llm = _duration_summary([event for event in bucket_events if event.category == "llm"])
        points.append(
            {
                "timestamp": datetime.fromtimestamp(minute, UTC).isoformat(),
                "api_p95_ms": api["p95_ms"],
                "surreal_p95_ms": surreal["p95_ms"],
                "memory_p95_ms": memory["p95_ms"],
                "llm_p95_ms": llm["p95_ms"],
                "error_rate": round(
                    (api["errors"] + surreal["errors"] + memory["errors"] + llm["errors"])
                    / max(1, api["count"] + surreal["count"] + memory["count"] + llm["count"]),
                    4,
                ),
                "request_count": api["count"],
                "query_count": surreal["count"],
                "memory_count": memory["count"],
                "llm_count": llm["count"],
            }
        )
    return points[-120:]


def _prometheus_line(
    name: str,
    labels: tuple[tuple[str, str], ...],
    value: object,
) -> str:
    label_text = ""
    if labels:
        label_text = (
            "{" + ",".join(f'{key}="{_escape_label(value)}"' for key, value in labels) + "}"
        )
    return f"{name}{label_text} {_float_value(value):.6f}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _float_value(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _maxrss_bytes(value: int) -> int:
    if os.uname().sysname == "Darwin":
        return value
    return value * 1024


__all__ = [
    "TelemetryRegistry",
    "elapsed_ms",
    "safe_label",
    "telemetry_registry",
]
