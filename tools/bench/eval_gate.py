#!/usr/bin/env python3
"""Enforce threshold gates for saved Sibyl evaluation reports."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

ProfileName = Literal["smoke", "acceptance", "context-pack"]


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
        "source_metadata_coverage": MetricThreshold(minimum=1.0),
        "facet_order_match_rate": MetricThreshold(minimum=1.0),
        "leak_count": MetricThreshold(maximum=0.0),
        "forbidden_term_matches": MetricThreshold(maximum=0.0),
    },
}


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


def evaluate_report(
    report: dict[str, Any],
    *,
    profile: ProfileName,
    minimums: dict[str, float] | None = None,
    maximums: dict[str, float] | None = None,
    required_metadata: dict[str, str] | None = None,
) -> list[str]:
    metrics = extract_metrics(report)
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
    for key in ("label", "search_type"):
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
        choices=("smoke", "acceptance", "context-pack"),
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
    metrics = extract_metrics(report)
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
