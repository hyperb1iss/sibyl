#!/usr/bin/env python3
"""Record native retrieval compare runs for the eventual default flip."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_HISTORY_PATH = Path(".moon/retrieval-mode-history.json")
DEFAULT_REQUIRED_CONSECUTIVE = 3
DEFAULT_MAX_RECORDS = 50
REQUIRED_CONTEXT_REPEAT_COUNT = "20"

REQUIRED_CONTEXT_METRICS: dict[str, tuple[str, float]] = {
    "pass_rate": ("minimum", 1.0),
    "latency_p95_ms": ("maximum", 1000.0),
    "source_metadata_coverage": ("minimum", 1.0),
    "facet_order_match_rate": ("minimum", 1.0),
    "leak_count": ("maximum", 0.0),
    "forbidden_term_matches": ("maximum", 0.0),
}


def load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = "retrieval compare report must be a JSON object"
        raise TypeError(msg)
    return payload


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        records = payload.get("records", [])
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
    msg = "retrieval mode history must contain a records list"
    raise TypeError(msg)


def save_history(
    path: Path,
    records: list[dict[str, Any]],
    *,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "records": records[-max_records:],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _metadata(report: dict[str, Any]) -> dict[str, Any]:
    metadata = report.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _metrics(report: dict[str, Any]) -> dict[str, float]:
    raw_metrics = report.get("metrics")
    if not isinstance(raw_metrics, dict):
        return {}
    metrics: dict[str, float] = {}
    for key, value in raw_metrics.items():
        if isinstance(value, int | float) and not isinstance(value, bool):
            metrics[key] = float(value)
    return metrics


def _per_case_failures(report: dict[str, Any]) -> list[str]:
    per_case = report.get("per_case")
    if not isinstance(per_case, list):
        return []

    failures: list[str] = []
    for raw_case in per_case:
        if not isinstance(raw_case, dict):
            continue
        name = raw_case.get("name") or raw_case.get("fixture") or "<unknown>"
        if raw_case.get("passed") is not True:
            failures.append(f"case {name!r} did not pass")
        if raw_case.get("error"):
            failures.append(f"case {name!r} errored")
    return failures


def current_run_blockers(
    report: dict[str, Any],
    *,
    branch: str,
    policy_affecting_diffs: int,
) -> list[str]:
    blockers: list[str] = []
    metadata = _metadata(report)
    metrics = _metrics(report)

    if branch != "main":
        blockers.append(f"branch {branch!r} is not main")
    if metadata.get("retrieval_mode") != "compare":
        blockers.append("metadata['retrieval_mode'] is not 'compare'")
    if str(metadata.get("repeat_count") or "") != REQUIRED_CONTEXT_REPEAT_COUNT:
        blockers.append(f"metadata['repeat_count'] is not {REQUIRED_CONTEXT_REPEAT_COUNT!r}")
    if policy_affecting_diffs != 0:
        blockers.append(f"policy_affecting_diffs is {policy_affecting_diffs}")

    for metric, (kind, threshold) in REQUIRED_CONTEXT_METRICS.items():
        actual = metrics.get(metric)
        if actual is None:
            blockers.append(f"missing metric {metric!r}")
            continue
        if kind == "minimum" and actual < threshold:
            blockers.append(f"metric {metric!r} below {threshold:.4f}: {actual:.4f}")
        if kind == "maximum" and actual > threshold:
            blockers.append(f"metric {metric!r} above {threshold:.4f}: {actual:.4f}")

    blockers.extend(_per_case_failures(report))
    return blockers


def build_record(
    report: dict[str, Any],
    *,
    report_path: Path,
    branch: str,
    sha: str,
    run_id: str,
    run_attempt: str,
    event: str,
    workflow: str,
    policy_affecting_diffs: int,
) -> dict[str, Any]:
    blockers = current_run_blockers(
        report,
        branch=branch,
        policy_affecting_diffs=policy_affecting_diffs,
    )
    metadata = _metadata(report)
    metrics = _metrics(report)
    tracked_metrics = {
        key: metrics[key] for key in sorted(REQUIRED_CONTEXT_METRICS) if key in metrics
    }
    if "latency_ms" in metrics:
        tracked_metrics["latency_ms"] = metrics["latency_ms"]

    return {
        "timestamp": report.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "branch": branch,
        "sha": sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "event": event,
        "workflow": workflow,
        "report": str(report_path),
        "retrieval_mode": metadata.get("retrieval_mode"),
        "policy_affecting_diffs": policy_affecting_diffs,
        "metrics": tracked_metrics,
        "qualifies": not blockers,
        "blockers": blockers,
    }


def append_record(
    records: list[dict[str, Any]],
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    identity = (record.get("run_id"), record.get("run_attempt"))
    if all(identity):
        records = [
            existing
            for existing in records
            if (existing.get("run_id"), existing.get("run_attempt")) != identity
        ]
    return [*records, record]


def consecutive_qualifying_count(
    records: list[dict[str, Any]],
    *,
    branch: str = "main",
) -> int:
    count = 0
    branch_records = [record for record in records if record.get("branch") == branch]
    for record in reversed(branch_records):
        if record.get("qualifies") is True:
            count += 1
            continue
        break
    return count


def _env(name: str, fallback: str) -> str:
    value = os.environ.get(name)
    return value if value else fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record retrieval compare history for the native default flip."
    )
    parser.add_argument("report", type=Path, help="Saved context-pack eval report JSON.")
    parser.add_argument(
        "--history",
        type=Path,
        default=DEFAULT_HISTORY_PATH,
        help="History JSON path to update.",
    )
    parser.add_argument("--branch", default=_env("GITHUB_REF_NAME", "local"))
    parser.add_argument("--sha", default=_env("GITHUB_SHA", "local"))
    parser.add_argument("--run-id", default=_env("GITHUB_RUN_ID", "local"))
    parser.add_argument("--run-attempt", default=_env("GITHUB_RUN_ATTEMPT", "1"))
    parser.add_argument("--event", default=_env("GITHUB_EVENT_NAME", "local"))
    parser.add_argument("--workflow", default=_env("GITHUB_WORKFLOW", "local"))
    parser.add_argument(
        "--policy-affecting-diffs",
        type=int,
        default=0,
        help="Count of policy-affecting native-vs-Graphiti compare diffs.",
    )
    parser.add_argument(
        "--required-consecutive",
        type=int,
        default=DEFAULT_REQUIRED_CONSECUTIVE,
        help="Consecutive qualifying main runs required before native can flip.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable status instead of a text summary.",
    )
    args = parser.parse_args(argv)

    report = load_report(args.report)
    record = build_record(
        report,
        report_path=args.report,
        branch=args.branch,
        sha=args.sha,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        event=args.event,
        workflow=args.workflow,
        policy_affecting_diffs=args.policy_affecting_diffs,
    )
    records = append_record(load_history(args.history), record)
    save_history(args.history, records)

    consecutive = consecutive_qualifying_count(records)
    ready = consecutive >= args.required_consecutive
    status = {
        "current_run_qualifies": record["qualifies"],
        "consecutive_main_qualifying": consecutive,
        "required_consecutive": args.required_consecutive,
        "ready_to_flip": ready,
        "blockers": record["blockers"],
        "history": str(args.history),
    }

    if args.json:
        sys.stdout.write(json.dumps(status, sort_keys=True) + "\n")
    else:
        sys.stdout.write("\nRetrieval mode compare history\n\n")
        sys.stdout.write(f"  history: {args.history}\n")
        sys.stdout.write(f"  current_run_qualifies: {str(record['qualifies']).lower()}\n")
        sys.stdout.write(
            f"  consecutive_main_qualifying: {consecutive}/{args.required_consecutive}\n"
        )
        sys.stdout.write(f"  ready_to_flip: {str(ready).lower()}\n")
        for blocker in record["blockers"]:
            sys.stdout.write(f"  blocker: {blocker}\n")

    return 0 if record["qualifies"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
