#!/usr/bin/env python3
# ruff: noqa: T201
"""Render side-by-side eval quality, latency, token, and cost accounting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ACCURACY_METRICS = ("recall@5", "pass_rate", "success@5", "ndcg@5", "mrr")
COMPARISON_REPORT_COUNT = 2
DELTA_TABLE_HEADER_LINES = 5
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
    "latency_p50_ms",
    "latency_p95_ms",
    "avg_estimated_tokens",
    "max_estimated_tokens",
    "full_context_baseline_estimated_tokens",
    "embedding_call_count",
    "elapsed_seconds",
)
LOWER_IS_BETTER = {
    "latency_ms",
    "latency_p50_ms",
    "latency_p95_ms",
    "elapsed_seconds",
    "avg_estimated_tokens",
    "max_estimated_tokens",
    "full_context_baseline_estimated_tokens",
    "embedding_call_count",
}
ONE_DECIMAL_METRICS = {
    "latency_ms",
    "latency_p50_ms",
    "latency_p95_ms",
    "elapsed_seconds",
    "avg_estimated_tokens",
    "max_estimated_tokens",
    "full_context_baseline_estimated_tokens",
    "embedding_call_count",
}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _metrics(report: dict[str, Any]) -> dict[str, Any]:
    metrics = report.get("metrics")
    if isinstance(metrics, dict):
        return metrics
    overall = report.get("overall")
    if isinstance(overall, dict):
        return overall
    return {}


def _numeric_metrics(report: dict[str, Any]) -> dict[str, float]:
    metrics = _metrics(report)
    extracted = {key: float(value) for key, value in metrics.items() if _is_number(value)}
    elapsed_seconds = report.get("elapsed_seconds")
    if _is_number(elapsed_seconds):
        extracted["elapsed_seconds"] = float(elapsed_seconds)
    return extracted


def _report_name(path: Path, report: dict[str, Any]) -> str:
    for key in ("label", "suite", "search_type"):
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return path.stem


def _format_delta(metric: str, delta: float) -> str:
    sign = "+" if delta >= 0 else "-"
    magnitude = abs(delta)
    if metric in ONE_DECIMAL_METRICS:
        return f"{sign}{magnitude:.1f}"
    return f"{sign}{magnitude:.4f}"


def _accuracy(metrics: dict[str, Any]) -> tuple[str, float | None]:
    for metric in ACCURACY_METRICS:
        value = metrics.get(metric)
        if _is_number(value):
            return metric, float(value)
    return "n/a", None


def _fallback_token_estimate(metrics: dict[str, Any]) -> Any:
    for key in ("estimated_input_tokens", "full_context_baseline_estimated_tokens"):
        if _is_number(metrics.get(key)):
            return metrics[key]
    cases = metrics.get("cases")
    for key in ("avg_budgeted_estimated_tokens", "avg_estimated_tokens"):
        if _is_number(metrics.get(key)) and _is_number(cases):
            return float(metrics[key]) * float(cases)
    return metrics.get("avg_estimated_tokens")


def _accounting(report: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    accounting = report.get("accounting")
    if not isinstance(accounting, dict):
        return {
            "schema_version": "missing",
            "p50_ms": metrics.get("latency_p50_ms"),
            "p95_ms": metrics.get("latency_p95_ms"),
            "token_estimate": _fallback_token_estimate(metrics),
            "embedding_calls": metrics.get("embedding_call_count"),
            "estimated_cost_usd": None,
        }

    latency = accounting.get("latency") if isinstance(accounting.get("latency"), dict) else {}
    tokens = accounting.get("tokens") if isinstance(accounting.get("tokens"), dict) else {}
    embedding = accounting.get("embedding") if isinstance(accounting.get("embedding"), dict) else {}
    cost = accounting.get("cost") if isinstance(accounting.get("cost"), dict) else {}
    return {
        "schema_version": accounting.get("schema_version"),
        "p50_ms": latency.get("p50_ms"),
        "p95_ms": latency.get("p95_ms"),
        "token_estimate": tokens.get("estimated_input_tokens"),
        "embedding_calls": embedding.get("calls"),
        "estimated_cost_usd": cost.get("estimated_total_usd"),
    }


def summarize_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    metrics = _metrics(report)
    accuracy_metric, accuracy_value = _accuracy(metrics)
    accounting = _accounting(report, metrics)
    return {
        "report": _report_name(path, report),
        "path": str(path),
        "metrics": _numeric_metrics(report),
        "accuracy_metric": accuracy_metric,
        "accuracy": accuracy_value,
        **accounting,
    }


def _format_number(value: Any, *, precision: int = 4) -> str:
    if not _is_number(value):
        return "n/a"
    return f"{float(value):.{precision}f}"


def _format_cost(value: Any) -> str:
    if not _is_number(value):
        return "n/a"
    return f"${float(value):.6f}"


def render_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| report | accuracy | p50 ms | p95 ms | token estimate | embedding calls | estimated cost | accounting |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        accuracy = (
            "n/a"
            if row["accuracy"] is None
            else f"{row['accuracy_metric']}={_format_number(row['accuracy'])}"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["report"]),
                    accuracy,
                    _format_number(row["p50_ms"], precision=1),
                    _format_number(row["p95_ms"], precision=1),
                    _format_number(row["token_estimate"], precision=0),
                    _format_number(row["embedding_calls"], precision=0),
                    _format_cost(row["estimated_cost_usd"]),
                    str(row["schema_version"]),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def render_metric_deltas(rows: list[dict[str, Any]], metrics: list[str]) -> str:
    if len(rows) != COMPARISON_REPORT_COUNT:
        return ""
    baseline, candidate = rows
    baseline_metrics = baseline["metrics"]
    candidate_metrics = candidate["metrics"]
    lines = [
        "",
        "Metric deltas",
        "",
        "| metric | baseline | candidate | delta | winner |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for metric in metrics:
        if metric not in baseline_metrics or metric not in candidate_metrics:
            continue
        baseline_value = baseline_metrics[metric]
        candidate_value = candidate_metrics[metric]
        delta = candidate_value - baseline_value
        lower_is_better = metric in LOWER_IS_BETTER
        if delta == 0:
            winner = "tie"
        elif lower_is_better:
            winner = candidate["report"] if delta < 0 else baseline["report"]
        else:
            winner = candidate["report"] if delta > 0 else baseline["report"]
        precision = 1 if metric in ONE_DECIMAL_METRICS else 4
        lines.append(
            "| "
            + " | ".join(
                [
                    metric,
                    _format_number(baseline_value, precision=precision),
                    _format_number(candidate_value, precision=precision),
                    _format_delta(metric, delta),
                    str(winner),
                ]
            )
            + " |"
        )
    return "\n".join(lines) if len(lines) > DELTA_TABLE_HEADER_LINES else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare saved Sibyl eval reports across quality, latency, tokens, and cost."
    )
    parser.add_argument("reports", nargs="+", type=Path, help="Saved eval report JSON files.")
    parser.add_argument(
        "--metrics",
        nargs="*",
        default=list(DEFAULT_METRICS),
        help="Metric keys to include in the two-report delta table.",
    )
    args = parser.parse_args(argv)

    rows = [summarize_report(path) for path in args.reports]
    print(render_markdown(rows))
    deltas = render_metric_deltas(rows, args.metrics)
    if deltas:
        print(deltas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
