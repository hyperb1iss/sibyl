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
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AI_MEMORY_MANIFEST = REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "manifest.json"


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


def _is_mapping(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _is_non_empty_sequence(value: Any) -> TypeGuard[list[Any] | tuple[Any, ...]]:
    return isinstance(value, list | tuple) and bool(value)


def _has_any_key(record: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in record and record[key] not in (None, "", [], {}) for key in keys)


def _is_present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _is_positive_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str) and value.strip().isdigit():
        return int(value) > 0
    return False


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
            _validate_positive_integer_field(
                runtime_record.get("embedding_dimensions"),
                path="runtime['embedding_dimensions']",
            )
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
    failures.extend(_validate_ai_memory_cases(report))
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


def _validate_citable_manifest_entry(
    entry: dict[str, Any],
    *,
    index: int,
    manifest_path: Path,
) -> list[str]:
    failures: list[str] = []
    artifact_name = entry.get("artifact")
    if not isinstance(artifact_name, str) or not artifact_name.strip():
        return [f"citable[{index}] missing non-empty artifact"]

    artifact_path = manifest_path.parent / artifact_name
    if not artifact_path.exists():
        return [f"citable[{index}] artifact does not exist: {artifact_name}"]

    report = load_report(artifact_path)
    failures.extend(
        f"{artifact_name}: {failure}" for failure in evaluate_report(report, profile="ai-memory")
    )

    case_results = report.get("case_results")
    case_result_count = len(case_results) if isinstance(case_results, list) else None
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
            failures.append(f"{artifact_name}: manifest {field} does not match artifact")

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
    args = parser.parse_args(argv)

    if args.report is None:
        return _gate_default_manifest()

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
    metrics = _extract_metrics_for_profile(report, args.profile)
    thresholds = build_thresholds(
        profile=args.profile,
        minimums=minimums,
        maximums=maximums,
    )
    _print_thresholds(metrics, thresholds)

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
