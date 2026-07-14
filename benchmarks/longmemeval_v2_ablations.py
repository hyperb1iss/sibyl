#!/usr/bin/env python3
"""Plan, run, and gate resumable LongMemEval-V2 retrieval ablations."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.git_provenance import git_provenance  # noqa: E402
from benchmarks.longmemeval_v2_reader_holdout import (  # noqa: E402
    build_reader_holdout_plan,
    run_reader_holdout_plan,
)
from benchmarks.longmemeval_v2_reader_holdout_report import (  # noqa: E402
    build_reader_holdout_report,
)
from benchmarks.longmemeval_v2_reader_replication import (  # noqa: E402
    DEFAULT_MAX_WORKERS,
    build_reader_replication_plan,
    run_reader_replication_plan,
)
from benchmarks.longmemeval_v2_reader_replication_report import (  # noqa: E402
    build_reader_replication_report,
)
from benchmarks.longmemeval_v2_reader_report import build_reader_report  # noqa: E402

SCHEMA_VERSION = "sibyl-longmemeval-v2-ablations-v1"
GATE_SCHEMA_VERSION = "sibyl-longmemeval-v2-ablation-gate-v1"
TREATMENT_GATE_SCHEMA_VERSION = "sibyl-longmemeval-v2-treatment-gate-v1"
TREATMENT_SCHEMA_VERSION = "sibyl-longmemeval-v2-treatment-v1"
READER_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-plan-v1"
SLICE_SCHEMA_VERSION = "sibyl-longmemeval-v2-diagnostic-slice-v1"
DEFAULT_SLICE = ROOT / "benchmarks" / "longmemeval_v2_diagnostic_slice.json"
DEFAULT_QUERY_WORKERS = 16
DEFAULT_STANDARD_CONTEXT_TOKENS = 200_000
DOMAINS = ("web", "enterprise")
BASELINE_ARM = "trajectory_18k"
REPRESENTATIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "trajectory_18k",
        "chunking_mode": "trajectory",
        "content_max_chars": 18_000,
    },
    {
        "name": "state_18k",
        "chunking_mode": "state",
        "content_max_chars": 18_000,
    },
    {
        "name": "state_8k",
        "chunking_mode": "state",
        "content_max_chars": 8_000,
    },
)
RETRIEVAL_ARMS: tuple[dict[str, Any], ...] = (
    {
        "name": "trajectory_18k",
        "representation": "trajectory_18k",
        "search_limit": 12,
        "max_context_items": 8,
        "max_chunks_per_trajectory": 8,
        "neighbor_stitch_items": 0,
        "neighbor_stitch_span": 0,
        "state_part_completion_items": 0,
        "state_part_refinement": False,
    },
    {
        "name": "state_18k",
        "representation": "state_18k",
        "search_limit": 12,
        "max_context_items": 8,
        "max_chunks_per_trajectory": 8,
        "neighbor_stitch_items": 0,
        "neighbor_stitch_span": 0,
        "state_part_completion_items": 0,
        "state_part_refinement": False,
    },
    {
        "name": "state_8k",
        "representation": "state_8k",
        "search_limit": 12,
        "max_context_items": 8,
        "max_chunks_per_trajectory": 8,
        "neighbor_stitch_items": 0,
        "neighbor_stitch_span": 0,
        "state_part_completion_items": 0,
        "state_part_refinement": False,
    },
    {
        "name": "state_8k_diverse",
        "representation": "state_8k",
        "search_limit": 12,
        "max_context_items": 8,
        "max_chunks_per_trajectory": 2,
        "neighbor_stitch_items": 0,
        "neighbor_stitch_span": 0,
        "state_part_completion_items": 0,
        "state_part_refinement": False,
    },
    {
        "name": "state_8k_diverse_neighbors",
        "representation": "state_8k",
        "search_limit": 12,
        "max_context_items": 8,
        "max_chunks_per_trajectory": 2,
        "neighbor_stitch_items": 2,
        "neighbor_stitch_span": 1,
        "state_part_completion_items": 0,
        "state_part_refinement": False,
    },
)
READER_CONFIGURATIONS = (
    "baseline_fixed_reader",
    "winner_fixed_reader",
    "winner_matched_context",
)
QUERY_OVERRIDE_KEYS = (
    "search_limit",
    "max_context_items",
    "max_chunks_per_trajectory",
    "neighbor_stitch_items",
    "neighbor_stitch_span",
    "state_part_completion_items",
    "state_part_refinement",
    "context_expansion_max_ratio",
)
QUERY_OVERRIDE_DEFAULTS: dict[str, int | float | bool] = {
    "state_part_completion_items": 0,
    "state_part_refinement": False,
    "context_expansion_max_ratio": 0.0,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "plan":
        plan = build_experiment_plan(
            data_root=Path(args.data_root).expanduser().resolve(),
            official_repo=Path(args.official_repo).expanduser().resolve(),
            output_root=Path(args.output_root).expanduser().resolve(),
            slice_path=Path(args.slice).expanduser().resolve(),
            api_url=args.api_url,
            tier=args.tier,
            allow_localhost=args.allow_localhost,
            query_workers=args.query_workers,
        )
        output = Path(args.output).expanduser().resolve()
        write_json(output, plan)
        print(json.dumps(plan, indent=2, sort_keys=True))  # noqa: T201
        return 0
    if args.command == "retrieve":
        return run_retrieval_command(args)
    if args.command == "gate":
        reports = parse_named_paths(args.arm)
        gate = evaluate_ablation_reports(
            reports,
            slice_record=load_json(Path(args.slice).expanduser().resolve()),
        )
        write_json(Path(args.output).expanduser().resolve(), gate)
        print(json.dumps(gate, indent=2, sort_keys=True))  # noqa: T201
        return 0
    if args.command.startswith("reader-"):
        return run_reader_cli_command(args)
    if args.command == "doctor":
        result = probe_official_loader(Path(args.official_repo).expanduser().resolve())
        print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201
        return 0
    raise RuntimeError(f"Unknown command: {args.command}")


def run_reader_cli_command(args: argparse.Namespace) -> int:
    if args.command.startswith("reader-holdout-"):
        return run_reader_holdout_cli_command(args)
    if args.command == "reader-plan":
        plan_path = Path(args.plan).expanduser().resolve()
        gate_path = Path(args.gate).expanduser().resolve()
        preregister_path = (
            Path(args.winner_preregister).expanduser().resolve()
            if args.winner_preregister
            else None
        )
        baseline_runs = parse_named_paths(args.baseline_run)
        reader_plan = build_reader_plan(
            experiment_plan=load_json(plan_path),
            gate=load_json(gate_path),
            baseline_runs=baseline_runs,
            winner_preregister=load_json(preregister_path) if preregister_path else None,
        )
        reader_plan["source_artifacts"].update(
            {
                "experiment_plan": {"path": str(plan_path), "sha256": sha256_file(plan_path)},
                "gate": {"path": str(gate_path), "sha256": sha256_file(gate_path)},
            }
        )
        if preregister_path:
            reader_plan["source_artifacts"]["winner_preregister"] = {
                "path": str(preregister_path),
                "sha256": sha256_file(preregister_path),
            }
        write_json(Path(args.output).expanduser().resolve(), reader_plan)
        print(json.dumps(reader_plan, indent=2, sort_keys=True))  # noqa: T201
        return 0
    if args.command == "reader-report":
        plan_path = Path(args.plan).expanduser().resolve()
        report = build_reader_report(
            reader_plan=load_json(plan_path),
            run_roots=parse_named_paths(args.run),
        )
        report["source_artifacts"]["reader_plan"] = {
            "path": str(plan_path),
            "sha256": sha256_file(plan_path),
        }
        write_json(Path(args.output).expanduser().resolve(), report)
        print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
        return 0
    if args.command == "reader-replication-plan":
        reader_plan_path = Path(args.reader_plan).expanduser().resolve()
        replication_plan = build_reader_replication_plan(
            reader_plan=load_json(reader_plan_path),
            reader_plan_path=reader_plan_path,
            source_run_roots=parse_named_paths(args.source_run),
            output_root=Path(args.output_root).expanduser().resolve(),
        )
        write_json(Path(args.output).expanduser().resolve(), replication_plan)
        print(json.dumps(replication_plan, indent=2, sort_keys=True))  # noqa: T201
        return 0
    if args.command == "reader-replication-run":
        result = run_reader_replication_plan(
            load_json(Path(args.plan).expanduser().resolve()),
            max_workers=args.max_workers,
        )
        print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201
        return 0 if result["status"] == "PASS" else 1
    if args.command == "reader-replication-report":
        plan_path = Path(args.plan).expanduser().resolve()
        report = build_reader_replication_report(
            plan=load_json(plan_path),
            plan_path=plan_path,
        )
        write_json(Path(args.output).expanduser().resolve(), report)
        print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
        return 0
    raise RuntimeError(f"Unknown command: {args.command}")


def run_reader_holdout_cli_command(args: argparse.Namespace) -> int:
    if args.command == "reader-holdout-plan":
        replication_plan_path = Path(args.replication_plan).expanduser().resolve()
        replication_report_path = Path(args.replication_report).expanduser().resolve()
        plan = build_reader_holdout_plan(
            replication_plan=load_json(replication_plan_path),
            replication_plan_path=replication_plan_path,
            replication_report=load_json(replication_report_path),
            replication_report_path=replication_report_path,
            output_root=Path(args.output_root).expanduser().resolve(),
        )
        write_json(Path(args.output).expanduser().resolve(), plan)
        print(json.dumps(plan, indent=2, sort_keys=True))  # noqa: T201
        return 0
    if args.command == "reader-holdout-run":
        result = run_reader_holdout_plan(
            load_json(Path(args.plan).expanduser().resolve()),
            max_workers=args.max_workers,
        )
        print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201
        return 0 if result["status"] == "PASS" else 1
    if args.command == "reader-holdout-report":
        plan_path = Path(args.plan).expanduser().resolve()
        report = build_reader_holdout_report(
            plan=load_json(plan_path),
            plan_path=plan_path,
        )
        write_json(Path(args.output).expanduser().resolve(), report)
        print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
        return 0 if report["status"] == "PASS" else 1
    raise RuntimeError(f"Unknown command: {args.command}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--data-root", required=True)
    plan.add_argument("--official-repo", required=True)
    plan.add_argument("--output-root", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--slice", default=str(DEFAULT_SLICE))
    plan.add_argument("--tier", choices=("small",), default="small")
    plan.add_argument("--api-url", default=os.getenv("SIBYL_API_URL", "http://127.0.0.1:3434/api"))
    plan.add_argument("--allow-localhost", action="store_true")
    plan.add_argument("--query-workers", type=int, default=DEFAULT_QUERY_WORKERS)

    retrieve = subparsers.add_parser("retrieve")
    retrieve.add_argument("--data-root", required=True)
    retrieve.add_argument("--official-repo", required=True)
    retrieve.add_argument("--memory-dir", required=True)
    retrieve.add_argument("--output-dir", required=True)
    retrieve.add_argument("--slice", default=str(DEFAULT_SLICE))
    retrieve.add_argument("--domain", choices=DOMAINS, required=True)
    retrieve.add_argument("--tier", choices=("small",), default="small")
    retrieve.add_argument(
        "--arm", choices=tuple(arm["name"] for arm in RETRIEVAL_ARMS), required=True
    )
    retrieve.add_argument("--query-workers", type=int, default=DEFAULT_QUERY_WORKERS)
    retrieve.add_argument("--api-timeout-seconds", type=float, default=600.0)
    retrieve.add_argument("--allow-localhost", action="store_true")
    add_retrieval_override_arguments(retrieve)

    gate = subparsers.add_parser("gate")
    gate.add_argument("--arm", action="append", required=True, metavar="NAME=REPORT")
    gate.add_argument("--slice", default=str(DEFAULT_SLICE))
    gate.add_argument("--output", required=True)

    reader = subparsers.add_parser("reader-plan")
    reader.add_argument("--plan", required=True)
    reader.add_argument("--gate", required=True)
    reader.add_argument("--winner-preregister")
    reader.add_argument(
        "--baseline-run",
        action="append",
        required=True,
        metavar="DOMAIN=OUTPUT_DIR",
    )
    reader.add_argument("--output", required=True)

    add_reader_report_arguments(subparsers)
    add_reader_replication_arguments(subparsers)
    add_reader_holdout_arguments(subparsers)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--official-repo", required=True)

    args = parser.parse_args(argv)
    validate_args(parser, args)
    return args


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if hasattr(args, "query_workers") and args.query_workers < 1:
        parser.error("--query-workers must be positive")
    if hasattr(args, "api_timeout_seconds") and args.api_timeout_seconds <= 0:
        parser.error("--api-timeout-seconds must be positive")
    if hasattr(args, "max_workers") and args.max_workers < 1:
        parser.error("--max-workers must be positive")
    for key in ("search_limit", "max_context_items", "max_chunks_per_trajectory"):
        value = getattr(args, key, None)
        if value is not None and value < 1:
            parser.error(f"--{key.replace('_', '-')} must be positive")
    for key in (
        "neighbor_stitch_items",
        "neighbor_stitch_span",
        "state_part_completion_items",
    ):
        value = getattr(args, key, None)
        if value is not None and value < 0:
            parser.error(f"--{key.replace('_', '-')} must be non-negative")
    context_expansion_max_ratio = getattr(args, "context_expansion_max_ratio", None)
    if context_expansion_max_ratio is not None and (
        not math.isfinite(context_expansion_max_ratio)
        or context_expansion_max_ratio < 0.0
        or 0.0 < context_expansion_max_ratio < 1.0
    ):
        parser.error("--context-expansion-max-ratio must be zero or at least 1.0")


def add_reader_report_arguments(subparsers: Any) -> None:
    reader_report = subparsers.add_parser("reader-report")
    reader_report.add_argument("--plan", required=True)
    reader_report.add_argument(
        "--run",
        action="append",
        required=True,
        metavar="CONFIGURATION=OUTPUT_ROOT",
    )
    reader_report.add_argument("--output", required=True)


def add_reader_replication_arguments(subparsers: Any) -> None:
    plan = subparsers.add_parser("reader-replication-plan")
    plan.add_argument("--reader-plan", required=True)
    plan.add_argument(
        "--source-run",
        action="append",
        required=True,
        metavar="CONFIGURATION=OUTPUT_ROOT",
    )
    plan.add_argument("--output-root", required=True)
    plan.add_argument("--output", required=True)

    run = subparsers.add_parser("reader-replication-run")
    run.add_argument("--plan", required=True)
    run.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)

    report = subparsers.add_parser("reader-replication-report")
    report.add_argument("--plan", required=True)
    report.add_argument("--output", required=True)


def add_reader_holdout_arguments(subparsers: Any) -> None:
    plan = subparsers.add_parser("reader-holdout-plan")
    plan.add_argument("--replication-plan", required=True)
    plan.add_argument("--replication-report", required=True)
    plan.add_argument("--output-root", required=True)
    plan.add_argument("--output", required=True)

    run = subparsers.add_parser("reader-holdout-run")
    run.add_argument("--plan", required=True)
    run.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)

    report = subparsers.add_parser("reader-holdout-report")
    report.add_argument("--plan", required=True)
    report.add_argument("--output", required=True)


def add_retrieval_override_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search-limit", type=int)
    parser.add_argument("--max-context-items", type=int)
    parser.add_argument("--max-chunks-per-trajectory", type=int)
    parser.add_argument("--neighbor-stitch-items", type=int)
    parser.add_argument("--neighbor-stitch-span", type=int)
    parser.add_argument("--state-part-completion-items", type=int)
    parser.add_argument(
        "--state-part-refinement",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument("--context-expansion-max-ratio", type=float)


def build_experiment_plan(
    *,
    data_root: Path,
    official_repo: Path,
    output_root: Path,
    slice_path: Path,
    api_url: str,
    tier: str,
    allow_localhost: bool,
    query_workers: int,
) -> dict[str, Any]:
    slice_record = load_json(slice_path)
    require_slice(slice_record)
    question_ids = question_ids_by_domain(slice_record)
    representations = []
    for representation in REPRESENTATIONS:
        memory_builds = {}
        for domain in DOMAINS:
            build_dir = output_root / "memory_builds" / representation["name"] / domain
            memory_builds[domain] = {
                "output_dir": str(build_dir),
                "memory_dir": str(build_dir / "memory_state"),
                "command": memory_build_command(
                    data_root=data_root,
                    official_repo=official_repo,
                    output_dir=build_dir,
                    domain=domain,
                    tier=tier,
                    question_ids=question_ids[domain],
                    api_url=api_url,
                    allow_localhost=allow_localhost,
                    representation=representation,
                ),
            }
        representations.append({**representation, "memory_builds": memory_builds})

    arms = []
    representation_by_name = {item["name"]: item for item in representations}
    for arm in RETRIEVAL_ARMS:
        retrieval_runs = {}
        representation = representation_by_name[arm["representation"]]
        for domain in DOMAINS:
            output_dir = output_root / "retrieval" / arm["name"] / domain
            retrieval_runs[domain] = {
                "output_dir": str(output_dir),
                "command": retrieval_command(
                    data_root=data_root,
                    official_repo=official_repo,
                    memory_dir=Path(representation["memory_builds"][domain]["memory_dir"]),
                    output_dir=output_dir,
                    slice_path=slice_path,
                    domain=domain,
                    tier=tier,
                    arm=arm,
                    allow_localhost=allow_localhost,
                    query_workers=query_workers,
                ),
            }
        diagnostic_dir = output_root / "diagnostics" / arm["name"]
        arms.append(
            {
                **arm,
                "retrieval_runs": retrieval_runs,
                "diagnostic_output_dir": str(diagnostic_dir),
                "diagnostic_command": diagnostic_command(
                    data_root=data_root,
                    slice_path=slice_path,
                    output_dir=diagnostic_dir,
                    retrieval_runs=retrieval_runs,
                ),
            }
        )

    plan = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "runner_provenance": git_provenance(ROOT),
        "official_repo": str(official_repo),
        "official_repo_provenance": git_provenance(official_repo),
        "dataset": {
            "data_root": str(data_root),
            "tier": tier,
            "questions_sha256": sha256_file(data_root / "questions.jsonl"),
            "trajectories_sha256": sha256_file(data_root / "trajectories.jsonl"),
            "haystack_sha256": sha256_file(haystack_path(data_root, tier)),
        },
        "slice": {
            "path": str(slice_path),
            "sha256": sha256_file(slice_path),
            "record_sha256": sha256_json(slice_record),
            "question_ids_by_domain": question_ids,
        },
        "output_root": str(output_root),
        "api_url": api_url,
        "representations": representations,
        "retrieval_arms": arms,
        "promotion_gate": deepcopy(slice_record["decision_thresholds"]),
        "reader_phase": {
            "fixed_model": "Qwen/Qwen3.5-9B",
            "fixed_generation": {"temperature": 0.6, "top_p": 0.95, "top_k": 20},
            "configurations": list(READER_CONFIGURATIONS),
            "requires_gate_decision": "GO",
            "matched_context_source": "observed median baseline memory_context_token_count",
        },
        "cost_accounting": {
            "ingest": "memory_state/memory_manifest.json:ingest_embedding_usage",
            "retrieval": "retrieval_summary.json:embedding_accounting",
            "reader": "provider_usage/reader.jsonl",
            "judge": "provider_usage/judge.jsonl",
            "basis": "provider-reported usage and cost only",
        },
        "integrity_contract": {
            "memory_inputs": ["question", "optional image"],
            "retrieval_rows_contain_answers": False,
            "question_ids_visible_to_memory": False,
            "reader_prompts_changed": False,
            "scoring_changed": False,
        },
    }
    baseline_arm = next(arm for arm in arms if arm["name"] == BASELINE_ARM)
    plan["reader_phase"]["baseline_commands"] = [
        {
            "configuration": READER_CONFIGURATIONS[0],
            "domain": domain,
            "command": reader_command(
                experiment_plan=plan,
                arm=baseline_arm,
                domain=domain,
                configuration=READER_CONFIGURATIONS[0],
                memory_context_max_tokens=DEFAULT_STANDARD_CONTEXT_TOKENS,
            ),
        }
        for domain in DOMAINS
    ]
    return plan


def run_retrieval_command(args: argparse.Namespace) -> int:
    data_root = Path(args.data_root).expanduser().resolve()
    official_repo = Path(args.official_repo).expanduser().resolve()
    memory_dir = Path(args.memory_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    arm = retrieval_arm_from_args(args)
    questions = load_safe_questions(
        data_root / "questions.jsonl",
        selected_ids=set(question_ids_by_domain(load_json(Path(args.slice)))[args.domain]),
        domain=args.domain,
        data_root=data_root,
    )
    haystack = load_json(haystack_path(data_root, args.tier))
    selected_haystack = {item["id"]: haystack[item["id"]] for item in questions}
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    write_json(runtime_dir / "questions.json", questions)
    write_json(runtime_dir / "haystack.json", selected_haystack)

    run_identity = {
        "schema_version": SCHEMA_VERSION,
        "domain": args.domain,
        "tier": args.tier,
        "arm": arm,
        "question_ids": [item["id"] for item in questions],
        "memory_artifact_sha256": memory_artifact_sha256(memory_dir),
    }
    run_provenance = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "official_repo_provenance": git_provenance(official_repo),
        "runner_provenance": git_provenance(ROOT),
    }
    ensure_run_identity(
        output_dir / "retrieval_run.json",
        identity=run_identity,
        provenance=run_provenance,
    )
    memory = load_saved_memory(
        official_repo=official_repo,
        memory_dir=memory_dir,
        arm=arm,
        allow_localhost=args.allow_localhost,
        api_timeout_seconds=args.api_timeout_seconds,
    )
    try:
        records = execute_retrieval(
            memory,
            questions,
            output_path=output_dir / "per_question.jsonl",
            max_workers=args.query_workers,
        )
    finally:
        close = getattr(memory, "close", None)
        if callable(close):
            close()
    write_json(
        output_dir / "retrieval_summary.json",
        {
            **run_identity,
            "provenance": run_provenance,
            "completed_at": datetime.now(UTC).isoformat(),
            "question_count": len(records),
            "completed_question_ids": [record["question_id"] for record in records],
            **summarize_retrieval_accounting(records),
        },
    )
    return 0


def retrieval_arm_from_args(args: argparse.Namespace) -> dict[str, Any]:
    arm = dict(arm_by_name(args.arm))
    for key in QUERY_OVERRIDE_KEYS:
        value = getattr(args, key, None)
        if value is not None:
            arm[key] = value
    return normalize_reader_arm(arm)


def execute_retrieval(
    memory: Any,
    questions: list[dict[str, Any]],
    *,
    output_path: Path,
    max_workers: int,
) -> list[dict[str, Any]]:
    existing = {row["question_id"]: row for row in load_jsonl_if_exists(output_path)}
    missing = [item for item in questions if item["id"] not in existing]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_question = {
            executor.submit(query_one, memory, question): question for question in missing
        }
        for future in as_completed(future_to_question):
            record = future.result()
            append_jsonl_fsync(output_path, record)
            existing[record["question_id"]] = record
    selected_ids = {item["id"] for item in questions}
    records = [existing[question_id] for question_id in sorted(selected_ids)]
    write_jsonl_atomic(output_path, records)
    return records


def query_one(memory: Any, question: dict[str, Any]) -> dict[str, Any]:
    question_text = str(question["question"])
    question_image = question.get("image")
    memory.set_query_context(question_item={"question": question_text, "image": question_image})
    try:
        started = time.perf_counter()
        memory_context = memory.query(question_text, query_image=question_image)
        query_duration = time.perf_counter() - started
        post_started = time.perf_counter()
        post_metadata = memory.post_query_hook(
            query=question_text,
            query_image=question_image,
            memory_context=memory_context,
        )
        post_duration = time.perf_counter() - post_started
    finally:
        memory.clear_query_context()
    return {
        "question_id": question["id"],
        "question_type": question["question_type"],
        "memory_context": memory_context,
        "memory_query_duration_seconds": query_duration,
        "memory_post_query_duration_seconds": post_duration,
        "memory_post_query_metadata": post_metadata,
    }


def load_saved_memory(
    *,
    official_repo: Path,
    memory_dir: Path,
    arm: dict[str, Any],
    allow_localhost: bool,
    api_timeout_seconds: float,
) -> Any:
    if not (official_repo / "memory_modules" / "memory.py").is_file():
        raise RuntimeError(f"Invalid official repository: {official_repo}")
    sys.path.insert(0, str(official_repo))
    importlib.import_module("benchmarks.longmemeval_v2_memory.sibyl_memory")
    memory_module = importlib.import_module("memory_modules.memory")
    requested_config = load_json(memory_dir / "memory_config.json")
    params = requested_config.get("memory_params")
    if not isinstance(params, dict):
        raise TypeError("Saved memory config is missing memory_params")
    params = dict(params)
    params.update({key: arm[key] for key in QUERY_OVERRIDE_KEYS})
    params["allow_localhost"] = allow_localhost
    params["api_timeout_seconds"] = api_timeout_seconds
    requested_config["memory_params"] = params
    return memory_module.load_memory(memory_dir, requested_config=requested_config)


def probe_official_loader(official_repo: Path) -> dict[str, Any]:
    if not (official_repo / "memory_modules" / "memory.py").is_file():
        raise RuntimeError(f"Invalid official repository: {official_repo}")
    sys.path.insert(0, str(official_repo))
    importlib.import_module("benchmarks.longmemeval_v2_memory.sibyl_memory")
    memory_module = importlib.import_module("memory_modules.memory")
    registered = sorted(memory_module.MEMORY_TYPES)
    if "sibyl_live_api" not in registered:
        raise RuntimeError("Sibyl memory adapter was not registered with the official loader")
    return {
        "official_repo": str(official_repo),
        "registered_memory_types": registered,
        "sibyl_live_api_registered": True,
    }


def evaluate_ablation_reports(
    report_paths: dict[str, Path],
    *,
    slice_record: dict[str, Any],
) -> dict[str, Any]:
    expected_names = {arm["name"] for arm in RETRIEVAL_ARMS}
    if set(report_paths) != expected_names:
        missing = sorted(expected_names - set(report_paths))
        extra = sorted(set(report_paths) - expected_names)
        raise ValueError(
            f"Ablation reports must contain exactly five arms; missing={missing}, extra={extra}"
        )
    reports = {name: load_json(path) for name, path in report_paths.items()}
    baseline = reports[BASELINE_ARM]
    baseline_metrics = diagnostic_metrics(baseline)
    baseline_shape = diagnostic_shape(baseline)
    comparisons = []
    for arm in RETRIEVAL_ARMS:
        name = arm["name"]
        report = reports[name]
        if diagnostic_shape(report) != baseline_shape:
            raise ValueError(f"Diagnostic sample shape differs for arm {name!r}")
        metrics = diagnostic_metrics(report)
        comparisons.append(
            {
                "name": name,
                "representation": arm["representation"],
                "metrics": metrics,
                "exact_context_recall_gain": (
                    metrics["exact_context_recall_at_10"]
                    - baseline_metrics["exact_context_recall_at_10"]
                ),
                "multi_state_evidence_coverage_gain": (
                    metrics["multi_state_evidence_coverage_at_10"]
                    - baseline_metrics["multi_state_evidence_coverage_at_10"]
                ),
                "report_path": str(report_paths[name]),
            }
        )
    candidates = [item for item in comparisons if item["name"] != BASELINE_ARM]
    reporting_winner = max(
        candidates,
        key=lambda item: (
            item["exact_context_recall_gain"],
            item["multi_state_evidence_coverage_gain"],
            item["name"],
        ),
    )
    thresholds = slice_record["decision_thresholds"]
    selection_rule = thresholds.get("selection_rule")
    if selection_rule != "any_arm_meets_both":
        raise ValueError(f"Unsupported ablation selection rule: {selection_rule!r}")
    go = thresholds["go"]
    no_go = thresholds["no_go"]
    qualifying = [
        item
        for item in candidates
        if item["exact_context_recall_gain"] >= go["exact_context_recall_at_10_absolute_gain"]
        and item["multi_state_evidence_coverage_gain"]
        >= go["multi_state_evidence_coverage_at_10_absolute_gain"]
    ]
    if qualifying:
        decision = "GO"
        winner = max(
            qualifying,
            key=lambda item: (
                item["exact_context_recall_gain"],
                item["multi_state_evidence_coverage_gain"],
                item["name"],
            ),
        )
    elif all(
        item["exact_context_recall_gain"] < no_go["all_arms_exact_context_recall_gain_below"]
        for item in candidates
    ):
        decision = "NO-GO"
        winner = reporting_winner
    else:
        decision = "RESEARCH-MORE"
        winner = reporting_winner
    return {
        "schema_version": GATE_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "decision": decision,
        "reader_phase_allowed": decision == "GO",
        "selection_rule": selection_rule,
        "baseline_arm": BASELINE_ARM,
        "winner_arm": winner["name"],
        "thresholds": deepcopy(thresholds),
        "comparisons": comparisons,
    }


def build_reader_plan(
    *,
    experiment_plan: dict[str, Any],
    gate: dict[str, Any],
    baseline_runs: dict[str, Path],
    winner_preregister: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if experiment_plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Invalid ablation experiment plan")
    if gate.get("decision") != "GO":
        raise ValueError("Reader plan requires a GO ablation gate")
    if set(baseline_runs) != set(DOMAINS):
        raise ValueError("Baseline reader runs must include web and enterprise")
    matched_context_tokens = baseline_context_token_median(baseline_runs)
    arm_by_plan_name = {arm["name"]: arm for arm in experiment_plan["retrieval_arms"]}
    baseline_arm = normalize_reader_arm(arm_by_plan_name[BASELINE_ARM])
    winner_arm, winner_source = resolve_reader_winner_arm(
        experiment_plan=experiment_plan,
        gate=gate,
        arm_by_plan_name=arm_by_plan_name,
        winner_preregister=winner_preregister,
    )
    configs = (
        (READER_CONFIGURATIONS[0], baseline_arm, DEFAULT_STANDARD_CONTEXT_TOKENS),
        (READER_CONFIGURATIONS[1], winner_arm, DEFAULT_STANDARD_CONTEXT_TOKENS),
        (READER_CONFIGURATIONS[2], winner_arm, matched_context_tokens),
    )
    commands = []
    for config_name, arm, context_tokens in configs:
        for domain in DOMAINS:
            commands.append(
                {
                    "configuration": config_name,
                    "domain": domain,
                    "command": reader_command(
                        experiment_plan=experiment_plan,
                        arm=arm,
                        domain=domain,
                        configuration=config_name,
                        memory_context_max_tokens=context_tokens,
                    ),
                }
            )
    return {
        "schema_version": READER_PLAN_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "gate_decision": "GO",
        "winner_arm": winner_arm["name"],
        "winner_arm_config": winner_arm,
        "winner_source": winner_source,
        "matched_context_tokens": matched_context_tokens,
        "configurations": list(READER_CONFIGURATIONS),
        "commands": commands,
        "source_artifacts": {
            "baseline_runs": {
                domain: {
                    "path": str(run_dir),
                    "per_question_path": str(run_dir / "per_question.jsonl"),
                    "per_question_sha256": sha256_file(run_dir / "per_question.jsonl"),
                }
                for domain, run_dir in sorted(baseline_runs.items())
            }
        },
    }


def resolve_reader_winner_arm(
    *,
    experiment_plan: dict[str, Any],
    gate: dict[str, Any],
    arm_by_plan_name: dict[str, dict[str, Any]],
    winner_preregister: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    gate_schema = gate.get("schema_version")
    if gate_schema == GATE_SCHEMA_VERSION:
        winner_name = str(gate["winner_arm"])
        if winner_name not in arm_by_plan_name:
            raise ValueError(f"Unknown reader winner arm: {winner_name}")
        return normalize_reader_arm(arm_by_plan_name[winner_name]), {
            "kind": "named_ablation_arm",
            "gate_schema_version": gate_schema,
        }
    if gate_schema != TREATMENT_GATE_SCHEMA_VERSION:
        raise ValueError("Invalid ablation gate")
    return resolve_treatment_reader_arm(
        experiment_plan=experiment_plan,
        gate=gate,
        winner_preregister=winner_preregister,
    )


def resolve_treatment_reader_arm(
    *,
    experiment_plan: dict[str, Any],
    gate: dict[str, Any],
    winner_preregister: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if gate.get("reader_phase_allowed") is not True:
        raise ValueError("Treatment gate does not allow reader evaluation")
    if winner_preregister is None:
        raise ValueError("Treatment gate requires --winner-preregister")
    if winner_preregister.get("schema_version") != TREATMENT_SCHEMA_VERSION:
        raise ValueError("Invalid treatment preregistration")
    treatment = winner_preregister.get("treatment")
    replay = winner_preregister.get("frozen_replay")
    if not isinstance(treatment, dict) or not isinstance(replay, dict):
        raise TypeError("Treatment preregistration is incomplete")
    treatment_name = str(treatment.get("name") or "")
    if not treatment_name or gate.get("treatment") != treatment_name:
        raise ValueError("Treatment gate and preregistration disagree")
    preregister_record_sha256 = sha256_json(winner_preregister)
    if gate.get("treatment_preregister_record_sha256") != preregister_record_sha256:
        raise ValueError("Treatment gate does not bind this preregistration")
    plan_slice_hashes = experiment_slice_hashes(experiment_plan)
    replay_slice_sha256 = replay.get("slice_sha256")
    if replay_slice_sha256 not in plan_slice_hashes:
        raise ValueError("Treatment and experiment plan use different frozen slices")
    arm = {
        "name": treatment_name,
        "representation": replay.get("memory_representation"),
        **{key: replay.get(key, QUERY_OVERRIDE_DEFAULTS.get(key)) for key in QUERY_OVERRIDE_KEYS},
    }
    normalized = normalize_reader_arm(arm)
    gate_arm = gate.get("winner_arm_config")
    if not isinstance(gate_arm, dict):
        raise TypeError("Treatment gate winner arm is incomplete")
    if normalize_reader_arm(gate_arm) != normalized:
        raise ValueError("Treatment gate and preregistration configurations disagree")
    representations = {item["name"] for item in experiment_plan["representations"]}
    if normalized["representation"] not in representations:
        raise ValueError("Treatment uses an unknown memory representation")
    return normalized, {
        "kind": "preregistered_treatment",
        "gate_schema_version": TREATMENT_GATE_SCHEMA_VERSION,
        "treatment_schema_version": TREATMENT_SCHEMA_VERSION,
        "treatment_preregister_record_sha256": preregister_record_sha256,
        "slice_sha256": replay_slice_sha256,
    }


def experiment_slice_hashes(experiment_plan: dict[str, Any]) -> set[str]:
    slice_plan = experiment_plan.get("slice")
    if not isinstance(slice_plan, dict):
        raise TypeError("Experiment plan slice is incomplete")
    hashes = {
        value
        for key in ("sha256", "record_sha256")
        if isinstance((value := slice_plan.get(key)), str) and value
    }
    slice_path_value = slice_plan.get("path")
    if not isinstance(slice_path_value, str) or not slice_path_value:
        raise ValueError("Experiment plan slice path is missing")
    slice_path = Path(slice_path_value)
    if slice_path.is_file():
        file_sha256 = sha256_file(slice_path)
        if slice_plan.get("sha256") != file_sha256:
            raise ValueError("Experiment plan frozen slice file has changed")
        record_sha256 = sha256_json(load_json(slice_path))
        recorded_record_sha256 = slice_plan.get("record_sha256")
        if recorded_record_sha256 is not None and recorded_record_sha256 != record_sha256:
            raise ValueError("Experiment plan frozen slice record has changed")
        hashes.add(record_sha256)
    return hashes


def normalize_reader_arm(raw_arm: dict[str, Any]) -> dict[str, Any]:
    arm = dict(raw_arm)
    for key in QUERY_OVERRIDE_KEYS:
        if key not in arm and key in QUERY_OVERRIDE_DEFAULTS:
            arm[key] = QUERY_OVERRIDE_DEFAULTS[key]
        if key not in arm:
            raise ValueError(f"Reader arm is missing {key}")
    for key in ("search_limit", "max_context_items", "max_chunks_per_trajectory"):
        if type(arm[key]) is not int or arm[key] < 1:
            raise ValueError(f"Reader arm {key} must be a positive integer")
    for key in ("neighbor_stitch_items", "neighbor_stitch_span", "state_part_completion_items"):
        if type(arm[key]) is not int or arm[key] < 0:
            raise ValueError(f"Reader arm {key} must be a non-negative integer")
    if not isinstance(arm["state_part_refinement"], bool):
        raise TypeError("Reader arm state_part_refinement must be boolean")
    context_expansion_max_ratio = arm["context_expansion_max_ratio"]
    if isinstance(context_expansion_max_ratio, bool) or not isinstance(
        context_expansion_max_ratio, int | float
    ):
        raise TypeError("Reader arm context_expansion_max_ratio must be numeric")
    normalized_expansion_ratio = float(context_expansion_max_ratio)
    if (
        not math.isfinite(normalized_expansion_ratio)
        or normalized_expansion_ratio < 0.0
        or 0.0 < normalized_expansion_ratio < 1.0
    ):
        raise ValueError("Reader arm context_expansion_max_ratio must be zero or at least 1.0")
    arm["context_expansion_max_ratio"] = normalized_expansion_ratio
    if not isinstance(arm.get("name"), str) or not arm["name"]:
        raise ValueError("Reader arm must have a name")
    if not isinstance(arm.get("representation"), str) or not arm["representation"]:
        raise ValueError("Reader arm must have a representation")
    return arm


def memory_build_command(
    *,
    data_root: Path,
    official_repo: Path,
    output_dir: Path,
    domain: str,
    tier: str,
    question_ids: list[str],
    api_url: str,
    allow_localhost: bool,
    representation: dict[str, Any],
) -> list[str]:
    command = [
        "moon",
        "run",
        "root:bench-longmemeval-v2-official-full",
        "--",
        "--official-repo",
        str(official_repo),
        "--data-root",
        str(data_root),
        "--domain",
        domain,
        "--tier",
        tier,
        "--question-ids",
        *question_ids,
        "--output-dir",
        str(output_dir),
        "--checkpoint-dir",
        str(output_dir / "ingest_checkpoint"),
        "--api-url",
        api_url,
        "--save-memory",
        "--skip-evaluation",
        "--chunking-mode",
        representation["chunking_mode"],
        "--content-max-chars",
        str(representation["content_max_chars"]),
        "--max-chunks-per-trajectory",
        "8",
        "--neighbor-stitch-items",
        "0",
        "--neighbor-stitch-span",
        "0",
    ]
    if allow_localhost:
        command.append("--allow-localhost")
    return command


def retrieval_command(
    *,
    data_root: Path,
    official_repo: Path,
    memory_dir: Path,
    output_dir: Path,
    slice_path: Path,
    domain: str,
    tier: str,
    arm: dict[str, Any],
    allow_localhost: bool,
    query_workers: int,
) -> list[str]:
    command = [
        "moon",
        "run",
        "root:bench-longmemeval-v2-ablations",
        "--",
        "retrieve",
        "--data-root",
        str(data_root),
        "--official-repo",
        str(official_repo),
        "--memory-dir",
        str(memory_dir),
        "--output-dir",
        str(output_dir),
        "--slice",
        str(slice_path),
        "--domain",
        domain,
        "--tier",
        tier,
        "--arm",
        arm["name"],
        "--query-workers",
        str(query_workers),
    ]
    if allow_localhost:
        command.append("--allow-localhost")
    return command


def diagnostic_command(
    *,
    data_root: Path,
    slice_path: Path,
    output_dir: Path,
    retrieval_runs: dict[str, dict[str, Any]],
) -> list[str]:
    return [
        "moon",
        "run",
        "root:bench-longmemeval-v2-diagnostics",
        "--",
        "--data-root",
        str(data_root),
        "--run",
        f"web={retrieval_runs['web']['output_dir']}",
        "--run",
        f"enterprise={retrieval_runs['enterprise']['output_dir']}",
        "--output-dir",
        str(output_dir),
        "--slice",
        str(slice_path),
        "--slice-output",
        str(output_dir / "slice.json"),
    ]


def reader_command(
    *,
    experiment_plan: dict[str, Any],
    arm: dict[str, Any],
    domain: str,
    configuration: str,
    memory_context_max_tokens: int,
) -> list[str]:
    arm = normalize_reader_arm(arm)
    representation = next(
        item for item in experiment_plan["representations"] if item["name"] == arm["representation"]
    )
    memory_dir = representation["memory_builds"][domain]["memory_dir"]
    output_dir = Path(experiment_plan["output_root"]) / "reader" / configuration / domain
    command = [
        "moon",
        "run",
        "root:bench-longmemeval-v2-official-full",
        "--",
        "--official-repo",
        experiment_plan["official_repo"],
        "--data-root",
        experiment_plan["dataset"]["data_root"],
        "--domain",
        domain,
        "--tier",
        experiment_plan["dataset"]["tier"],
        "--output-dir",
        str(output_dir),
        "--load-memory-dir",
        memory_dir,
        "--reader-model",
        experiment_plan["reader_phase"]["fixed_model"],
        "--reader-temperature",
        str(experiment_plan["reader_phase"]["fixed_generation"]["temperature"]),
        "--reader-top-p",
        str(experiment_plan["reader_phase"]["fixed_generation"]["top_p"]),
        "--reader-top-k",
        str(experiment_plan["reader_phase"]["fixed_generation"]["top_k"]),
        "--memory-context-max-tokens",
        str(memory_context_max_tokens),
        "--question-ids",
        *experiment_plan["slice"]["question_ids_by_domain"][domain],
    ]
    for key in QUERY_OVERRIDE_KEYS:
        flag = f"--{key.replace('_', '-')}"
        value = arm[key]
        if isinstance(value, bool):
            command.append(flag if value else f"--no-{flag.removeprefix('--')}")
        else:
            command.extend((flag, str(value)))
    if experiment_plan["api_url"].startswith(("http://127.0.0.1", "http://localhost")):
        command.append("--allow-localhost")
    return command


def load_safe_questions(
    path: Path,
    *,
    selected_ids: set[str],
    domain: str,
    data_root: Path,
) -> list[dict[str, Any]]:
    selected = []
    for raw in load_jsonl(path):
        question_id = str(raw.get("id") or "")
        if question_id not in selected_ids or raw.get("domain") != domain:
            continue
        question = raw.get("question")
        image = raw.get("image")
        if isinstance(question, dict):
            image = question.get("image") or image
            question = question.get("text")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Question {question_id!r} has no text")
        selected.append(
            {
                "id": question_id,
                "domain": domain,
                "question_type": str(raw.get("question_type") or "unknown"),
                "question": question,
                "image": str((data_root / str(image)).resolve()) if image else None,
            }
        )
    found = {item["id"] for item in selected}
    if missing := selected_ids - found:
        raise ValueError(f"Frozen slice questions missing for {domain}: {sorted(missing)}")
    return sorted(selected, key=lambda item: item["id"])


def baseline_context_token_median(runs: dict[str, Path]) -> int:
    values = []
    for domain in DOMAINS:
        for row in load_jsonl(runs[domain] / "per_question.jsonl"):
            value = row.get("memory_context_token_count")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                values.append(value)
    if not values:
        raise ValueError("Baseline reader runs contain no memory_context_token_count values")
    return max(1, round(statistics.median(values)))


def summarize_retrieval_accounting(records: list[dict[str, Any]]) -> dict[str, Any]:
    ingest_records = []
    query_records = []
    query_durations = []
    for record in records:
        post_metadata = record.get("memory_post_query_metadata")
        if not isinstance(post_metadata, dict):
            continue
        ingest = post_metadata.get("ingest_embedding_usage")
        if isinstance(ingest, dict) and ingest and not ingest_records:
            ingest_records.append(ingest)
        search_metadata = post_metadata.get("search_metadata")
        if isinstance(search_metadata, dict):
            query = search_metadata.get("embedding_usage")
            if isinstance(query, dict) and query:
                query_records.append(query)
        duration = record.get("memory_query_duration_seconds")
        if isinstance(duration, int | float) and not isinstance(duration, bool):
            query_durations.append(float(duration))
    ingest_summary = summarize_embedding_usage(ingest_records)
    query_summary = summarize_embedding_usage(query_records)
    requests = ingest_summary["requests"] + query_summary["requests"]
    priced_requests = (
        ingest_summary["cost_reported_requests"] + query_summary["cost_reported_requests"]
    )
    return {
        "memory_query_duration_seconds": {
            "average": statistics.fmean(query_durations) if query_durations else None,
            "median": statistics.median(query_durations) if query_durations else None,
            "maximum": max(query_durations) if query_durations else None,
        },
        "embedding_accounting": {
            "requests": requests,
            "priced_requests": priced_requests,
            "provider_reported_cost_usd": (
                ingest_summary["provider_reported_cost_usd"]
                + query_summary["provider_reported_cost_usd"]
            ),
            "cost_coverage_complete": bool(requests) and priced_requests == requests,
            "ingest": ingest_summary,
            "query": query_summary,
        },
    }


def summarize_embedding_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "requests": int(sum(numeric(record.get("requests")) or 0 for record in records)),
        "inputs": int(sum(numeric(record.get("inputs")) or 0 for record in records)),
        "input_tokens": sum(
            numeric(record.get("prompt_tokens")) or numeric(record.get("total_tokens")) or 0.0
            for record in records
        ),
        "cost_reported_requests": int(
            sum(numeric(record.get("cost_reported_requests")) or 0 for record in records)
        ),
        "provider_reported_cost_usd": sum(
            numeric(record.get("cost_usd")) or 0.0 for record in records
        ),
        "providers": sorted(
            {
                provider
                for record in records
                if isinstance(provider := record.get("provider"), str) and provider
            }
        ),
        "models": sorted(
            {model for record in records if isinstance(model := record.get("model"), str) and model}
        ),
    }


def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def diagnostic_metrics(report: dict[str, Any]) -> dict[str, float]:
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise TypeError("Diagnostic report is missing metrics")
    names = (
        "exact_context_recall_at_10",
        "multi_state_evidence_coverage_at_10",
    )
    parsed = {}
    for name in names:
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"Diagnostic metric {name!r} is missing")
        parsed[name] = float(value)
    return parsed


def diagnostic_shape(report: dict[str, Any]) -> tuple[int, int, int]:
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise TypeError("Diagnostic report is missing metrics")
    values = (
        report.get("question_count"),
        metrics.get("eligible_count"),
        metrics.get("multi_state_eligible_count"),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("Diagnostic report is missing sample counts")
    return values


def question_ids_by_domain(slice_record: dict[str, Any]) -> dict[str, list[str]]:
    require_slice(slice_record)
    result = {domain: [] for domain in DOMAINS}
    for case in slice_record["cases"]:
        domain = str(case.get("domain") or "")
        question_id = str(case.get("question_id") or "")
        if domain not in result or not question_id:
            raise ValueError("Frozen slice contains an invalid case")
        result[domain].append(question_id)
    if any(not result[domain] for domain in DOMAINS):
        raise ValueError("Frozen slice must contain both domains")
    return {domain: sorted(ids) for domain, ids in result.items()}


def require_slice(slice_record: dict[str, Any]) -> None:
    if slice_record.get("schema_version") != SLICE_SCHEMA_VERSION:
        raise ValueError(f"Frozen slice schema must be {SLICE_SCHEMA_VERSION!r}")
    if not isinstance(slice_record.get("cases"), list):
        raise TypeError("Frozen slice is missing cases")
    if not isinstance(slice_record.get("decision_thresholds"), dict):
        raise TypeError("Frozen slice is missing decision_thresholds")


def arm_by_name(name: str) -> dict[str, Any]:
    return next(dict(arm) for arm in RETRIEVAL_ARMS if arm["name"] == name)


def parse_named_paths(values: list[str]) -> dict[str, Path]:
    parsed = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name.strip() or not raw_path.strip():
            raise ValueError(f"Expected NAME=PATH, got {value!r}")
        if name in parsed:
            raise ValueError(f"Duplicate path name {name!r}")
        parsed[name] = Path(raw_path).expanduser().resolve()
    return parsed


def ensure_run_identity(
    path: Path,
    *,
    identity: dict[str, Any],
    provenance: dict[str, Any],
) -> None:
    if path.exists():
        loaded = load_json(path)
        existing_identity = loaded.get("identity")
        if not isinstance(existing_identity, dict):
            existing_identity = {key: loaded.get(key) for key in identity}
        if existing_identity != identity:
            raise RuntimeError(f"Refusing to resume retrieval with changed identity: {path}")
        attempts = loaded.get("provenance_attempts")
        provenance_attempts = list(attempts) if isinstance(attempts, list) else []
    else:
        provenance_attempts = []
    provenance_attempts.append(provenance)
    write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "identity": identity,
            "provenance_attempts": provenance_attempts,
        },
    )


def memory_artifact_sha256(memory_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in ("memory_config.json", "chunk_catalog.jsonl.gz", "memory_manifest.json"):
        path = memory_dir / name
        if not path.is_file():
            raise RuntimeError(f"Saved memory artifact is missing {path}")
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def haystack_path(data_root: Path, tier: str) -> Path:
    nested = data_root / "haystacks" / f"lme_v2_{tier}.json"
    return nested if nested.exists() else data_root / f"lme_v2_{tier}.json"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"Expected JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def load_jsonl_if_exists(path: Path) -> list[dict[str, Any]]:
    return load_jsonl(path) if path.exists() else []


def append_jsonl_fsync(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())
