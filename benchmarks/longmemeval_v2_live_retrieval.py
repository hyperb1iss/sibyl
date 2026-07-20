#!/usr/bin/env python3
"""Query an existing LongMemEval-V2 Sibyl project without rebuilding memory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

BENCHMARKS_ROOT = Path(__file__).resolve().parent
if str(BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS_ROOT))

from longmemeval_v2_memory.sibyl_memory import (  # noqa: E402
    DEFAULT_EVIDENCE_COMPOSITION_MODE,
    EVIDENCE_COMPOSITION_MODES,
    SibylLiveApiMemory,
    load_api_credentials_file,
)

from sibyl_core.retrieval.refinement import MAX_REFINEMENT_QUERIES  # noqa: E402

RUN_SCHEMA_VERSION = "sibyl-longmemeval-v2-live-retrieval-v1"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    questions_path = Path(args.questions).expanduser().resolve()
    haystack_path = Path(args.haystack).expanduser().resolve()
    trajectories_path = Path(args.trajectories).expanduser().resolve()
    question_ids_path = Path(args.question_ids_file).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    token_path = Path(args.api_token_file).expanduser().resolve()

    questions_by_id = load_jsonl_by_id(questions_path)
    question_ids = load_question_ids(question_ids_path)
    questions = select_questions(questions_by_id, question_ids, domain=args.domain)
    haystack = load_haystack_subset(haystack_path, question_ids)
    expected_trajectory_ids = {
        trajectory_id for trajectory_ids in haystack.values() for trajectory_id in trajectory_ids
    }
    trajectories = load_trajectory_subset(trajectories_path, expected_trajectory_ids)
    credentials = load_api_credentials_file(token_path)

    memory = SibylLiveApiMemory.attach_existing(
        {
            "api_url": args.api_url,
            **credentials,
            "allow_localhost": args.allow_localhost,
            "project_id": args.project_id,
            "run_id": args.run_id,
            "content_max_chars": args.content_max_chars,
            "chunking_mode": args.chunking_mode,
            "api_timeout_seconds": args.timeout_seconds,
            "search_limit": args.search_limit,
            "max_context_items": args.max_context_items,
            "max_context_chars_per_item": args.max_context_chars_per_item,
            "max_context_total_chars": args.max_context_total_chars,
            "retrieval_mode": args.retrieval_mode,
            "retrieval_max_planned_queries": args.max_planned_queries,
            "evidence_composition_mode": args.evidence_composition_mode,
            "source_evidence_bundling": args.source_evidence_bundling,
            "typed_stream_retrieval": args.typed_stream_retrieval,
            "typed_stream_limit": args.typed_stream_limit,
        },
        expected_trajectory_ids=expected_trajectory_ids,
        trajectories=trajectories,
    )
    try:
        run_config = build_run_config(
            args,
            question_ids=question_ids,
            questions_path=questions_path,
            haystack_path=haystack_path,
            api_runtime=memory.api_runtime,
        )
        completed = prepare_output(
            output_dir,
            run_config=run_config,
            questions=questions,
            haystack=haystack,
            resume=args.resume,
        )
        summary = run_queries(
            memory,
            questions=questions,
            haystack=haystack,
            output_dir=output_dir,
            completed_question_ids=completed,
        )
        write_json(output_dir / "retrieval_run.json", summary)
        print(json.dumps(summary, indent=2, sort_keys=True))  # noqa: T201
        return 0
    finally:
        memory._client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:3434/api")
    parser.add_argument("--api-token-file", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--haystack", required=True)
    parser.add_argument("--trajectories", required=True)
    parser.add_argument("--question-ids-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--domain", default="")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-localhost", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--content-max-chars", type=int, default=18_000)
    parser.add_argument("--chunking-mode", choices=("state", "trajectory"), default="state")
    parser.add_argument("--search-limit", type=int, default=12)
    parser.add_argument("--max-context-items", type=int, default=8)
    parser.add_argument("--max-context-chars-per-item", type=int, default=12_000)
    parser.add_argument("--max-context-total-chars", type=int, default=60_000)
    parser.add_argument("--retrieval-mode", choices=("fast", "accurate"), default="accurate")
    parser.add_argument("--max-planned-queries", type=int, default=3)
    parser.add_argument(
        "--evidence-composition-mode",
        choices=sorted(EVIDENCE_COMPOSITION_MODES),
        default=DEFAULT_EVIDENCE_COMPOSITION_MODE,
    )
    parser.add_argument(
        "--source-evidence-bundling",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--typed-stream-retrieval", action="store_true")
    parser.add_argument("--typed-stream-limit", type=int, default=8)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.search_limit <= 0 or args.max_context_items <= 0:
        parser.error("search and context limits must be positive")
    if (
        args.content_max_chars <= 0
        or args.max_context_chars_per_item <= 0
        or args.max_context_total_chars <= 0
    ):
        parser.error("context character limits must be positive")
    if not 1 <= args.max_planned_queries <= MAX_REFINEMENT_QUERIES:
        parser.error(f"--max-planned-queries must be between 1 and {MAX_REFINEMENT_QUERIES}")
    return args


def load_jsonl_by_id(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on {path}:{line_number}") from exc
            record_id = str(record.get("id") or "") if isinstance(record, dict) else ""
            if not record_id:
                raise ValueError(f"Missing id on {path}:{line_number}")
            records[record_id] = record
    return records


def load_question_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("question ids file must contain a non-empty JSON list")
    question_ids = [str(value).strip() for value in payload]
    if any(not value for value in question_ids):
        raise ValueError("question ids must be non-empty strings")
    if len(question_ids) != len(set(question_ids)):
        raise ValueError("question ids must be unique")
    return question_ids


def select_questions(
    questions_by_id: dict[str, dict[str, Any]],
    question_ids: list[str],
    *,
    domain: str,
) -> list[dict[str, Any]]:
    missing = [question_id for question_id in question_ids if question_id not in questions_by_id]
    if missing:
        raise ValueError(f"Questions not found: {missing}")
    questions = [questions_by_id[question_id] for question_id in question_ids]
    if domain:
        mismatched = [
            str(question["id"])
            for question in questions
            if str(question.get("domain") or "") != domain
        ]
        if mismatched:
            raise ValueError(f"Questions outside domain {domain!r}: {mismatched}")
    return questions


def load_haystack_subset(path: Path, question_ids: list[str]) -> dict[str, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("haystack must be a JSON object")
    missing = [question_id for question_id in question_ids if question_id not in payload]
    if missing:
        raise ValueError(f"Haystacks not found: {missing}")
    return {
        question_id: [str(value) for value in payload[question_id]] for question_id in question_ids
    }


def load_trajectory_subset(
    path: Path,
    expected_trajectory_ids: set[str],
) -> list[dict[str, Any]]:
    trajectories = load_jsonl_by_id(path)
    missing = sorted(expected_trajectory_ids - trajectories.keys())
    if missing:
        raise ValueError(f"Trajectories not found: {missing}")
    return [trajectories[trajectory_id] for trajectory_id in sorted(expected_trajectory_ids)]


def build_run_config(
    args: argparse.Namespace,
    *,
    question_ids: list[str],
    questions_path: Path,
    haystack_path: Path,
    api_runtime: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "api_url": args.api_url,
        "api_runtime": api_runtime,
        "project_id": args.project_id,
        "run_id": args.run_id,
        "domain": args.domain,
        "question_ids": question_ids,
        "questions_sha256": sha256_file(questions_path),
        "haystack_sha256": sha256_file(haystack_path),
        "trajectories_sha256": sha256_file(Path(args.trajectories).expanduser().resolve()),
        "content_max_chars": args.content_max_chars,
        "chunking_mode": args.chunking_mode,
        "retrieval_mode": args.retrieval_mode,
        "max_planned_queries": args.max_planned_queries,
        "search_limit": args.search_limit,
        "max_context_items": args.max_context_items,
        "max_context_chars_per_item": args.max_context_chars_per_item,
        "max_context_total_chars": args.max_context_total_chars,
        "evidence_composition_mode": args.evidence_composition_mode,
        "source_evidence_bundling": args.source_evidence_bundling,
        "typed_stream_retrieval": args.typed_stream_retrieval,
        "typed_stream_limit": args.typed_stream_limit,
    }


def prepare_output(
    output_dir: Path,
    *,
    run_config: dict[str, object],
    questions: list[dict[str, Any]],
    haystack: dict[str, list[str]],
    resume: bool,
) -> set[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_inputs = output_dir / "runtime_inputs"
    runtime_inputs.mkdir(parents=True, exist_ok=True)
    config_path = output_dir / "run_config.json"
    results_path = output_dir / "per_question.jsonl"
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if not resume:
            raise ValueError(f"Output already exists; pass --resume: {output_dir}")
        if existing != run_config:
            raise ValueError("Resume configuration does not match the existing run")
    elif resume and results_path.exists():
        raise ValueError("Cannot resume without run_config.json")
    else:
        write_json(config_path, run_config)
        write_json(runtime_inputs / "questions.json", questions)
        write_json(runtime_inputs / "haystack.json", haystack)
    if not results_path.exists():
        return set()
    return {
        str(record.get("question_id") or "")
        for record in load_resumable_jsonl(results_path)
        if record.get("question_id")
    }


def run_queries(
    memory: SibylLiveApiMemory,
    *,
    questions: list[dict[str, Any]],
    haystack: dict[str, list[str]],
    output_dir: Path,
    completed_question_ids: set[str],
) -> dict[str, object]:
    started = time.monotonic()
    completed = len(completed_question_ids)
    query_seconds = 0.0
    query_embedding_usage: dict[str, object] = {}
    query_planner_usage: dict[str, object] = {}
    results_path = output_dir / "per_question.jsonl"
    with results_path.open("a", encoding="utf-8") as handle:
        for index, question in enumerate(questions):
            question_id = str(question["id"])
            if question_id in completed_question_ids:
                continue
            memory.set_query_context(question_item=question)
            query_started = time.monotonic()
            try:
                context = memory.query(str(question["question"]))
                duration = time.monotonic() - query_started
                metadata = memory.post_query_hook(
                    query=str(question["question"]),
                    query_image=None,
                    memory_context=context,
                )
            finally:
                memory.clear_query_context()
            if isinstance(metadata, dict):
                search_metadata = metadata.get("search_metadata")
                if isinstance(search_metadata, dict):
                    merge_usage(query_embedding_usage, search_metadata.get("embedding_usage"))
                    merge_usage(query_planner_usage, search_metadata.get("planner_usage"))
            query_seconds += duration
            record = {
                "index": index,
                "stream_index": index,
                "question_id": question_id,
                "question_type": question.get("question_type"),
                "category": question.get("domain"),
                "eval_function": question.get("eval_function"),
                "question_text": question.get("question"),
                "question_image": question.get("image"),
                "haystack_ids": haystack[question_id],
                "memory_context": context,
                "memory_query_duration_seconds": duration,
                "memory_post_query_metadata": metadata,
                "score_bool": None,
            }
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
            handle.flush()
            completed += 1
            print(  # noqa: T201
                f"Retrieved {completed}/{len(questions)} {question_id} in {duration:.2f}s",
                file=sys.stderr,
            )
    embedding_cost = numeric_usage(query_embedding_usage.get("cost_usd"))
    planner_cost = numeric_usage(query_planner_usage.get("cost_usd"))
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "project_id": memory.project_id,
        "run_id": memory.run_id,
        "question_count": len(questions),
        "completed_question_count": completed,
        "query_seconds_this_invocation": query_seconds,
        "query_embedding_usage_this_invocation": query_embedding_usage,
        "query_planner_usage_this_invocation": query_planner_usage,
        "query_cost_usd_this_invocation": embedding_cost + planner_cost,
        "query_cost_complete": usage_cost_complete(query_embedding_usage)
        and usage_cost_complete(query_planner_usage),
        "elapsed_seconds_this_invocation": time.monotonic() - started,
        "resumed_question_count": len(completed_question_ids),
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on {path}:{line_number}") from exc
            if not isinstance(record, dict):
                raise TypeError(f"Expected JSON object on {path}:{line_number}")
            records.append(record)
        return records


def load_resumable_jsonl(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    valid_bytes = 0
    for index, line in enumerate(lines):
        if not line.strip():
            valid_bytes += len(line)
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if any(candidate.strip() for candidate in lines[index + 1 :]):
                raise ValueError(f"Invalid JSON on {path}:{index + 1}") from exc
            quarantine = path.with_name(f"{path.name}.torn-tail")
            with quarantine.open("ab") as handle:
                handle.write(b"".join(lines[index:]))
                handle.flush()
                os.fsync(handle.fileno())
            with path.open("r+b") as handle:
                handle.truncate(valid_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            return records
        if not isinstance(record, dict):
            raise TypeError(f"Expected JSON object on {path}:{index + 1}")
        records.append(record)
        valid_bytes += len(line)
    if raw and not raw.endswith(b"\n"):
        with path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    return records


def merge_usage(total: dict[str, object], usage: object) -> None:
    if not isinstance(usage, dict):
        return
    for key in ("provider", "model"):
        value = usage.get(key)
        if isinstance(value, str) and value:
            total[key] = value
    for key in (
        "requests",
        "inputs",
        "prompt_tokens",
        "total_tokens",
        "cost_reported_requests",
        "cost_usd",
    ):
        value = numeric_usage(usage.get(key))
        total[key] = numeric_usage(total.get(key)) + value
    cost_complete = usage.get("cost_complete")
    if isinstance(cost_complete, bool):
        total["cost_complete"] = bool(total.get("cost_complete", True)) and cost_complete


def numeric_usage(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def usage_cost_complete(usage: dict[str, object]) -> bool:
    explicit = usage.get("cost_complete")
    if isinstance(explicit, bool):
        return explicit
    provider = str(usage.get("provider") or "").casefold()
    requests = numeric_usage(usage.get("requests"))
    reported = numeric_usage(usage.get("cost_reported_requests"))
    return requests == 0 or provider == "local" or reported == requests


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
