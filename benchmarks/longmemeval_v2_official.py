#!/usr/bin/env python3
"""Run Sibyl through the official LongMemEval-V2 harness."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen
from uuid import uuid4

HTTP_STATUS_OK = 200
HTTP_STATUS_SERVER_ERROR = 500
MIN_COMBINED_SOURCE_METRICS = 2
ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "packages" / "python" / "sibyl-core" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from sibyl_core.evals.longmemeval_v2 import (  # noqa: E402
    load_longmemeval_v2_haystack,
    load_longmemeval_v2_questions,
    summarize_longmemeval_v2_inputs,
)

PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-official-plan-v1"
RECEIPT_SCHEMA_VERSION = "sibyl-longmemeval-v2-official-receipt-v1"
ACCOUNTING_SCHEMA_VERSION = "sibyl-eval-accounting-v1"
OFFICIAL_REPO_URL = "https://github.com/xiaowu0162/LongMemEval-V2"
OFFICIAL_HARNESS_PATH = "evaluation/harness.py"
QWEN_READER_MODEL_FRAGMENT = "qwen3.5-9b"
GPT_EVALUATOR_MODEL_FRAGMENT = "gpt-5.2"
DEFAULT_METHOD = "sibyl_live_api"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.command_args = list(argv if argv is not None else sys.argv[1:])
    data_root = Path(args.data_root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if args.receipt_only:
        receipt = build_receipt_from_artifacts(
            args=args, data_root=data_root, output_dir=output_dir
        )
        write_json(resolve_receipt_output(args, output_dir), receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))  # noqa: T201
        return 0

    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    selected_questions = materialize_runtime_questions(
        data_root=data_root,
        domain=args.domain,
        question_ids=parse_question_ids(args.question_ids),
        limit=args.limit,
        output_path=runtime_dir / "questions.json",
    )
    selected_haystack = materialize_runtime_haystack(
        data_root=data_root,
        tier=args.tier,
        selected_questions=selected_questions,
        output_path=runtime_dir / "haystack.json",
    )
    memory_config = build_memory_config(args)
    memory_config_path = runtime_dir / "memory_config.json"
    write_json(memory_config_path, memory_config)

    plan = build_run_plan(
        args=args,
        data_root=data_root,
        output_dir=output_dir,
        runtime_dir=runtime_dir,
        memory_config_path=memory_config_path,
        selected_questions=selected_questions,
        selected_haystack=selected_haystack,
    )
    write_json(output_dir / "longmemeval_v2_official_plan.json", plan)
    print(json.dumps(plan, indent=2, sort_keys=True))  # noqa: T201
    if args.plan_only:
        return 0

    official_repo = resolve_official_repo(args.official_repo)
    ensure_official_harness(official_repo)
    sys.path.insert(0, str(official_repo))
    import benchmarks.longmemeval_v2_memory.sibyl_memory  # noqa: F401, PLC0415
    from evaluation.harness import main as harness_main  # noqa: PLC0415

    old_argv = sys.argv
    try:
        sys.argv = build_harness_argv(
            args=args,
            data_root=data_root,
            output_dir=output_dir,
            runtime_dir=runtime_dir,
            memory_config_path=memory_config_path,
        )
        harness_main()
    finally:
        sys.argv = old_argv
    receipt = build_receipt_from_artifacts(args=args, data_root=data_root, output_dir=output_dir)
    write_json(resolve_receipt_output(args, output_dir), receipt)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:  # noqa: PLR0915
    parser = argparse.ArgumentParser(description="Run LongMemEval-V2 with Sibyl memory.")
    parser.add_argument("--official-repo", default=os.getenv("LME_V2_OFFICIAL_REPO"))
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--domain", choices=["web", "enterprise", "combined"], required=True)
    parser.add_argument("--tier", choices=["small", "medium"], default="small")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--receipt-output", default=None)
    parser.add_argument("--receipt-only", action="store_true")
    parser.add_argument("--metric-overview", default=None)
    parser.add_argument("--combined-metrics", default=None)
    parser.add_argument("--submission-overview", default=None)
    parser.add_argument("--submission-archive", default=None)
    parser.add_argument("--web-output-dir", default=None)
    parser.add_argument("--enterprise-output-dir", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--question-ids", nargs="*", default=None)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--save-memory", action="store_true")
    parser.add_argument("--skip-evaluation", action="store_true")
    parser.add_argument("--load-memory-dir", default=None)

    parser.add_argument(
        "--api-url", default=os.getenv("SIBYL_API_URL", "http://127.0.0.1:3334/api")
    )
    parser.add_argument("--api-token", default=os.getenv("SIBYL_API_TOKEN", ""))
    parser.add_argument("--email", default=os.getenv("LME_SIBYL_EMAIL", ""))
    parser.add_argument("--password", default=os.getenv("LME_SIBYL_PASSWORD", ""))
    parser.add_argument("--project-id", default="")
    parser.add_argument(
        "--run-id", default=os.getenv("LME_V2_RUN_ID", f"lme-v2-{uuid4().hex[:12]}")
    )
    parser.add_argument("--allow-localhost", action="store_true")
    parser.add_argument("--no-signup", action="store_true")
    parser.add_argument("--content-max-chars", type=int, default=50_000)
    parser.add_argument("--search-limit", type=int, default=12)
    parser.add_argument("--max-context-items", type=int, default=8)
    parser.add_argument("--max-context-chars-per-item", type=int, default=18_000)
    parser.add_argument("--include-screenshot-refs", action="store_true")

    parser.add_argument("--reader-model", default=os.getenv("READER_MODEL", "Qwen/Qwen3.5-9B"))
    parser.add_argument(
        "--reader-base-url", default=os.getenv("READER_BASE_URL", "http://localhost:8023/v1")
    )
    parser.add_argument("--reader-api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--reader-disable-thinking", action="store_true")
    parser.add_argument("--reader-temperature", type=float, default=0.6)
    parser.add_argument("--reader-top-p", type=float, default=0.95)
    parser.add_argument("--reader-top-k", type=int, default=20)
    parser.add_argument("--reader-max-concurrent-requests", type=int, default=16)
    parser.add_argument("--max-completion-tokens", type=int, default=20_000)
    parser.add_argument("--memory-context-max-tokens", type=int, default=200_000)
    parser.add_argument("--timeout-seconds", type=float, default=43_200.0)

    parser.add_argument("--evaluator-model", default=os.getenv("EVALUATOR_MODEL", "gpt-5.2"))
    parser.add_argument("--evaluator-base-url", default=os.getenv("EVALUATOR_BASE_URL", ""))
    parser.add_argument(
        "--evaluator-api-key-env", default=os.getenv("EVALUATOR_API_KEY_ENV", "OPENAI_API_KEY")
    )
    parser.add_argument(
        "--evaluator-reasoning-effort", choices=["low", "medium", "high"], default="medium"
    )
    parser.add_argument("--evaluator-max-completion-tokens", type=int, default=4096)
    parser.add_argument("--evaluator-timeout-seconds", type=float, default=43_200.0)
    parser.add_argument("--prompt-build-max-workers", type=int, default=1)
    parser.add_argument("--shuffle-questions-seed", type=int, default=None)
    args = parser.parse_args(argv)
    if not args.receipt_only and args.domain == "combined":
        parser.error("--domain combined is only valid with --receipt-only")
    return args


def parse_question_ids(raw_values: list[str] | None) -> list[str] | None:
    if not raw_values:
        return None
    ids = []
    for raw_value in raw_values:
        ids.extend(item.strip() for item in raw_value.split(",") if item.strip())
    return ids or None


def materialize_runtime_questions(
    *,
    data_root: Path,
    domain: str,
    question_ids: list[str] | None,
    limit: int | None,
    output_path: Path,
) -> list[dict[str, Any]]:
    questions = [
        question
        for question in load_longmemeval_v2_questions(data_root / "questions.jsonl")
        if question.domain == domain
    ]
    if question_ids:
        requested = set(question_ids)
        questions = [question for question in questions if question.id in requested]
        found = {question.id for question in questions}
        missing = requested - found
        if missing:
            msg = f"Unknown question ids for {domain}: {sorted(missing)}"
            raise RuntimeError(msg)
    if limit is not None:
        if limit <= 0:
            msg = "--limit must be positive"
            raise RuntimeError(msg)
        questions = questions[:limit]
    if not questions:
        msg = "No questions selected"
        raise RuntimeError(msg)

    rows: list[dict[str, Any]] = []
    for question in questions:
        row: dict[str, Any] = {
            "id": question.id,
            "domain": question.domain,
            "environment": question.environment,
            "question_type": question.question_type,
            "question": question.question,
            "answer": question.answer,
            "eval_function": question.eval_function,
        }
        if question.image is not None:
            image_path = data_root / question.image
            if not image_path.exists():
                msg = f"Missing question image: {image_path}"
                raise RuntimeError(msg)
            row["question"] = {"text": question.question, "image": str(image_path.resolve())}
        rows.append(row)
    write_json(output_path, rows)
    return rows


def materialize_runtime_haystack(
    *,
    data_root: Path,
    tier: str,
    selected_questions: list[dict[str, Any]],
    output_path: Path,
) -> dict[str, list[str]]:
    haystack = load_longmemeval_v2_haystack(haystack_path(data_root, tier))
    selected_haystack = {}
    for question in selected_questions:
        question_id = str(question["id"])
        if question_id not in haystack:
            msg = f"Missing haystack entry for question {question_id}"
            raise RuntimeError(msg)
        selected_haystack[question_id] = list(haystack[question_id])
    write_json(output_path, selected_haystack)
    return selected_haystack


def build_memory_config(args: argparse.Namespace) -> dict[str, object]:
    params: dict[str, object] = {
        "api_url": args.api_url,
        "api_token": args.api_token,
        "email": args.email,
        "password": args.password,
        "project_id": args.project_id,
        "run_id": args.run_id,
        "allow_localhost": args.allow_localhost,
        "allow_signup": not args.no_signup,
        "content_max_chars": args.content_max_chars,
        "search_limit": args.search_limit,
        "max_context_items": args.max_context_items,
        "max_context_chars_per_item": args.max_context_chars_per_item,
        "include_screenshot_refs": args.include_screenshot_refs,
    }
    return {"memory_type": "sibyl_live_api", "memory_params": params}


def haystack_path(data_root: Path, tier: str) -> Path:
    nested = data_root / "haystacks" / f"lme_v2_{tier}.json"
    if nested.exists():
        return nested
    return data_root / f"lme_v2_{tier}.json"


def build_run_plan(
    *,
    args: argparse.Namespace,
    data_root: Path,
    output_dir: Path,
    runtime_dir: Path,
    memory_config_path: Path,
    selected_questions: list[dict[str, Any]],
    selected_haystack: dict[str, list[str]],
) -> dict[str, Any]:
    all_questions = load_longmemeval_v2_questions(data_root / "questions.jsonl")
    question_by_id = {question.id: question for question in all_questions}
    selected_question_models = [question_by_id[str(row["id"])] for row in selected_questions]
    required_trajectories = sorted({tid for ids in selected_haystack.values() for tid in ids})
    llm_eval_count = sum(
        1
        for row in selected_questions
        if str(row["eval_function"]).split("(", 1)[0]
        in {"llm_abstention_checker", "llm_gotchas_checker"}
    )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "domain": args.domain,
        "tier": args.tier,
        "method": DEFAULT_METHOD,
        "data_root": str(data_root),
        "output_dir": str(output_dir),
        "runtime_dir": str(runtime_dir),
        "memory_config_path": str(memory_config_path),
        "official_repo": args.official_repo,
        "plan_only": args.plan_only,
        "save_memory": args.save_memory,
        "skip_evaluation": args.skip_evaluation,
        "load_memory_dir": args.load_memory_dir,
        "trajectory_path": str(data_root / "trajectories.jsonl"),
        "trajectory_path_exists": (data_root / "trajectories.jsonl").exists(),
        "question_count": len(selected_questions),
        "required_trajectory_count": len(required_trajectories),
        "llm_eval_count": llm_eval_count,
        "reader_model": args.reader_model,
        "reader_base_url": args.reader_base_url,
        "evaluator_model": args.evaluator_model,
        "requirements": build_requirement_status(args=args, data_root=data_root),
        "summary": summarize_longmemeval_v2_inputs(
            selected_question_models,
            selected_haystack,
        ),
        "honesty_contract": {
            "answer_gold_visible_to_memory": False,
            "question_gold_ids_visible_to_memory": False,
            "memory_surface": "Sibyl live API /entities and /search",
            "reader_surface": "official harness reader model",
            "scoring_surface": "official deterministic and LLM scoring functions",
        },
    }


def build_requirement_status(*, args: argparse.Namespace, data_root: Path) -> dict[str, bool]:
    official_repo = Path(args.official_repo).expanduser().resolve() if args.official_repo else None
    return {
        "official_repo_configured": official_repo is not None,
        "official_harness_exists": bool(
            official_repo and (official_repo / "evaluation" / "harness.py").exists()
        ),
        "trajectories_jsonl_exists": (data_root / "trajectories.jsonl").exists(),
        "reader_api_key_env_set": bool(os.getenv(args.reader_api_key_env)),
        "reader_endpoint_reachable": reader_endpoint_reachable(args.reader_base_url),
        "evaluator_api_key_env_set": bool(os.getenv(args.evaluator_api_key_env)),
        "transformers_available": importlib.util.find_spec("transformers") is not None,
        "torch_available": importlib.util.find_spec("torch") is not None,
    }


def reader_endpoint_reachable(base_url: str) -> bool:
    if not base_url:
        return True
    models_url = f"{base_url.rstrip('/')}/models"
    if urlparse(models_url).scheme not in {"http", "https"}:
        return False
    try:
        with urlopen(models_url, timeout=2) as response:  # noqa: S310
            return HTTP_STATUS_OK <= int(response.status) < HTTP_STATUS_SERVER_ERROR
    except Exception:
        return False


def build_harness_argv(
    *,
    args: argparse.Namespace,
    data_root: Path,
    output_dir: Path,
    runtime_dir: Path,
    memory_config_path: Path,
) -> list[str]:
    argv = [
        "evaluation.harness",
        "--domain",
        args.domain,
        "--questions-path",
        str(runtime_dir / "questions.json"),
        "--haystack-path",
        str(runtime_dir / "haystack.json"),
        "--trajectories-path",
        str(data_root / "trajectories.jsonl"),
        "--memory-config-path",
        str(memory_config_path),
        "--output-dir",
        str(output_dir),
        "--model",
        args.reader_model,
        "--api-key-env",
        args.reader_api_key_env,
        "--temperature",
        str(args.reader_temperature),
        "--top-p",
        str(args.reader_top_p),
        "--top-k",
        str(args.reader_top_k),
        "--max-completion-tokens",
        str(args.max_completion_tokens),
        "--memory-context-max-tokens",
        str(args.memory_context_max_tokens),
        "--reader-max-concurrent-requests",
        str(args.reader_max_concurrent_requests),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--prompt-build-max-workers",
        str(args.prompt_build_max_workers),
        "--evaluator-model",
        args.evaluator_model,
        "--evaluator-api-key-env",
        args.evaluator_api_key_env,
        "--evaluator-reasoning-effort",
        args.evaluator_reasoning_effort,
        "--evaluator-max-completion-tokens",
        str(args.evaluator_max_completion_tokens),
        "--evaluator-timeout-seconds",
        str(args.evaluator_timeout_seconds),
    ]
    if args.save_memory:
        argv.append("--save-memory")
    if args.skip_evaluation:
        argv.append("--skip-evaluation")
    if args.load_memory_dir:
        argv.extend(["--load-memory-dir", args.load_memory_dir])
    if args.reader_base_url:
        argv.extend(["--base-url", args.reader_base_url])
    if args.reader_disable_thinking:
        argv.append("--reader-disable-thinking")
    if args.evaluator_base_url:
        argv.extend(["--evaluator-base-url", args.evaluator_base_url])
    if args.shuffle_questions_seed is not None:
        argv.extend(["--shuffle-questions-seed", str(args.shuffle_questions_seed)])
    return argv


def resolve_official_repo(raw_path: str | None) -> Path:
    if not raw_path:
        msg = "Set --official-repo or LME_V2_OFFICIAL_REPO to the LongMemEval-V2 checkout"
        raise RuntimeError(msg)
    return Path(raw_path).expanduser().resolve()


def ensure_official_harness(path: Path) -> None:
    if not (path / "evaluation" / "harness.py").exists():
        msg = f"Missing official evaluation/harness.py under {path}"
        raise RuntimeError(msg)


def resolve_receipt_output(args: argparse.Namespace, output_dir: Path) -> Path:
    if args.receipt_output:
        return Path(args.receipt_output).expanduser().resolve()
    return output_dir / "longmemeval_v2_official_receipt.json"


def build_receipt_from_artifacts(
    *,
    args: argparse.Namespace,
    data_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    official_repo = Path(args.official_repo).expanduser().resolve() if args.official_repo else None
    plan_path = output_dir / "longmemeval_v2_official_plan.json"
    aggregated_path = output_dir / "aggregated_metrics.json"
    per_question_path = output_dir / "per_question.jsonl"
    run_args_path = output_dir / "run_args.json"
    metric_overview_path = (
        _optional_path(args.metric_overview) or output_dir / "metric_overview.json"
    )
    combined_metrics_path = _optional_path(args.combined_metrics)
    submission_overview_path = _optional_path(args.submission_overview)
    submission_archive_path = _optional_path(args.submission_archive)

    plan = _load_json_if_exists(plan_path)
    run_args = _load_json_if_exists(run_args_path)
    aggregated_metrics = _load_json_if_exists(aggregated_path)
    metric_overview = _load_json_if_exists(metric_overview_path)
    combined_metrics = _load_json_if_exists(combined_metrics_path)
    submission_overview = _load_json_if_exists(submission_overview_path)
    per_question_rows = _load_jsonl_if_exists(per_question_path)
    source_runs = load_receipt_source_runs(args=args, output_dir=output_dir)
    source_runs_receipt = build_source_runs_receipt(args=args, source_runs=source_runs)

    metrics = extract_receipt_metrics(
        metric_overview=metric_overview,
        combined_metrics=combined_metrics,
        aggregated_metrics=aggregated_metrics,
        submission_overview=submission_overview,
    )
    accounting = build_receipt_accounting(
        metrics=metrics,
        aggregated_metrics=accounting_aggregated_metrics(
            source_runs=source_runs,
            combined_metrics=combined_metrics,
            aggregated_metrics=aggregated_metrics,
        ),
        per_question_rows=accounting_per_question_rows(
            source_runs=source_runs,
            fallback_rows=per_question_rows,
        ),
    )
    metrics.update(accounting_metric_aliases(accounting))
    official_repo_record = {
        "url": OFFICIAL_REPO_URL,
        "path": str(official_repo) if official_repo else None,
        "commit": git_commit(official_repo) if official_repo else None,
        "harness_path": OFFICIAL_HARNESS_PATH,
        "harness_exists": bool(official_repo and (official_repo / OFFICIAL_HARNESS_PATH).exists()),
    }
    dataset = build_dataset_receipt(
        data_root=data_root,
        domain=args.domain,
        tier=args.tier,
        plan=plan,
        aggregated_metrics=aggregated_metrics,
    )
    artifacts = build_artifact_receipt(
        output_dir=output_dir,
        plan_path=plan_path,
        aggregated_path=aggregated_path,
        per_question_path=per_question_path,
        run_args_path=run_args_path,
        metric_overview_path=metric_overview_path,
        combined_metrics_path=combined_metrics_path,
        submission_overview_path=submission_overview_path,
        submission_archive_path=submission_archive_path,
    )
    source_reader_model = _consistent_source_run_arg(source_runs, "model")
    source_reader_base_url = _consistent_source_run_arg(source_runs, "base_url")
    source_evaluator_model = _consistent_source_run_arg(source_runs, "evaluator_model")
    source_evaluator_reasoning_effort = _consistent_source_run_arg(
        source_runs,
        "evaluator_reasoning_effort",
    )
    cli_model_fallback = (
        "" if args.domain == "combined" and args.receipt_only else args.reader_model
    )
    cli_evaluator_fallback = (
        "" if args.domain == "combined" and args.receipt_only else args.evaluator_model
    )
    models = {
        "reader_model": _first_string(
            source_reader_model, run_args.get("model"), cli_model_fallback
        ),
        "reader_base_url": _first_string(
            source_reader_base_url,
            run_args.get("base_url"),
            args.reader_base_url,
        ),
        "reader_expected_fragment": QWEN_READER_MODEL_FRAGMENT,
        "evaluator_model": _first_string(
            source_evaluator_model,
            run_args.get("evaluator_model"),
            cli_evaluator_fallback,
        ),
        "evaluator_expected_fragment": GPT_EVALUATOR_MODEL_FRAGMENT,
        "evaluator_reasoning_effort": _first_string(
            source_evaluator_reasoning_effort,
            run_args.get("evaluator_reasoning_effort"),
            args.evaluator_reasoning_effort,
        ),
    }
    method = _first_string(
        _consistent_source_run_arg(source_runs, "method"),
        run_args.get("method"),
        DEFAULT_METHOD,
    )

    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "suite": "LongMemEval-V2 official",
        "suite_version": "official-harness-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "sibyl_commit": git_commit(ROOT),
        "command": ["benchmarks/longmemeval_v2_official.py", *args.command_args],
        "domain": args.domain,
        "tier": args.tier,
        "method": method,
        "claim_boundary": (
            "Official LongMemEval-V2 harness receipt for Sibyl live API memory. "
            "It is citable only when both domains are complete and LAFS gain is finite."
        ),
        "official_repo": official_repo_record,
        "dataset": dataset,
        "source_runs": source_runs_receipt,
        "models": models,
        "artifacts": artifacts,
        "metrics": metrics,
        "accounting": accounting,
        "approval_boundary": {
            "official_full_run_requires_approval": True,
            "paid_model_calls": "reader/evaluator endpoints are external to the PR-safe probe",
            "safe_without_approval": args.plan_only or args.receipt_only,
        },
    }
    receipt["checks"] = build_receipt_checks(receipt)
    return receipt


def load_receipt_source_runs(
    *,
    args: argparse.Namespace,
    output_dir: Path,
) -> list[dict[str, Any]]:
    configured = [
        ("web", _optional_path(args.web_output_dir)),
        ("enterprise", _optional_path(args.enterprise_output_dir)),
    ]
    source_dirs = [(domain, path) for domain, path in configured if path is not None]
    if not source_dirs:
        source_dirs = [(args.domain, output_dir)]

    source_runs: list[dict[str, Any]] = []
    for domain, source_dir in source_dirs:
        run_args_path = source_dir / "run_args.json"
        aggregated_path = source_dir / "aggregated_metrics.json"
        per_question_path = source_dir / "per_question.jsonl"
        source_runs.append(
            {
                "domain": domain,
                "output_dir": source_dir,
                "run_args_path": run_args_path,
                "aggregated_path": aggregated_path,
                "per_question_path": per_question_path,
                "run_args": _load_json_if_exists(run_args_path),
                "aggregated_metrics": _load_json_if_exists(aggregated_path),
                "per_question_rows": _load_jsonl_if_exists(per_question_path),
            }
        )
    return source_runs


def build_source_runs_receipt(
    *,
    args: argparse.Namespace,
    source_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    domains: dict[str, dict[str, Any]] = {}
    for source_run in source_runs:
        domain = str(source_run["domain"])
        run_args = source_run["run_args"]
        output_dir = source_run["output_dir"]
        domains[domain] = {
            "output_dir": str(output_dir),
            "run_args": artifact_path_record(source_run["run_args_path"]),
            "aggregated_metrics": artifact_path_record(source_run["aggregated_path"]),
            "per_question": artifact_path_record(source_run["per_question_path"]),
            "reader_model": run_args.get("model"),
            "reader_base_url": run_args.get("base_url"),
            "evaluator_model": run_args.get("evaluator_model"),
            "method": run_args.get("method"),
            "tier": run_args.get("tier"),
        }

    expected_domains = ("web", "enterprise") if args.domain == "combined" else (args.domain,)
    complete = all(
        domain in domains
        and domains[domain]["run_args"]["exists"]
        and domains[domain]["aggregated_metrics"]["exists"]
        and domains[domain]["per_question"]["exists"]
        for domain in expected_domains
    )
    return {
        "expected_domains": list(expected_domains),
        "domains": domains,
        "complete": complete,
        "model_consistent": _source_run_values_consistent(source_runs, "model")
        and _source_run_values_consistent(source_runs, "evaluator_model"),
        "method_consistent": _source_run_values_consistent(source_runs, "method"),
    }


def accounting_aggregated_metrics(
    *,
    source_runs: list[dict[str, Any]],
    combined_metrics: dict[str, Any],
    aggregated_metrics: dict[str, Any],
) -> dict[str, Any]:
    if combined_metrics:
        return combined_metrics
    source_metrics = [
        source_run["aggregated_metrics"]
        for source_run in source_runs
        if source_run["aggregated_metrics"]
    ]
    if len(source_metrics) == 1:
        return source_metrics[0]
    if len(source_metrics) >= MIN_COMBINED_SOURCE_METRICS:
        return combine_accounting_metrics(source_metrics)
    return aggregated_metrics


def accounting_per_question_rows(
    *,
    source_runs: list[dict[str, Any]],
    fallback_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = [
        row
        for source_run in source_runs
        for row in source_run["per_question_rows"]
        if isinstance(row, dict)
    ]
    return rows or fallback_rows


def combine_accounting_metrics(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    question_counts = [_question_count_from_metrics(metric) for metric in metrics]
    total_questions = sum(question_counts)
    return {
        "tokens": _combine_token_metrics(metrics, question_counts),
        "memory_query": _combine_timing_metrics(metrics, question_counts, "memory_query"),
        "overall": {"count_all_questions": total_questions},
    }


def _question_count_from_metrics(metrics: dict[str, Any]) -> int:
    count = _number_from(metrics, ("overall", "count_all_questions"), ("question_count",))
    return int(count or 0)


def _combine_token_metrics(
    metrics: list[dict[str, Any]],
    question_counts: list[int],
) -> dict[str, float]:
    prompt_tokens = _sum_metric(metrics, ("tokens", "prompt_tokens"))
    completion_tokens = _sum_metric(metrics, ("tokens", "completion_tokens"))
    total_tokens = _sum_metric(metrics, ("tokens", "total_tokens"))
    if total_tokens == 0.0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    total_questions = sum(question_counts)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "avg_prompt_tokens": prompt_tokens / total_questions if total_questions else 0.0,
        "avg_completion_tokens": (completion_tokens / total_questions if total_questions else 0.0),
        "avg_total_tokens": total_tokens / total_questions if total_questions else 0.0,
    }


def _combine_timing_metrics(
    metrics: list[dict[str, Any]],
    question_counts: list[int],
    section: str,
) -> dict[str, float | None]:
    total_seconds = 0.0
    saw_total = False
    max_seconds: float | None = None
    total_questions = sum(question_counts)
    for metric, question_count in zip(metrics, question_counts, strict=True):
        timing = metric.get(section) if isinstance(metric.get(section), dict) else {}
        section_total = _number_from(timing, ("total_seconds",))
        if section_total is None:
            avg_seconds = _number_from(timing, ("avg_seconds",))
            section_total = avg_seconds * question_count if avg_seconds is not None else None
        if section_total is not None:
            total_seconds += section_total
            saw_total = True
        section_max = _number_from(timing, ("max_seconds",))
        if section_max is not None:
            max_seconds = section_max if max_seconds is None else max(max_seconds, section_max)
    return {
        "avg_seconds": total_seconds / total_questions if saw_total and total_questions else None,
        "max_seconds": max_seconds,
        "total_seconds": total_seconds if saw_total else None,
    }


def _sum_metric(metrics: list[dict[str, Any]], path: tuple[str, ...]) -> float:
    total = 0.0
    for metric in metrics:
        total += _number_from(metric, path) or 0.0
    return total


def _source_run_values_consistent(source_runs: list[dict[str, Any]], key: str) -> bool:
    values = [
        value.strip()
        for source_run in source_runs
        if isinstance(value := source_run["run_args"].get(key), str) and value.strip()
    ]
    return len(set(values)) <= 1


def _consistent_source_run_arg(source_runs: list[dict[str, Any]], key: str) -> str:
    values = [
        value.strip()
        for source_run in source_runs
        if isinstance(value := source_run["run_args"].get(key), str) and value.strip()
    ]
    if not values:
        return ""
    return values[0] if len(set(values)) == 1 else ""


def extract_receipt_metrics(
    *,
    metric_overview: dict[str, Any],
    combined_metrics: dict[str, Any],
    aggregated_metrics: dict[str, Any],
    submission_overview: dict[str, Any],
) -> dict[str, float | None]:
    metric_sources = (metric_overview, combined_metrics, aggregated_metrics)
    return {
        "lafs_gain": _extract_lafs_gain(submission_overview),
        "overall_full_set": _number_from_sources(
            metric_sources,
            ("overall_full_set",),
            ("overall", "overall_full_set"),
        ),
        "gotchas_accuracy": _number_from_sources(
            metric_sources,
            ("gotchas_accuracy",),
            ("non_abstention_by_category", "gotchas", "pct_correct"),
        ),
        "static_accuracy": _number_from_sources(
            metric_sources,
            ("static_accuracy",),
            ("combined_abstention_by_category", "static", "pct_correct"),
        ),
        "dynamic_accuracy": _number_from_sources(
            metric_sources,
            ("dynamic_accuracy",),
            ("combined_abstention_by_category", "dynamic", "pct_correct"),
        ),
        "procedure_accuracy": _number_from_sources(
            metric_sources,
            ("procedure_accuracy",),
            ("combined_abstention_by_category", "procedure", "pct_correct"),
        ),
        "memory_query_avg_seconds": _number_from_sources(
            metric_sources,
            ("memory_query_avg_seconds",),
            ("memory_query", "avg_seconds"),
        ),
    }


def _number_from_sources(
    sources: tuple[dict[str, Any], ...],
    *paths: tuple[str, ...],
) -> float | None:
    for source in sources:
        number = _number_from(source, *paths)
        if number is not None:
            return number
    return None


def _extract_lafs_gain(submission_overview: dict[str, Any]) -> float | None:
    return _number_from(
        submission_overview,
        ("lafs_gain",),
        ("lafs", "lafs_gain"),
        ("lafs_summary", "lafs_gain"),
    )


def build_receipt_accounting(
    *,
    metrics: dict[str, float | None],
    aggregated_metrics: dict[str, Any],
    per_question_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    latencies = [
        latency
        for row in per_question_rows
        if (latency := _extract_case_latency_seconds(row)) is not None
    ]
    p50_seconds = percentile(latencies, 50)
    p95_seconds = percentile(latencies, 95)
    max_seconds = (
        max(latencies)
        if latencies
        else _number_from(aggregated_metrics, ("memory_query", "max_seconds"))
    )
    avg_seconds = metrics.get("memory_query_avg_seconds")
    if p50_seconds is None:
        p50_seconds = avg_seconds
    if p95_seconds is None:
        p95_seconds = max_seconds if max_seconds is not None else avg_seconds
    if max_seconds is None:
        max_seconds = p95_seconds
    p50_ms = seconds_to_ms(p50_seconds)
    p95_ms = seconds_to_ms(p95_seconds)
    max_ms = seconds_to_ms(max_seconds)

    tokens = (
        aggregated_metrics.get("tokens")
        if isinstance(aggregated_metrics.get("tokens"), dict)
        else {}
    )
    prompt_tokens = (
        _as_number(tokens.get("prompt_tokens")) or _as_number(tokens.get("total_tokens")) or 0.0
    )
    completion_tokens = _as_number(tokens.get("completion_tokens")) or 0.0
    total_tokens = _as_number(tokens.get("total_tokens")) or (prompt_tokens + completion_tokens)

    return {
        "schema_version": ACCOUNTING_SCHEMA_VERSION,
        "gate_status": "required-for-citable-longmemeval-v2-receipts",
        "latency": {
            "p50_ms": p50_ms,
            "p95_ms": p95_ms,
            "max_ms": max_ms,
            "source": "official per_question memory query timings",
        },
        "tokens": {
            "estimated_input_tokens": prompt_tokens,
            "estimated_output_tokens": completion_tokens,
            "full_context_baseline_estimated_tokens": total_tokens,
            "estimator": "official-harness-token-counters",
            "source": "official aggregated token counters",
        },
        "embedding": {
            "calls": 0,
            "provider": "not-applicable",
            "model": "not-applicable",
            "estimated_input_tokens": 0.0,
            "estimated_cost_usd": 0.0,
            "cost_basis": "official V2 runner delegates embeddings outside Sibyl receipt",
        },
        "reader": {
            "estimated_input_tokens": prompt_tokens,
            "estimated_output_tokens": completion_tokens,
            "estimated_cost_usd": 0.0,
            "cost_basis": "official reader token estimate; local/self-hosted reader cost not priced",
        },
        "judge": {
            "estimated_input_tokens": 0.0,
            "estimated_output_tokens": 0.0,
            "estimated_cost_usd": 0.0,
            "cost_basis": "official evaluator cost not priced by runner",
        },
        "cost": {
            "estimated_total_usd": 0.0,
            "currency": "USD",
            "enforcement": "receipt-recorded; external runtime pricing is model/provider dependent",
        },
    }


def accounting_metric_aliases(accounting: dict[str, Any]) -> dict[str, float]:
    latency = accounting.get("latency") if isinstance(accounting.get("latency"), dict) else {}
    tokens = accounting.get("tokens") if isinstance(accounting.get("tokens"), dict) else {}
    embedding = accounting.get("embedding") if isinstance(accounting.get("embedding"), dict) else {}
    aliases: dict[str, float] = {}
    for metric, value in (
        ("latency_p50_ms", latency.get("p50_ms")),
        ("latency_p95_ms", latency.get("p95_ms")),
        ("max_latency_ms", latency.get("max_ms")),
        ("estimated_input_tokens", tokens.get("estimated_input_tokens")),
        ("estimated_output_tokens", tokens.get("estimated_output_tokens")),
        (
            "full_context_baseline_estimated_tokens",
            tokens.get("full_context_baseline_estimated_tokens"),
        ),
        ("embedding_call_count", embedding.get("calls")),
        ("embedding_estimated_input_tokens", embedding.get("estimated_input_tokens")),
    ):
        number = _as_number(value)
        if number is not None:
            aliases[metric] = number
    return aliases


def build_dataset_receipt(
    *,
    data_root: Path,
    domain: str,
    tier: str,
    plan: dict[str, Any],
    aggregated_metrics: dict[str, Any],
) -> dict[str, Any]:
    questions_path = data_root / "questions.jsonl"
    trajectories_path = data_root / "trajectories.jsonl"
    haystack = haystack_path(data_root, tier)
    question_count, required_trajectory_count = summarize_dataset_counts(
        data_root=data_root,
        domain=domain,
        tier=tier,
    )
    dataset_record = {
        "name": "longmemeval-v2",
        "data_root": str(data_root),
        "tier": tier,
        "questions_sha256": sha256_file(questions_path),
        "trajectories_sha256": sha256_file(trajectories_path),
        "haystack_sha256": sha256_file(haystack),
        "question_count": plan.get("question_count")
        or _number_from(aggregated_metrics, ("overall", "count_all_questions")),
        "required_trajectory_count": plan.get("required_trajectory_count"),
    }
    if dataset_record["question_count"] is None:
        dataset_record["question_count"] = question_count
    if dataset_record["required_trajectory_count"] is None:
        dataset_record["required_trajectory_count"] = required_trajectory_count
    return dataset_record


def summarize_dataset_counts(
    *, data_root: Path, domain: str, tier: str
) -> tuple[int | None, int | None]:
    try:
        questions = load_longmemeval_v2_questions(data_root / "questions.jsonl")
        haystack = load_longmemeval_v2_haystack(haystack_path(data_root, tier))
    except (OSError, ValueError, RuntimeError):
        return None, None
    selected_ids = {
        question.id for question in questions if domain in ("combined", question.domain)
    }
    required_trajectories = {
        trajectory_id
        for question_id in selected_ids
        for trajectory_id in haystack.get(question_id, [])
    }
    return len(selected_ids), len(required_trajectories)


def build_artifact_receipt(
    *,
    output_dir: Path,
    plan_path: Path,
    aggregated_path: Path,
    per_question_path: Path,
    run_args_path: Path,
    metric_overview_path: Path,
    combined_metrics_path: Path | None,
    submission_overview_path: Path | None,
    submission_archive_path: Path | None,
) -> dict[str, Any]:
    paths = {
        "output_dir": output_dir,
        "plan": plan_path,
        "aggregated_metrics": aggregated_path,
        "per_question": per_question_path,
        "run_args": run_args_path,
        "metric_overview": metric_overview_path,
        "combined_metrics": combined_metrics_path,
        "submission_overview": submission_overview_path,
        "submission_archive": submission_archive_path,
    }
    return {key: artifact_path_record(path) for key, path in paths.items() if path is not None}


def build_receipt_checks(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        check(
            "official harness",
            _truthy_path(receipt, ("official_repo", "commit"))
            and _truthy_path(receipt, ("official_repo", "harness_exists")),
            "official repo commit and evaluation/harness.py are recorded",
            ["official harness"],
        ),
        check(
            "dataset hashes",
            all(
                _truthy_path(receipt, ("dataset", field))
                for field in ("questions_sha256", "trajectories_sha256", "haystack_sha256")
            ),
            "questions, trajectories, and tier haystack SHA-256 digests are recorded",
            ["dataset hashes"],
        ),
        check(
            "model pins",
            _contains_fragment(receipt["models"].get("reader_model"), QWEN_READER_MODEL_FRAGMENT)
            and _contains_fragment(
                receipt["models"].get("evaluator_model"), GPT_EVALUATOR_MODEL_FRAGMENT
            ),
            "reader is Qwen3.5-9B and evaluator is gpt-5.2",
            ["model pins"],
        ),
        check(
            "source runs",
            receipt.get("source_runs", {}).get("complete") is True
            and receipt.get("source_runs", {}).get("model_consistent") is True,
            "source web/enterprise run artifacts are present and model-consistent",
            ["source runs"],
        ),
        check(
            "leaderboard metrics",
            all(
                _is_finite_number(receipt["metrics"].get(field))
                for field in ("lafs_gain", "overall_full_set", "memory_query_avg_seconds")
            ),
            "LAFS gain, accuracy, and memory-query latency are finite",
            ["leaderboard metrics"],
        ),
        check(
            "accounting",
            _receipt_accounting_complete(receipt.get("accounting")),
            "latency, token, embedding, reader, judge, and cost sections are recorded",
            ["accounting"],
        ),
        check(
            "approval boundary",
            receipt.get("approval_boundary", {}).get("official_full_run_requires_approval") is True,
            "official full runs remain approval-bound",
            ["approval boundary"],
        ),
    ]


def check(name: str, passed: bool, detail: str, surfaces: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "detail": detail,
        "surfaces": surfaces,
    }


def _receipt_accounting_complete(accounting: Any) -> bool:
    if not isinstance(accounting, dict):
        return False
    required_sections = ("latency", "tokens", "embedding", "reader", "judge", "cost")
    if not all(isinstance(accounting.get(section), dict) for section in required_sections):
        return False
    latency = accounting["latency"]
    tokens = accounting["tokens"]
    cost = accounting["cost"]
    return (
        _is_finite_number(latency.get("p50_ms"))
        and _is_finite_number(latency.get("p95_ms"))
        and _is_finite_number(latency.get("max_ms"))
        and _is_finite_number(tokens.get("estimated_input_tokens"))
        and _is_finite_number(tokens.get("estimated_output_tokens"))
        and _is_finite_number(tokens.get("full_context_baseline_estimated_tokens"))
        and _is_finite_number(cost.get("estimated_total_usd"))
    )


def artifact_path_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "sha256": sha256_file(path) if path.is_file() else None,
        "size_bytes": path.stat().st_size if path.is_file() else None,
    }


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def git_commit(path: Path | None) -> str | None:
    if path is None:
        return None
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [git, "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _load_json_if_exists(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _load_jsonl_if_exists(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _is_finite_number(value: Any) -> bool:
    return _as_number(value) is not None


def _number_from(mapping: dict[str, Any], *paths: tuple[str, ...]) -> float | None:
    for path in paths:
        current: Any = mapping
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        number = _as_number(current)
        if number is not None:
            return number
    return None


def _truthy_path(mapping: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
    return current not in (None, "", False)


def _contains_fragment(value: Any, fragment: str) -> bool:
    return fragment in str(value or "").lower()


def _extract_case_latency_seconds(row: dict[str, Any]) -> float | None:
    seconds_paths = (
        ("memory_query_seconds",),
        ("memory_query_avg_seconds",),
        ("memory_query_latency_seconds",),
        ("timing", "memory_query_seconds"),
        ("timings", "memory_query_seconds"),
        ("memory_query", "seconds"),
        ("memory_query", "avg_seconds"),
        ("memory_query", "total_seconds"),
        ("memory_query", "elapsed_seconds"),
        ("memory_query", "duration_seconds"),
    )
    milliseconds_paths = (
        ("memory_query_ms",),
        ("memory_query_latency_ms",),
        ("timing", "memory_query_ms"),
        ("timings", "memory_query_ms"),
        ("memory_query", "ms"),
        ("memory_query", "latency_ms"),
    )
    seconds = _number_from(row, *seconds_paths)
    if seconds is not None:
        return seconds
    milliseconds = _number_from(row, *milliseconds_paths)
    if milliseconds is not None:
        return milliseconds / 1000.0
    return None


def percentile(values: list[float], percentile_value: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile_value / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def seconds_to_ms(seconds: float | None) -> float:
    return float(seconds or 0.0) * 1000.0


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
