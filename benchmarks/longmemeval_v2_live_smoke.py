#!/usr/bin/env python3
"""Prove LongMemEval-V2 operational memory against a live isolated Sibyl API."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

BENCHMARKS_ROOT = Path(__file__).resolve().parent
if str(BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS_ROOT))

from longmemeval_v2_memory.sibyl_memory import (  # noqa: E402
    DEFAULT_EVIDENCE_COMPOSITION_MODE,
    EVIDENCE_COMPOSITION_MODES,
    SibylLiveApiMemory,
    build_operational_experience_payload,
)

from sibyl_core.retrieval.query_planning import MAX_SUPPLEMENTAL_QUERIES  # noqa: E402

SMOKE_SCHEMA_VERSION = "sibyl-longmemeval-v2-live-smoke-v1"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    trajectory = load_trajectory(
        Path(args.trajectories).expanduser().resolve(),
        args.trajectory_id,
    )
    report = run_live_smoke(
        trajectory,
        api_url=args.api_url,
        run_id=args.run_id or f"lme-v2-smoke-{uuid4().hex[:12]}",
        timeout_seconds=args.timeout_seconds,
        retrieval_mode=args.retrieval_mode,
        max_planned_queries=args.max_planned_queries,
        evidence_composition_mode=args.evidence_composition_mode,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
    return 0 if report["passed"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:3434/api")
    parser.add_argument("--trajectories", required=True)
    parser.add_argument("--trajectory-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--retrieval-mode", choices=("fast", "accurate"), default="fast")
    parser.add_argument("--max-planned-queries", type=int, default=3)
    parser.add_argument(
        "--evidence-composition-mode",
        choices=sorted(EVIDENCE_COMPOSITION_MODES),
        default=DEFAULT_EVIDENCE_COMPOSITION_MODE,
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if not 1 <= args.max_planned_queries <= MAX_SUPPLEMENTAL_QUERIES:
        parser.error(f"--max-planned-queries must be between 1 and {MAX_SUPPLEMENTAL_QUERIES}")
    return args


def load_trajectory(path: Path, trajectory_id: str) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON on {path}:{line_number}") from exc
            if isinstance(item, dict) and item.get("id") == trajectory_id:
                return {str(key): value for key, value in item.items()}
    raise RuntimeError(f"Trajectory {trajectory_id!r} not found in {path}")


def run_live_smoke(
    trajectory: dict[str, object],
    *,
    api_url: str,
    run_id: str,
    timeout_seconds: float,
    retrieval_mode: str,
    max_planned_queries: int,
    evidence_composition_mode: str,
) -> dict[str, Any]:
    memory = SibylLiveApiMemory(
        {
            "api_url": api_url,
            "allow_localhost": True,
            "allow_signup": True,
            "run_id": run_id,
            "defer_embeddings": True,
            "api_timeout_seconds": timeout_seconds,
            "embedding_job_wait_timeout_seconds": timeout_seconds,
            "max_context_items": 8,
            "search_limit": 12,
            "retrieval_mode": retrieval_mode,
            "retrieval_max_planned_queries": max_planned_queries,
            "evidence_composition_mode": evidence_composition_mode,
        }
    )
    try:
        ingest_started = time.monotonic()
        memory.insert(trajectory)
        memory.finalize_ingest()
        ingest_seconds = time.monotonic() - ingest_started

        query = str(trajectory.get("goal") or "What happened in this experience?")
        query_started = time.monotonic()
        context = memory.query(query)
        query_seconds = time.monotonic() - query_started
        query_receipt = (
            memory.post_query_hook(
                query=query,
                query_image=None,
                memory_context=context,
            )
            or {}
        )

        replay_payload = build_operational_experience_payload(
            trajectory,
            project_id=memory.project_id,
            run_id=memory.run_id,
            content_max_chars=memory.content_max_chars,
            include_screenshot_refs=memory.include_screenshot_refs,
        )
        replay_payload["defer_embeddings"] = True
        replay = memory._request_json(
            "POST",
            "/memory/experience",
            json=replay_payload,
        )

        trace = query_receipt.get("retrieval_trace")
        origins = (
            [str(item.get("selection_origin") or "") for item in trace if isinstance(item, dict)]
            if isinstance(trace, list)
            else []
        )
        report: dict[str, Any] = {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "api_url": api_url,
            "api_runtime": memory.api_runtime,
            "run_id": memory.run_id,
            "project_id": memory.project_id,
            "trajectory_id": trajectory.get("id"),
            "trajectory_states": len(trajectory.get("states") or []),
            "ingest": {
                "seconds": ingest_seconds,
                "written_entities": memory.created_entities,
                "write_receipt": memory.last_experience_write_receipt,
                "embedding_usage": memory.ingest_embedding_usage,
                "pending_embedding_jobs": len(memory._pending_embedding_job_ids),
                "pending_projection_jobs": len(memory._pending_projection_job_ids),
            },
            "query": {
                "seconds": query_seconds,
                "context_items": len(context),
                "selection_origins": origins,
                "retrieval_mode": retrieval_mode,
                "evidence_composition_mode": evidence_composition_mode,
                "receipt": query_receipt,
            },
            "replay": replay,
        }
        report["checks"] = evaluate_smoke_report(report)
        report["passed"] = all(report["checks"].values())
        return report
    finally:
        memory._client.close()


def evaluate_smoke_report(report: dict[str, Any]) -> dict[str, bool]:
    runtime = report.get("api_runtime")
    ingest = report.get("ingest")
    query = report.get("query")
    replay = report.get("replay")
    origins = query.get("selection_origins") if isinstance(query, dict) else []
    write_receipt = ingest.get("write_receipt") if isinstance(ingest, dict) else None
    relationship_ids = (
        write_receipt.get("relationship_ids") if isinstance(write_receipt, dict) else None
    )
    checks = {
        "api_healthy": isinstance(runtime, dict) and runtime.get("status") == "healthy",
        "fresh_write_created_entities": (
            isinstance(ingest, dict) and int(ingest.get("written_entities") or 0) > 0
        ),
        "embedding_jobs_drained": (
            isinstance(ingest, dict)
            and ingest.get("pending_embedding_jobs") == 0
            and ingest.get("pending_projection_jobs") == 0
        ),
        "relationship_inventory_persisted": (
            isinstance(write_receipt, dict)
            and isinstance(relationship_ids, list)
            and len(relationship_ids) > 0
            and write_receipt.get("written_relationships") == len(relationship_ids)
        ),
        "query_returned_context": (
            isinstance(query, dict) and int(query.get("context_items") or 0) > 0
        ),
        "typed_evidence_selected": (
            isinstance(origins, list)
            and any(str(origin).startswith("context_pack:") for origin in origins)
        ),
        "raw_evidence_selected": (
            isinstance(origins, list) and any(origin == "search" for origin in origins)
        ),
        "unchanged_replay_zero_write": (
            isinstance(replay, dict)
            and replay.get("written_entities") == 0
            and replay.get("written_relationships") == 0
            and replay.get("deleted_entities") == 0
            and replay.get("deleted_relationships") == 0
            and not replay.get("background_jobs")
        ),
    }
    if isinstance(query, dict) and query.get("retrieval_mode") == "accurate":
        receipt = query.get("receipt")
        search_metadata = receipt.get("search_metadata") if isinstance(receipt, dict) else None
        planner_usage = (
            search_metadata.get("planner_usage") if isinstance(search_metadata, dict) else None
        )
        planned_queries = (
            search_metadata.get("planned_queries") if isinstance(search_metadata, dict) else None
        )
        checks.update(
            {
                "accurate_planner_succeeded": (
                    isinstance(search_metadata, dict)
                    and search_metadata.get("planner_status") == "success"
                ),
                "accurate_query_fanout_bounded": (
                    isinstance(planned_queries, list)
                    and 0 < len(planned_queries) <= MAX_SUPPLEMENTAL_QUERIES
                    and search_metadata.get("query_count") == len(planned_queries) + 1
                ),
                "accurate_planner_usage_recorded": (
                    isinstance(planner_usage, dict)
                    and int(planner_usage.get("requests") or 0) >= 1
                    and int(planner_usage.get("total_tokens") or 0) > 0
                    and bool(planner_usage.get("provider"))
                    and bool(planner_usage.get("model"))
                ),
            }
        )
    return checks


if __name__ == "__main__":
    raise SystemExit(main())
