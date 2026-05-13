#!/usr/bin/env python3
"""Enforce threshold gates for saved Sibyl evaluation reports."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast

ProfileName = Literal["smoke", "acceptance", "context-pack", "ai-memory"]


@dataclass(frozen=True)
class MetricThreshold:
    minimum: float | None = None
    maximum: float | None = None


PROFILE_THRESHOLDS: dict[ProfileName, dict[str, MetricThreshold]] = {
    "smoke": {
        "success@5": MetricThreshold(minimum=0.20),
        "latency_ms": MetricThreshold(maximum=3000.0),
    },
    "acceptance": {
        "success@5": MetricThreshold(minimum=0.40),
        "ndcg@10": MetricThreshold(minimum=0.30),
        "mrr": MetricThreshold(minimum=0.25),
        "latency_ms": MetricThreshold(maximum=3000.0),
    },
    "context-pack": {
        "pass_rate": MetricThreshold(minimum=1.0),
        "latency_p95_ms": MetricThreshold(maximum=1000.0),
        "source_metadata_coverage": MetricThreshold(minimum=1.0),
        "facet_order_match_rate": MetricThreshold(minimum=1.0),
        "leak_count": MetricThreshold(maximum=0.0),
        "forbidden_term_matches": MetricThreshold(maximum=0.0),
    },
    "ai-memory": {},
}

_AI_MEMORY_SUMMARY_KEYS = ("per_type", "per_slice", "per_category", "per_task")
_AI_MEMORY_CASE_ID_KEYS = ("case_id", "question_id", "task_id")
_AI_MEMORY_ANSWER_KEYS = (
    "answer_ids",
    "answer_session_ids",
    "expected_ids",
    "expected_result_ids",
)
_AI_MEMORY_RANKING_KEYS = ("ranked_ids", "ranked_session_ids", "ranked_result_ids", "result_ids")


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_metrics(report: dict[str, Any]) -> dict[str, float]:
    metrics_section = report.get("metrics")
    if not isinstance(metrics_section, dict):
        metrics_section = report.get("overall")
    if not isinstance(metrics_section, dict):
        msg = "Report does not contain a supported metrics section"
        raise TypeError(msg)

    metrics: dict[str, float] = {}
    for key, value in metrics_section.items():
        if isinstance(value, int | float):
            metrics[key] = float(value)

    elapsed_seconds = report.get("elapsed_seconds")
    if isinstance(elapsed_seconds, int | float):
        metrics["elapsed_seconds"] = float(elapsed_seconds)

    return metrics


def parse_kv_pairs(values: list[str], *, value_kind: Literal["float", "string"]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            msg = f"Invalid KEY=VALUE entry: {item!r}"
            raise ValueError(msg)
        key, raw_value = item.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            msg = f"Invalid KEY=VALUE entry: {item!r}"
            raise ValueError(msg)
        if value_kind == "float":
            parsed[key] = float(raw_value)
        else:
            parsed[key] = raw_value
    return parsed


def build_thresholds(
    *,
    profile: ProfileName,
    minimums: dict[str, float],
    maximums: dict[str, float],
) -> dict[str, MetricThreshold]:
    thresholds = {
        metric: MetricThreshold(minimum=rule.minimum, maximum=rule.maximum)
        for metric, rule in PROFILE_THRESHOLDS[profile].items()
    }
    for metric, value in minimums.items():
        current = thresholds.get(metric, MetricThreshold())
        thresholds[metric] = replace(current, minimum=value)
    for metric, value in maximums.items():
        current = thresholds.get(metric, MetricThreshold())
        thresholds[metric] = replace(current, maximum=value)
    return thresholds


def _is_non_empty_mapping(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict) and bool(value)


def _is_non_empty_sequence(value: Any) -> TypeGuard[list[Any] | tuple[Any, ...]]:
    return isinstance(value, list | tuple) and bool(value)


def _has_any_key(record: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in record and record[key] not in (None, "", [], {}) for key in keys)


def _has_case_metric(record: dict[str, Any]) -> bool:
    metrics = record.get("metrics")
    if _is_non_empty_mapping(metrics):
        return any(isinstance(value, int | float) for value in metrics.values())
    return any(
        isinstance(value, int | float)
        for key, value in record.items()
        if key.startswith(("recall@", "ndcg@", "precision@", "success@", "mrr"))
    )


def _validate_ai_memory_header(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in ("schema_version", "suite", "sibyl_commit"):
        if not isinstance(report.get(field), str) or not report[field].strip():
            failures.append(f"missing non-empty field {field!r}")

    if not isinstance(report.get("generated_at") or report.get("timestamp"), str):
        failures.append("missing timestamp field 'generated_at' or 'timestamp'")

    command = report.get("command")
    if not isinstance(command, str | list) or not command:
        failures.append("missing non-empty field 'command'")

    return failures


def _validate_ai_memory_scope(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not _is_non_empty_mapping(report.get("dataset") or report.get("corpus")):
        failures.append("missing non-empty field 'dataset' or 'corpus'")

    runtime = report.get("runtime")
    if not _is_non_empty_mapping(runtime):
        failures.append("missing non-empty field 'runtime'")
    else:
        for field in ("runtime_mode", "graph_engine", "store"):
            if not isinstance(runtime.get(field), str) or not runtime[field].strip():
                failures.append(f"runtime missing non-empty field {field!r}")

    if not _is_non_empty_mapping(report.get("overall")):
        failures.append("missing non-empty field 'overall'")

    return failures


def _validate_ai_memory_summaries(report: dict[str, Any]) -> list[str]:
    if not any(_is_non_empty_mapping(report.get(key)) for key in _AI_MEMORY_SUMMARY_KEYS):
        keys = "', '".join(_AI_MEMORY_SUMMARY_KEYS)
        return [f"missing per-slice summary field; expected one of '{keys}'"]
    return []


def _validate_ai_memory_cases(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    case_results = report.get("case_results")
    if not _is_non_empty_sequence(case_results):
        failures.append("missing non-empty field 'case_results'")
        return failures

    for index, case in enumerate(case_results):
        if not isinstance(case, dict):
            failures.append(f"case_results[{index}] is not an object")
            continue
        case_record = cast("dict[str, Any]", case)
        if not _has_any_key(case_record, _AI_MEMORY_CASE_ID_KEYS):
            failures.append(f"case_results[{index}] missing case identifier")
        if not _has_any_key(case_record, _AI_MEMORY_ANSWER_KEYS):
            failures.append(f"case_results[{index}] missing answer IDs")
        if not _has_any_key(case_record, _AI_MEMORY_RANKING_KEYS):
            failures.append(f"case_results[{index}] missing ranked result IDs")
        if not _has_case_metric(case_record):
            failures.append(f"case_results[{index}] missing numeric case metrics")

    return failures


def validate_ai_memory_record(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    failures.extend(_validate_ai_memory_header(report))
    failures.extend(_validate_ai_memory_scope(report))
    failures.extend(_validate_ai_memory_summaries(report))
    failures.extend(_validate_ai_memory_cases(report))
    return failures


def evaluate_report(
    report: dict[str, Any],
    *,
    profile: ProfileName,
    minimums: dict[str, float] | None = None,
    maximums: dict[str, float] | None = None,
    required_metadata: dict[str, str] | None = None,
) -> list[str]:
    try:
        metrics = extract_metrics(report)
    except TypeError:
        if profile != "ai-memory":
            raise
        metrics = {}
    thresholds = build_thresholds(
        profile=profile,
        minimums=minimums or {},
        maximums=maximums or {},
    )
    failures: list[str] = []

    metadata = report.get("metadata")
    if required_metadata:
        if not isinstance(metadata, dict):
            failures.append("report metadata is missing or invalid")
        else:
            for key, expected in required_metadata.items():
                actual = metadata.get(key)
                if actual != expected:
                    failures.append(f"metadata[{key!r}] expected {expected!r}, got {actual!r}")

    if profile == "ai-memory":
        failures.extend(validate_ai_memory_record(report))

    for metric, threshold in sorted(thresholds.items()):
        actual = metrics.get(metric)
        if actual is None:
            failures.append(f"missing metric {metric!r}")
            continue
        if threshold.minimum is not None and actual < threshold.minimum:
            failures.append(
                f"metric {metric!r} below minimum {threshold.minimum:.4f}: {actual:.4f}"
            )
        if threshold.maximum is not None and actual > threshold.maximum:
            failures.append(
                f"metric {metric!r} above maximum {threshold.maximum:.4f}: {actual:.4f}"
            )

    return failures


def _report_name(report: dict[str, Any], fallback: str) -> str:
    for key in ("label", "suite", "search_type"):
        value = report.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enforce threshold gates on a saved Sibyl eval report."
    )
    parser.add_argument("report", type=Path, help="Saved evaluation report JSON.")
    parser.add_argument(
        "--profile",
        choices=("smoke", "acceptance", "context-pack", "ai-memory"),
        default="acceptance",
        help="Named threshold profile to enforce.",
    )
    parser.add_argument(
        "--min-metric",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override or add a minimum metric threshold.",
    )
    parser.add_argument(
        "--max-metric",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override or add a maximum metric threshold.",
    )
    parser.add_argument(
        "--require-metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Require report metadata to include exact key/value pairs.",
    )
    args = parser.parse_args(argv)

    try:
        minimums = parse_kv_pairs(args.min_metric, value_kind="float")
        maximums = parse_kv_pairs(args.max_metric, value_kind="float")
        required_metadata = parse_kv_pairs(args.require_metadata, value_kind="string")
    except ValueError as exc:
        parser.error(str(exc))

    report = load_report(args.report)
    failures = evaluate_report(
        report,
        profile=args.profile,
        minimums=minimums,
        maximums=maximums,
        required_metadata=required_metadata,
    )

    report_name = _report_name(report, args.report.stem)
    _echo()
    _echo(f"Checking {report_name} with the {args.profile} profile")
    _echo()
    try:
        metrics = extract_metrics(report)
    except TypeError:
        if args.profile != "ai-memory":
            raise
        metrics = {}
    thresholds = build_thresholds(
        profile=args.profile,
        minimums=minimums,
        maximums=maximums,
    )
    for metric, threshold in sorted(thresholds.items()):
        actual = metrics.get(metric)
        if actual is None:
            _echo(f"  {metric}: missing")
            continue
        checks: list[str] = []
        if threshold.minimum is not None:
            checks.append(f">= {threshold.minimum:.4f}")
        if threshold.maximum is not None:
            checks.append(f"<= {threshold.maximum:.4f}")
        _echo(f"  {metric}: {actual:.4f} ({', '.join(checks)})")

    if failures:
        _echo()
        _echo("Gate failed:")
        for failure in failures:
            _echo(f"  - {failure}")
        return 1

    _echo()
    _echo("Gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
