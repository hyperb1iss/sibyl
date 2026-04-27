#!/usr/bin/env python3
# ruff: noqa: T201
"""Compare two Sibyl benchmark or eval reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

MetricMap = dict[str, float]
DEFAULT_METRICS = (
    "ndcg@5",
    "ndcg@10",
    "success@5",
    "success@10",
    "precision@5",
    "precision@10",
    "recall@5",
    "recall@10",
    "mrr",
    "pass_rate",
    "latency_ms",
    "avg_estimated_tokens",
    "max_estimated_tokens",
    "avg_markdown_chars",
    "max_markdown_chars",
    "source_metadata_coverage",
    "facet_order_match_rate",
    "forbidden_term_matches",
    "elapsed_seconds",
)

LOWER_IS_BETTER = {
    "latency_ms",
    "elapsed_seconds",
    "avg_estimated_tokens",
    "max_estimated_tokens",
    "avg_markdown_chars",
    "max_markdown_chars",
    "forbidden_term_matches",
}

ONE_DECIMAL_METRICS = {
    "latency_ms",
    "elapsed_seconds",
    "avg_estimated_tokens",
    "max_estimated_tokens",
    "avg_markdown_chars",
    "max_markdown_chars",
}


def _load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_metrics(report: dict[str, Any]) -> MetricMap:
    if isinstance(report.get("metrics"), dict):
        metrics = report["metrics"]
    elif isinstance(report.get("overall"), dict):
        metrics = report["overall"]
    else:
        msg = "Report does not contain a supported metrics section"
        raise TypeError(msg)

    extracted: MetricMap = {}
    for key, value in metrics.items():
        if isinstance(value, int | float):
            extracted[key] = float(value)

    elapsed_seconds = report.get("elapsed_seconds")
    if isinstance(elapsed_seconds, int | float):
        extracted["elapsed_seconds"] = float(elapsed_seconds)

    return extracted


def _report_name(report: dict[str, Any], fallback: str) -> str:
    for key in ("label", "mode", "search_type"):
        value = report.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _format_delta(metric: str, delta: float) -> str:
    sign = "+" if delta >= 0 else "-"
    magnitude = abs(delta)
    if metric in ONE_DECIMAL_METRICS:
        return f"{sign}{magnitude:.1f}"
    return f"{sign}{magnitude:.4f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two Sibyl benchmark reports.")
    parser.add_argument("baseline", type=Path, help="Baseline report JSON.")
    parser.add_argument("candidate", type=Path, help="Candidate report JSON.")
    parser.add_argument(
        "--metrics",
        nargs="*",
        default=list(DEFAULT_METRICS),
        help="Metric keys to compare.",
    )
    args = parser.parse_args()

    baseline_report = _load_report(args.baseline)
    candidate_report = _load_report(args.candidate)
    baseline_metrics = _extract_metrics(baseline_report)
    candidate_metrics = _extract_metrics(candidate_report)

    baseline_name = _report_name(baseline_report, args.baseline.stem)
    candidate_name = _report_name(candidate_report, args.candidate.stem)
    metric_width = max(16, *(len(metric) for metric in args.metrics))

    print(f"\nComparing {candidate_name} against {baseline_name}\n")
    print(
        f"{'metric':<{metric_width}} {'baseline':>12} {'candidate':>12} "
        f"{'delta':>12} {'winner':>12}"
    )
    print(
        f"{'-' * metric_width} {'-' * 12} {'-' * 12} {'-' * 12} {'-' * 12}"
    )

    for metric in args.metrics:
        if metric not in baseline_metrics or metric not in candidate_metrics:
            continue

        baseline_value = baseline_metrics[metric]
        candidate_value = candidate_metrics[metric]
        delta = candidate_value - baseline_value
        lower_is_better = metric in LOWER_IS_BETTER

        if delta == 0:
            winner = "tie"
        elif lower_is_better:
            winner = candidate_name if delta < 0 else baseline_name
        else:
            winner = candidate_name if delta > 0 else baseline_name

        if metric in ONE_DECIMAL_METRICS:
            baseline_display = f"{baseline_value:.1f}"
            candidate_display = f"{candidate_value:.1f}"
        else:
            baseline_display = f"{baseline_value:.4f}"
            candidate_display = f"{candidate_value:.4f}"

        print(
            f"{metric:<{metric_width}} {baseline_display:>12} {candidate_display:>12} "
            f"{_format_delta(metric, delta):>12} {winner:>12}"
        )


if __name__ == "__main__":
    main()
