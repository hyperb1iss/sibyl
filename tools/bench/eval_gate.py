#!/usr/bin/env python3
"""Enforce threshold gates for saved Sibyl evaluation reports."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast

ProfileName = Literal["smoke", "acceptance", "context-pack", "ai-memory"]
MetricDirection = Literal["higher", "lower"]


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
    "ai-memory": {
        "recall@5": MetricThreshold(minimum=0.75),
        "ndcg@5": MetricThreshold(minimum=0.70),
        "recall@10": MetricThreshold(minimum=0.80),
        "ndcg@10": MetricThreshold(minimum=0.70),
    },
}

AI_MEMORY_PER_SLICE_THRESHOLDS: dict[str, MetricThreshold] = {
    "recall@5": MetricThreshold(minimum=0.70),
    "ndcg@5": MetricThreshold(minimum=0.55),
    "recall@10": MetricThreshold(minimum=0.80),
    "ndcg@10": MetricThreshold(minimum=0.60),
}
AI_MEMORY_PER_SLICE_MIN_CASES = 10

_AI_MEMORY_SUMMARY_KEYS = ("per_type", "per_slice", "per_category", "per_task")
_AI_MEMORY_CASE_ID_KEYS = ("case_id", "question_id", "task_id")
_AI_MEMORY_ANSWER_KEYS = (
    "answer_ids",
    "answer_session_ids",
    "expected_ids",
    "expected_result_ids",
)
_AI_MEMORY_RANKING_KEYS = ("ranked_ids", "ranked_session_ids", "ranked_result_ids", "result_ids")
_CONTEXT_PACK_RETRIEVAL_MODES = frozenset(("pre-graphiti", "post-graphiti", "native", "compare"))
_AI_MEMORY_RETRIEVAL_MODES = frozenset(
    ("pre-graphiti", "post-graphiti", "native", "compare", "raw", "hybrid")
)
_RELEASE_METADATA_FIELDS = (
    "retrieval_mode",
    "embedding_provider",
    "embedding_model",
    "embedding_dimensions",
    "tokenizer_estimate_method",
    "dataset_name",
    "corpus_hash",
    "repeat_count",
    "auth_manifest_id",
    "sibyl_commit",
    "runtime_mode",
)
_AI_MEMORY_RUNTIME_FIELDS = (
    "runtime_mode",
    "retrieval_mode",
    "embedding_provider",
    "embedding_model",
    "embedding_dimensions",
    "tokenizer_estimate_method",
)
_AI_MEMORY_DATASET_FIELDS = ("name", "corpus_hash")
_AI_MEMORY_EXTERNAL_ARTIFACT_FIELDS = (
    "provider",
    "repo",
    "run_id",
    "run_url",
    "job_name",
    "artifact_name",
    "artifact_path",
    "sha256",
    "size_bytes",
    "archive_size_bytes",
    "expires_at",
    "verified_at",
    "verification_command",
    "verification_receipt",
    "gate_profile",
    "gate_command",
    "gate_passed",
    "gate_receipt",
)
SHA256_HEX_LENGTH = 64
SHA256_HEX_CHARACTERS = frozenset("0123456789abcdef")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AI_MEMORY_MANIFEST = REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "manifest.json"
LOWER_IS_BETTER_METRICS = frozenset(
    (
        "latency_ms",
        "latency_p95_ms",
        "elapsed_seconds",
        "error_rate",
        "avg_estimated_tokens",
        "max_estimated_tokens",
        "avg_markdown_chars",
        "max_markdown_chars",
        "leak_count",
        "forbidden_term_matches",
        "cross_question_result_count",
        "timeout_count",
        "skipped_case_count",
    )
)
LOWER_IS_BETTER_SUFFIXES = ("_ms", "_seconds", "_count", "_chars", "_tokens")
HIGHER_IS_BETTER_METRICS = frozenset(
    (
        "mrr",
        "pass_rate",
        "source_metadata_coverage",
        "facet_order_match_rate",
    )
)
HIGHER_IS_BETTER_PREFIXES = ("recall@", "ndcg@", "precision@", "success@")


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_finite_number(value: Any) -> TypeGuard[int | float]:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def extract_metrics(report: dict[str, Any]) -> dict[str, float]:
    metrics_section = report.get("metrics")
    if not isinstance(metrics_section, dict):
        metrics_section = report.get("overall")
    if not isinstance(metrics_section, dict):
        msg = "Report does not contain a supported metrics section"
        raise TypeError(msg)

    metrics: dict[str, float] = {}
    for key, value in metrics_section.items():
        if _is_finite_number(value):
            metrics[key] = float(value)

    elapsed_seconds = report.get("elapsed_seconds")
    if _is_finite_number(elapsed_seconds):
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


def _is_mapping(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _is_non_empty_sequence(value: Any) -> TypeGuard[list[Any] | tuple[Any, ...]]:
    return isinstance(value, list | tuple) and bool(value)


def _has_any_key(record: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in record and record[key] not in (None, "", [], {}) for key in keys)


def _is_present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _coerce_positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _is_positive_integer(value: Any) -> bool:
    return _coerce_positive_integer(value) is not None


def _validate_retrieval_mode(
    value: Any,
    *,
    path: str,
    allowed: frozenset[str],
) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{path} missing non-empty retrieval mode"]
    normalized = value.strip()
    if normalized not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        return [
            f"{path} has unsupported retrieval mode {normalized!r}; expected one of {allowed_values}"
        ]
    return []


def _validate_positive_integer_field(value: Any, *, path: str) -> list[str]:
    if _is_positive_integer(value):
        return []
    return [f"{path} must be a positive integer"]


def _validate_embedding_dimensions(
    runtime: dict[str, Any],
    *,
    path: str,
) -> list[str]:
    provider = str(runtime.get("embedding_provider") or "").strip().lower()
    dimensions = runtime.get("embedding_dimensions")
    if provider in {"none", "not-applicable", "n/a"}:
        if dimensions == 0 or (isinstance(dimensions, str) and dimensions.strip() == "0"):
            return []
        return [f"{path} must be 0 when embedding_provider is {provider!r}"]
    return _validate_positive_integer_field(dimensions, path=path)


def _validate_required_fields(
    mapping: dict[str, Any],
    *,
    path: str,
    fields: tuple[str, ...],
) -> list[str]:
    failures: list[str] = []
    for field in fields:
        if not _is_present(mapping.get(field)):
            failures.append(f"{path} missing non-empty field {field!r}")
    return failures


def _has_case_metric(record: dict[str, Any]) -> bool:
    metrics = record.get("metrics")
    if _is_non_empty_mapping(metrics):
        return any(_is_finite_number(value) for value in metrics.values())
    return any(
        _is_finite_number(value)
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


def _validate_ai_memory_release_metadata(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    runtime = report.get("runtime")
    dataset = report.get("dataset") or report.get("corpus")

    if not isinstance(runtime, dict):
        failures.append("missing non-empty field 'runtime'")
    else:
        runtime_record = cast("dict[str, Any]", runtime)
        failures.extend(
            _validate_required_fields(
                runtime_record,
                path="runtime",
                fields=_AI_MEMORY_RUNTIME_FIELDS,
            )
        )
        failures.extend(
            _validate_retrieval_mode(
                runtime_record.get("retrieval_mode"),
                path="runtime['retrieval_mode']",
                allowed=_AI_MEMORY_RETRIEVAL_MODES,
            )
        )
        failures.extend(
            _validate_embedding_dimensions(runtime_record, path="runtime['embedding_dimensions']")
        )

    if not isinstance(dataset, dict):
        failures.append("missing non-empty field 'dataset' or 'corpus'")
    else:
        dataset_record = cast("dict[str, Any]", dataset)
        failures.extend(
            _validate_required_fields(
                dataset_record,
                path="dataset",
                fields=_AI_MEMORY_DATASET_FIELDS,
            )
        )

    failures.extend(
        _validate_positive_integer_field(report.get("repeat_count"), path="repeat_count")
    )
    if not _is_present(report.get("auth_manifest_id")):
        failures.append("missing non-empty field 'auth_manifest_id'")

    mode = report.get("mode")
    if (
        isinstance(runtime, dict)
        and _is_present(mode)
        and _is_present(runtime.get("retrieval_mode"))
        and mode != runtime.get("retrieval_mode")
    ):
        failures.append(
            f"mode {mode!r} does not match runtime['retrieval_mode'] "
            f"{runtime.get('retrieval_mode')!r}"
        )

    return failures


def _validate_ai_memory_summaries(report: dict[str, Any]) -> list[str]:
    if not any(_is_non_empty_mapping(report.get(key)) for key in _AI_MEMORY_SUMMARY_KEYS):
        keys = "', '".join(_AI_MEMORY_SUMMARY_KEYS)
        return [f"missing per-slice summary field; expected one of '{keys}'"]
    return []


def _validate_ai_memory_isolation(report: dict[str, Any]) -> list[str]:
    overall = report.get("overall")
    if not isinstance(overall, dict):
        return []
    cross_question_count = overall.get("cross_question_result_count")
    if _is_finite_number(cross_question_count) and cross_question_count > 0:
        return [f"overall cross_question_result_count must be 0.0000: {cross_question_count:.4f}"]
    if _is_present(cross_question_count) and not _is_finite_number(cross_question_count):
        return ["overall cross_question_result_count must be finite"]
    return []


def _validate_ai_memory_per_slice_thresholds(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    diagnostics = report.get("diagnostics")
    type_counts = (
        diagnostics.get("question_type_counts")
        if isinstance(diagnostics, dict)
        and isinstance(diagnostics.get("question_type_counts"), dict)
        else {}
    )
    for summary_key in _AI_MEMORY_SUMMARY_KEYS:
        summary = report.get(summary_key)
        if not isinstance(summary, dict):
            continue
        for slice_name, metrics in summary.items():
            if not isinstance(metrics, dict):
                continue
            if summary_key == "per_type" and isinstance(type_counts, dict):
                slice_stats = type_counts.get(slice_name)
                if isinstance(slice_stats, dict):
                    case_count = slice_stats.get("cases")
                    if _is_finite_number(case_count) and case_count < AI_MEMORY_PER_SLICE_MIN_CASES:
                        continue
            metric_values = {
                key: float(value) for key, value in metrics.items() if _is_finite_number(value)
            }
            slice_failures = _validate_metric_thresholds(
                metric_values,
                AI_MEMORY_PER_SLICE_THRESHOLDS,
            )
            failures.extend(
                f"{summary_key}[{slice_name!r}]: {failure}" for failure in slice_failures
            )
    return failures


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
    failures.extend(_validate_ai_memory_release_metadata(report))
    failures.extend(_validate_ai_memory_summaries(report))
    failures.extend(_validate_ai_memory_isolation(report))
    failures.extend(_validate_ai_memory_per_slice_thresholds(report))
    failures.extend(_validate_ai_memory_cases(report))
    return failures


def _validate_external_artifact_metadata(report: dict[str, Any]) -> list[str]:
    external_artifact = report.get("external_artifact")
    if not isinstance(external_artifact, dict):
        return ["missing non-empty field 'external_artifact'"]

    failures = _validate_required_fields(
        external_artifact,
        path="external_artifact",
        fields=_AI_MEMORY_EXTERNAL_ARTIFACT_FIELDS,
    )
    if external_artifact.get("provider") != "github-actions":
        failures.append("external_artifact['provider'] must be 'github-actions'")
    if external_artifact.get("gate_profile") != "ai-memory":
        failures.append("external_artifact['gate_profile'] must be 'ai-memory'")
    if external_artifact.get("gate_passed") is not True:
        failures.append("external_artifact['gate_passed'] must be true")
    if not _is_positive_integer(external_artifact.get("size_bytes")):
        failures.append("external_artifact['size_bytes'] must be a positive integer")
    if not _is_positive_integer(external_artifact.get("archive_size_bytes")):
        failures.append("external_artifact['archive_size_bytes'] must be a positive integer")

    sha256 = external_artifact.get("sha256")
    normalized_sha256 = sha256.strip().lower() if isinstance(sha256, str) else ""
    if len(normalized_sha256) != SHA256_HEX_LENGTH or any(
        character not in SHA256_HEX_CHARACTERS for character in normalized_sha256
    ):
        failures.append("external_artifact['sha256'] must be a 64-character hex digest")

    return failures


def validate_external_ai_memory_record(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    failures.extend(_validate_ai_memory_header(report))
    failures.extend(_validate_ai_memory_scope(report))
    failures.extend(_validate_ai_memory_release_metadata(report))
    failures.extend(_validate_ai_memory_summaries(report))
    failures.extend(_validate_ai_memory_isolation(report))
    failures.extend(_validate_ai_memory_per_slice_thresholds(report))
    case_result_count = _coerce_positive_integer(report.get("case_results"))
    total_question_count = _coerce_positive_integer(report.get("total_questions"))
    if case_result_count is None:
        failures.append("case_results must be a positive integer")
    if total_question_count is None:
        failures.append("total_questions must be a positive integer")
    if (
        case_result_count is not None
        and total_question_count is not None
        and case_result_count != total_question_count
    ):
        failures.append("case_results must equal total_questions")
    failures.extend(_validate_external_artifact_metadata(report))
    return failures


def validate_context_pack_release_metadata(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    label = report.get("label")
    if not isinstance(label, str) or not label.strip():
        failures.append("missing non-empty field 'label'")

    metadata = report.get("metadata")
    if not isinstance(metadata, dict):
        return [*failures, "report metadata is missing or invalid"]
    metadata_record = cast("dict[str, Any]", metadata)
    failures.extend(
        _validate_required_fields(
            metadata_record,
            path="metadata",
            fields=_RELEASE_METADATA_FIELDS,
        )
    )
    failures.extend(
        _validate_retrieval_mode(
            metadata_record.get("retrieval_mode"),
            path="metadata['retrieval_mode']",
            allowed=_CONTEXT_PACK_RETRIEVAL_MODES,
        )
    )
    failures.extend(
        _validate_positive_integer_field(
            metadata_record.get("embedding_dimensions"),
            path="metadata['embedding_dimensions']",
        )
    )
    failures.extend(
        _validate_positive_integer_field(
            metadata_record.get("repeat_count"),
            path="metadata['repeat_count']",
        )
    )
    retrieval_mode = metadata_record.get("retrieval_mode")
    if isinstance(label, str) and isinstance(retrieval_mode, str):
        normalized_label = label.lower()
        normalized_mode = retrieval_mode.lower()
        if normalized_mode not in normalized_label:
            failures.append(f"label {label!r} must include retrieval mode {retrieval_mode!r}")
    return failures


def _validate_report_metadata_requirements(
    report: dict[str, Any],
    required_metadata: dict[str, str] | None,
) -> list[str]:
    if not required_metadata:
        return []
    metadata = report.get("metadata")
    if not isinstance(metadata, dict):
        return ["report metadata is missing or invalid"]
    failures: list[str] = []
    for key, expected in required_metadata.items():
        actual = metadata.get(key)
        if actual != expected:
            failures.append(f"metadata[{key!r}] expected {expected!r}, got {actual!r}")
    return failures


def _validate_profile_requirements(
    report: dict[str, Any],
    profile: ProfileName,
) -> list[str]:
    if profile == "ai-memory":
        return validate_ai_memory_record(report)
    if profile == "context-pack":
        return validate_context_pack_release_metadata(report)
    return []


def _validate_metric_thresholds(
    metrics: dict[str, float],
    thresholds: dict[str, MetricThreshold],
) -> list[str]:
    failures: list[str] = []
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


def _baseline_metric_names(
    profile: ProfileName,
    requested_metrics: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    if requested_metrics:
        return tuple(dict.fromkeys(requested_metrics))
    return tuple(PROFILE_THRESHOLDS[profile])


def _validate_regression_tolerances(tolerances: dict[str, float]) -> list[str]:
    failures: list[str] = []
    for metric, value in sorted(tolerances.items()):
        if not math.isfinite(value):
            failures.append(f"max regression for metric {metric!r} must be finite")
        elif value < 0:
            failures.append(f"max regression for metric {metric!r} must be non-negative")
    return failures


def _metric_direction(metric: str, profile: ProfileName) -> MetricDirection | None:
    threshold = PROFILE_THRESHOLDS[profile].get(metric)
    if threshold is not None:
        if threshold.maximum is not None and threshold.minimum is None:
            return "lower"
        if threshold.minimum is not None and threshold.maximum is None:
            return "higher"

    if metric in LOWER_IS_BETTER_METRICS or metric.endswith(LOWER_IS_BETTER_SUFFIXES):
        return "lower"
    if metric in HIGHER_IS_BETTER_METRICS or metric.startswith(HIGHER_IS_BETTER_PREFIXES):
        return "higher"
    return None


def evaluate_baseline_regressions(
    candidate_report: dict[str, Any],
    baseline_report: dict[str, Any],
    *,
    profile: ProfileName,
    metrics: list[str] | tuple[str, ...] | None = None,
    max_regressions: dict[str, float] | None = None,
) -> list[str]:
    candidate_metrics = _extract_metrics_for_profile(candidate_report, profile)
    baseline_metrics = _extract_metrics_for_profile(baseline_report, profile)
    metric_names = _baseline_metric_names(profile, metrics)
    tolerances = max_regressions or {}
    failures = _validate_regression_tolerances(tolerances)

    for metric in sorted(metric_names):
        baseline = baseline_metrics.get(metric)
        candidate = candidate_metrics.get(metric)
        if baseline is None:
            failures.append(f"baseline missing metric {metric!r}")
            continue
        if candidate is None:
            failures.append(f"candidate missing metric {metric!r}")
            continue

        tolerance = tolerances.get(metric, 0.0)
        direction = _metric_direction(metric, profile)
        if direction is None:
            failures.append(f"metric {metric!r} has unknown regression direction")
            continue
        if direction == "lower":
            regression = candidate - baseline
            if regression > tolerance:
                failures.append(
                    f"metric {metric!r} regressed above baseline {baseline:.4f} "
                    f"by {regression:.4f}; allowed {tolerance:.4f}"
                )
            continue

        regression = baseline - candidate
        if regression > tolerance:
            failures.append(
                f"metric {metric!r} regressed below baseline {baseline:.4f} "
                f"by {regression:.4f}; allowed {tolerance:.4f}"
            )

    return failures


def evaluate_external_ai_memory_report(report: dict[str, Any]) -> list[str]:
    try:
        metrics = extract_metrics(report)
    except TypeError:
        metrics = {}
    thresholds = build_thresholds(profile="ai-memory", minimums={}, maximums={})
    failures = validate_external_ai_memory_record(report)
    failures.extend(_validate_metric_thresholds(metrics, thresholds))
    return failures


def _validate_manifest_report_fields(
    entry: dict[str, Any],
    report: dict[str, Any],
    *,
    case_result_count: int | None,
    label: str,
) -> list[str]:
    failures: list[str] = []
    expected_pairs = (
        ("status", "citable"),
        ("gate_profile", "ai-memory"),
        ("suite", report.get("suite")),
        ("suite_version", report.get("suite_version")),
        ("mode", report.get("mode")),
        ("questions", report.get("total_questions")),
        ("case_results", case_result_count),
        ("elapsed_seconds", report.get("elapsed_seconds")),
        ("runtime", report.get("runtime")),
        ("dataset", report.get("dataset")),
        ("overall", report.get("overall")),
        ("claim_boundary", report.get("claim_boundary")),
        ("repeat_count", report.get("repeat_count")),
        ("auth_manifest_id", report.get("auth_manifest_id")),
        ("sibyl_commit", report.get("sibyl_commit")),
    )
    for field, expected in expected_pairs:
        if entry.get(field) != expected:
            failures.append(f"{label}: manifest {field} does not match artifact")
    return failures


def _validate_external_citable_manifest_entry(
    entry: dict[str, Any],
    *,
    index: int,
    manifest_path: Path,
    manifest_name: str,
) -> list[str]:
    manifest_file = manifest_path.parent / manifest_name
    if not manifest_file.exists():
        return [f"citable[{index}] external artifact manifest does not exist: {manifest_name}"]

    report = load_report(manifest_file)
    failures = [
        f"{manifest_name}: {failure}" for failure in evaluate_external_ai_memory_report(report)
    ]
    case_result_count = _coerce_positive_integer(report.get("case_results"))
    failures.extend(
        _validate_manifest_report_fields(
            entry,
            report,
            case_result_count=case_result_count,
            label=manifest_name,
        )
    )
    return failures


def _validate_citable_manifest_entry(
    entry: dict[str, Any],
    *,
    index: int,
    manifest_path: Path,
) -> list[str]:
    failures: list[str] = []
    artifact_name = entry.get("artifact")
    external_manifest_name = entry.get("external_artifact_manifest")
    if _is_present(artifact_name) and _is_present(external_manifest_name):
        return [f"citable[{index}] must not include both artifact and external_artifact_manifest"]
    if isinstance(external_manifest_name, str) and external_manifest_name.strip():
        return _validate_external_citable_manifest_entry(
            entry,
            index=index,
            manifest_path=manifest_path,
            manifest_name=external_manifest_name,
        )
    if not isinstance(artifact_name, str) or not artifact_name.strip():
        return [f"citable[{index}] missing non-empty artifact or external_artifact_manifest"]

    artifact_path = manifest_path.parent / artifact_name
    if not artifact_path.exists():
        return [f"citable[{index}] artifact does not exist: {artifact_name}"]

    report = load_report(artifact_path)
    failures.extend(
        f"{artifact_name}: {failure}" for failure in evaluate_report(report, profile="ai-memory")
    )

    case_results = report.get("case_results")
    case_result_count = len(case_results) if isinstance(case_results, list) else None
    failures.extend(
        _validate_manifest_report_fields(
            entry,
            report,
            case_result_count=case_result_count,
            label=artifact_name,
        )
    )

    return failures


def _validate_planned_manifest_entries(planned: Any) -> list[str]:
    failures: list[str] = []
    if not isinstance(planned, list):
        return failures
    for index, entry in enumerate(planned):
        if not _is_mapping(entry):
            failures.append(f"planned[{index}] is not an object")
            continue
        if entry.get("status") != "planned":
            failures.append(f"planned[{index}] status is not 'planned'")
        if "artifact" in entry:
            failures.append(f"planned[{index}] must not include artifact")
        if "external_artifact_manifest" in entry:
            failures.append(f"planned[{index}] must not include external_artifact_manifest")
    return failures


def _parse_manifest_metric_list(value: Any, *, path: str) -> tuple[list[str] | None, list[str]]:
    if value is None:
        return None, []
    if not isinstance(value, list) or not value:
        return None, [f"{path} must be a non-empty string list"]
    metrics: list[str] = []
    failures: list[str] = []
    for index, metric in enumerate(value):
        if not isinstance(metric, str) or not metric.strip():
            failures.append(f"{path}[{index}] must be a non-empty string")
            continue
        metrics.append(metric.strip())
    return metrics, failures


def _parse_manifest_regression_tolerances(
    value: Any,
    *,
    path: str,
) -> tuple[dict[str, float], list[str]]:
    if value is None:
        return {}, []
    if not isinstance(value, dict):
        return {}, [f"{path} must be an object"]

    tolerances: dict[str, float] = {}
    failures: list[str] = []
    for metric, raw_tolerance in value.items():
        if not isinstance(metric, str) or not metric.strip():
            failures.append(f"{path} contains a non-string metric key")
            continue
        if not _is_finite_number(raw_tolerance):
            failures.append(f"{path}[{metric!r}] must be finite numeric")
            continue
        tolerances[metric.strip()] = float(raw_tolerance)
    failures.extend(_validate_regression_tolerances(tolerances))
    return tolerances, failures


def _validate_no_regression_manifest_entries(
    manifest: dict[str, Any],
    manifest_path: Path,
) -> list[str]:
    if "no_regression" not in manifest:
        return []
    entries = manifest.get("no_regression")
    if not isinstance(entries, list):
        return ["no_regression must be a list"]

    citable = manifest.get("citable")
    citable_entries = citable if isinstance(citable, list) else []
    citable_artifacts = {
        entry.get("artifact") for entry in citable_entries if isinstance(entry, dict)
    }
    failures: list[str] = []
    for index, entry in enumerate(entries):
        prefix = f"no_regression[{index}]"
        if not _is_mapping(entry):
            failures.append(f"{prefix} is not an object")
            continue

        candidate_name = entry.get("candidate")
        baseline_name = entry.get("baseline")
        if not isinstance(candidate_name, str) or not candidate_name.strip():
            failures.append(f"{prefix} missing non-empty candidate")
        if not isinstance(baseline_name, str) or not baseline_name.strip():
            failures.append(f"{prefix} missing non-empty baseline")
        if isinstance(candidate_name, str) and candidate_name not in citable_artifacts:
            failures.append(f"{prefix} candidate {candidate_name!r} is not citable")

        profile_value = entry.get("profile", "ai-memory")
        if not isinstance(profile_value, str) or profile_value not in PROFILE_THRESHOLDS:
            failures.append(f"{prefix} has unsupported profile {profile_value!r}")
            continue
        profile = cast("ProfileName", profile_value)

        metrics, metric_failures = _parse_manifest_metric_list(
            entry.get("metrics"),
            path=f"{prefix}.metrics",
        )
        tolerances, tolerance_failures = _parse_manifest_regression_tolerances(
            entry.get("max_regression"),
            path=f"{prefix}.max_regression",
        )
        failures.extend(metric_failures)
        failures.extend(tolerance_failures)
        if metric_failures or tolerance_failures:
            continue
        if not isinstance(candidate_name, str) or not isinstance(baseline_name, str):
            continue

        candidate_path = manifest_path.parent / candidate_name
        baseline_path = manifest_path.parent / baseline_name
        if not candidate_path.exists():
            failures.append(f"{prefix} candidate does not exist: {candidate_name}")
            continue
        if not baseline_path.exists():
            failures.append(f"{prefix} baseline does not exist: {baseline_name}")
            continue

        candidate_report = load_report(candidate_path)
        baseline_report = load_report(baseline_path)
        failures.extend(
            f"{prefix} {candidate_name}: {failure}"
            for failure in evaluate_baseline_regressions(
                candidate_report,
                baseline_report,
                profile=profile,
                metrics=metrics,
                max_regressions=tolerances,
            )
        )

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
    failures.extend(_validate_report_metadata_requirements(report, required_metadata))
    failures.extend(_validate_profile_requirements(report, profile))
    failures.extend(_validate_metric_thresholds(metrics, thresholds))
    return failures


def validate_ai_memory_manifest(manifest_path: Path) -> list[str]:
    manifest = load_report(manifest_path)
    failures: list[str] = []
    citable = manifest.get("citable")
    if not isinstance(citable, list) or not citable:
        failures.append("manifest missing non-empty citable list")
        return failures

    for index, entry in enumerate(citable):
        if not _is_mapping(entry):
            failures.append(f"citable[{index}] is not an object")
            continue
        failures.extend(
            _validate_citable_manifest_entry(
                entry,
                index=index,
                manifest_path=manifest_path,
            )
        )

    failures.extend(_validate_no_regression_manifest_entries(manifest, manifest_path))
    failures.extend(_validate_planned_manifest_entries(manifest.get("planned")))
    return failures


def _report_name(report: dict[str, Any], fallback: str) -> str:
    for key in ("label", "suite", "search_type"):
        value = report.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def _gate_default_manifest() -> int:
    manifest_path = DEFAULT_AI_MEMORY_MANIFEST
    failures = validate_ai_memory_manifest(manifest_path)
    display_path = manifest_path.relative_to(REPO_ROOT)
    _echo()
    _echo(f"Checking {display_path} with the ai-memory manifest profile")
    if failures:
        _echo()
        _echo("Gate failed:")
        for failure in failures:
            _echo(f"  - {failure}")
        return 1
    _echo()
    _echo("Gate passed")
    return 0


def _print_thresholds(
    metrics: dict[str, float],
    thresholds: dict[str, MetricThreshold],
) -> None:
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


def _print_baseline_comparison(
    candidate_metrics: dict[str, float],
    baseline_metrics: dict[str, float],
    *,
    profile: ProfileName,
    requested_metrics: list[str] | tuple[str, ...] | None,
    max_regressions: dict[str, float],
) -> None:
    _echo()
    _echo("Baseline comparison")
    for metric in sorted(_baseline_metric_names(profile, requested_metrics)):
        baseline = baseline_metrics.get(metric)
        candidate = candidate_metrics.get(metric)
        if baseline is None or candidate is None:
            _echo(f"  {metric}: missing")
            continue
        tolerance = max_regressions.get(metric, 0.0)
        direction = _metric_direction(metric, profile)
        if direction is None:
            _echo(f"  {metric}: unknown regression direction")
            continue
        if direction == "lower":
            regression = candidate - baseline
            check = f"<= baseline + {tolerance:.4f}"
        else:
            regression = baseline - candidate
            check = f">= baseline - {tolerance:.4f}"
        status = "regressed" if regression > tolerance else "ok"
        _echo(f"  {metric}: baseline {baseline:.4f}, candidate {candidate:.4f} ({check}; {status})")


def _extract_metrics_for_profile(
    report: dict[str, Any],
    profile: ProfileName,
) -> dict[str, float]:
    try:
        return extract_metrics(report)
    except TypeError:
        if profile != "ai-memory":
            raise
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enforce threshold gates on a saved Sibyl eval report."
    )
    parser.add_argument(
        "report",
        nargs="?",
        type=Path,
        help=(
            "Saved evaluation report JSON. When omitted, gates the committed AI-memory manifest."
        ),
    )
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
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Saved baseline report JSON for no-regression comparison.",
    )
    parser.add_argument(
        "--baseline-metric",
        action="append",
        default=[],
        metavar="KEY",
        help="Metric to compare against the baseline. Defaults to profile metrics.",
    )
    parser.add_argument(
        "--max-regression",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Allowed absolute regression for a baseline metric. Defaults to zero.",
    )
    args = parser.parse_args(argv)

    if args.report is None:
        if args.baseline is not None or args.baseline_metric or args.max_regression:
            parser.error("--baseline options require a report argument")
        return _gate_default_manifest()
    if args.baseline is None and args.baseline_metric:
        parser.error("--baseline-metric requires --baseline")
    if args.baseline is None and args.max_regression:
        parser.error("--max-regression requires --baseline")

    try:
        minimums = parse_kv_pairs(args.min_metric, value_kind="float")
        maximums = parse_kv_pairs(args.max_metric, value_kind="float")
        required_metadata = parse_kv_pairs(args.require_metadata, value_kind="string")
        max_regressions = parse_kv_pairs(args.max_regression, value_kind="float")
    except ValueError as exc:
        parser.error(str(exc))
    regression_tolerance_failures = _validate_regression_tolerances(max_regressions)
    if regression_tolerance_failures:
        parser.error("; ".join(regression_tolerance_failures))

    report = load_report(args.report)
    baseline_report = load_report(args.baseline) if args.baseline is not None else None
    failures = evaluate_report(
        report,
        profile=args.profile,
        minimums=minimums,
        maximums=maximums,
        required_metadata=required_metadata,
    )
    if baseline_report is not None:
        failures.extend(
            evaluate_baseline_regressions(
                report,
                baseline_report,
                profile=args.profile,
                metrics=args.baseline_metric,
                max_regressions=max_regressions,
            )
        )

    report_name = _report_name(report, args.report.stem)
    _echo()
    _echo(f"Checking {report_name} with the {args.profile} profile")
    _echo()
    metrics = _extract_metrics_for_profile(report, args.profile)
    thresholds = build_thresholds(
        profile=args.profile,
        minimums=minimums,
        maximums=maximums,
    )
    _print_thresholds(metrics, thresholds)
    if baseline_report is not None:
        baseline_metrics = _extract_metrics_for_profile(baseline_report, args.profile)
        _print_baseline_comparison(
            metrics,
            baseline_metrics,
            profile=args.profile,
            requested_metrics=args.baseline_metric,
            max_regressions=max_regressions,
        )

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
