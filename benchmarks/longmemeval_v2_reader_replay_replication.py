#!/usr/bin/env python3
"""Plan, resume, and analyze paired LongMemEval-V2 reader replays."""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.git_provenance import git_provenance  # noqa: E402
from benchmarks.longmemeval_v2_reader_replay import (  # noqa: E402
    SCHEMA_VERSION as REPLAY_SCHEMA_VERSION,
)
from benchmarks.longmemeval_v2_reader_replay import (  # noqa: E402
    load_json,
    load_jsonl,
    load_source_run,
    select_question_set,
    sha256_file,
    sha256_question_ids,
    summarize_usage,
    validate_prompt_image_bytes,
    verify_official_repo,
    write_json,
)

PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-replay-replication-plan-v1"
REPORT_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-replay-replication-report-v2"
RUNNER_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-replay-replication-runner-v1"
CONFIGURATIONS = ("frozen_baseline", "candidate")
DOMAINS = ("web", "enterprise")
NEW_PASS_SEEDS = (1729, 2718)
PASS_IDS = ("pass_01", "pass_02", "pass_03")
DEFAULT_MAX_WORKERS = 4
MAX_INCREMENTAL_COST_USD = 1.25
BOOTSTRAP_SAMPLES = 20_000
BOOTSTRAP_SEED = 20_260_716
MIN_GO_ACCURACY_DELTA = 0.03
MAX_GO_DOMAIN_REGRESSION = -0.02
MAX_NO_GO_ACCURACY_DELTA = -0.03
COMMAND_PREFIX = ("moon", "run", "root:bench-longmemeval-v2-reader-replay", "--")
RUNTIME_IDENTITY_EXCLUDED_KEYS = (
    "domain",
    "questions_path",
    "haystack_path",
    "trajectories_path",
    "memory_config_path",
    "output_dir",
    "save_memory",
    "load_memory_dir",
    "started_at_utc",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    for configuration in CONFIGURATIONS:
        for domain in DOMAINS:
            plan.add_argument(f"--{configuration.replace('_', '-')}-{domain}", required=True)
    plan.add_argument("--official-repo", required=True)
    plan.add_argument("--data-root", required=True)
    plan.add_argument("--reader-api-key-file", required=True)
    plan.add_argument("--evaluator-api-key-env", default="OPENAI_API_KEY")
    plan.add_argument("--output-root", required=True)
    plan.add_argument("--plan-path", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--plan-path", required=True)
    run.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)

    report = subparsers.add_parser("report")
    report.add_argument("--plan-path", required=True)
    report.add_argument("--output-path", required=True)
    return parser.parse_args(argv)


def source_paths(args: argparse.Namespace) -> dict[str, dict[str, Path]]:
    return {
        configuration: {
            domain: Path(getattr(args, f"{configuration}_{domain}")).expanduser().resolve()
            for domain in DOMAINS
        }
        for configuration in CONFIGURATIONS
    }


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    paths = source_paths(args)
    official_repo = Path(args.official_repo).expanduser().resolve()
    data_root = Path(args.data_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    key_file = Path(args.reader_api_key_file).expanduser().resolve()
    sources: dict[str, dict[str, Any]] = {name: {} for name in CONFIGURATIONS}
    runtime_fingerprints = set()
    existing_candidate_cost = 0.0
    runs = []

    for domain in DOMAINS:
        candidate = load_source_run(paths["candidate"][domain])
        baseline = load_source_run(paths["frozen_baseline"][domain])
        verify_official_repo(official_repo, candidate["receipt"])
        verify_official_repo(official_repo, baseline["receipt"])
        baseline_prompts, _ = select_question_set(baseline, candidate)
        selected_ids = [row["question_id"] for row in candidate["prompt_rows"]]
        selected_hash = sha256_question_ids(selected_ids)
        validate_source_pair(candidate, baseline, baseline_prompts, selected_ids)
        validate_prompt_image_bytes(
            candidate["prompt_rows"],
            data_root=data_root,
            source_receipt=candidate["receipt"],
        )
        validate_prompt_image_bytes(
            baseline_prompts,
            data_root=data_root,
            source_receipt=baseline["receipt"],
        )
        runtime = runtime_identity(candidate["run_args"])
        if runtime != runtime_identity(baseline["run_args"]):
            raise ValueError(f"Reader runtimes differ for {domain}")
        runtime_fingerprints.add(json.dumps(runtime, sort_keys=True))
        existing_candidate_cost += official_source_cost(candidate["receipt"])

        for configuration, source in (
            ("candidate", candidate),
            ("frozen_baseline", baseline),
        ):
            sources[configuration][domain] = source_snapshot(
                source,
                selected_question_ids_sha256=selected_hash,
                question_count=len(selected_ids),
                question_set_from=(
                    None if configuration == "candidate" else paths["candidate"][domain]
                ),
            )
            runs.append(
                {
                    "pass_id": "pass_01",
                    "pass_index": 1,
                    "question_order_seed": None,
                    "configuration": configuration,
                    "domain": domain,
                    "existing": True,
                    "output_dir": str(source["run_dir"]),
                    "command": None,
                }
            )

    if len(runtime_fingerprints) != 1:
        raise ValueError("Reader sources do not share one runtime configuration")
    estimated_cost = existing_candidate_cost * len(CONFIGURATIONS) * len(NEW_PASS_SEEDS)
    if estimated_cost > MAX_INCREMENTAL_COST_USD:
        raise ValueError("Estimated replay cost exceeds the fixed budget")

    for pass_index, seed in enumerate(NEW_PASS_SEEDS, start=2):
        for configuration in CONFIGURATIONS:
            for domain in DOMAINS:
                output_dir = (
                    output_root / "runs" / f"pass_{pass_index:02d}" / configuration / domain
                )
                command = replay_command(
                    source=sources[configuration][domain],
                    output_dir=output_dir,
                    official_repo=official_repo,
                    data_root=data_root,
                    reader_api_key_file=key_file,
                    evaluator_api_key_env=args.evaluator_api_key_env,
                    question_order_seed=seed,
                )
                runs.append(
                    {
                        "pass_id": f"pass_{pass_index:02d}",
                        "pass_index": pass_index,
                        "question_order_seed": seed,
                        "configuration": configuration,
                        "domain": domain,
                        "existing": False,
                        "output_dir": str(output_dir),
                        "command": command,
                    }
                )

    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "output_root": str(output_root),
        "official_repo": str(official_repo),
        "data_root": str(data_root),
        "credentials": {
            "reader_api_key_file": str(key_file),
            "evaluator_api_key_env": args.evaluator_api_key_env,
        },
        "protocol": {
            "configurations": list(CONFIGURATIONS),
            "domains": list(DOMAINS),
            "passes": list(PASS_IDS),
            "new_question_order_seeds": list(NEW_PASS_SEEDS),
            "fixed_sample_no_score_based_stopping": True,
            "max_workers": DEFAULT_MAX_WORKERS,
        },
        "analysis": {
            "unit": "question",
            "decision_passes": list(PASS_IDS[1:]),
            "bootstrap_samples": BOOTSTRAP_SAMPLES,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "confidence_level": 0.95,
            "go_min_accuracy_delta": MIN_GO_ACCURACY_DELTA,
            "go_min_domain_delta": MAX_GO_DOMAIN_REGRESSION,
            "no_go_max_accuracy_delta": MAX_NO_GO_ACCURACY_DELTA,
        },
        "cost_budget": {
            "existing_candidate_reader_judge_cost_usd": existing_candidate_cost,
            "estimated_incremental_cost_usd": estimated_cost,
            "max_incremental_cost_usd": MAX_INCREMENTAL_COST_USD,
            "basis": "two configurations times two new passes at observed candidate cost",
            "enforcement": "reserve by estimate before and detect actual spend after each wave",
            "provider_unpriced_costs_remain_explicit": True,
        },
        "integrity_contract": {
            "memory_rebuilt": False,
            "retrieval_executed": False,
            "planning_uses_scores": False,
            "all_passes_required_before_reporting": True,
            "decision_uses_fresh_passes_only": True,
            "replay_receipts_are_not_official_receipts": True,
        },
        "runtime_identity": json.loads(next(iter(runtime_fingerprints))),
        "sources": sources,
        "runs": runs,
        "runner_provenance": git_provenance(ROOT),
    }


def validate_source_pair(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    baseline_prompts: list[dict[str, Any]],
    selected_ids: list[str],
) -> None:
    if (
        candidate["receipt"]["official_repo"]["commit"]
        != baseline["receipt"]["official_repo"]["commit"]
    ):
        raise ValueError("Reader sources use different official harness commits")
    if [row["question_id"] for row in baseline_prompts] != selected_ids:
        raise ValueError("Reader sources do not share selected question order")


def runtime_identity(run_args: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in run_args.items() if key not in RUNTIME_IDENTITY_EXCLUDED_KEYS
    }


def official_source_cost(receipt: dict[str, Any]) -> float:
    accounting = receipt.get("accounting")
    if not isinstance(accounting, dict):
        raise TypeError("Source receipt has no provider accounting")
    costs = []
    for section in ("reader", "judge"):
        section_accounting = accounting.get(section)
        cost = (
            section_accounting.get("provider_reported_cost_usd")
            if isinstance(section_accounting, dict)
            else None
        )
        if isinstance(cost, bool) or not isinstance(cost, int | float):
            raise TypeError(f"Source receipt has no {section} provider cost")
        costs.append(float(cost))
    if costs[0] <= 0.0 or any(cost < 0.0 for cost in costs):
        raise ValueError("Source receipt has invalid provider costs")
    return sum(costs)


def source_snapshot(
    source: dict[str, Any],
    *,
    selected_question_ids_sha256: str,
    question_count: int,
    question_set_from: Path | None,
) -> dict[str, Any]:
    return {
        "run_dir": str(source["run_dir"]),
        "artifacts": source["artifacts"],
        "official_commit": source["receipt"]["official_repo"]["commit"],
        "selected_question_ids_sha256": selected_question_ids_sha256,
        "question_count": question_count,
        "question_set_from": str(question_set_from) if question_set_from else None,
    }


def replay_command(
    *,
    source: dict[str, Any],
    output_dir: Path,
    official_repo: Path,
    data_root: Path,
    reader_api_key_file: Path,
    evaluator_api_key_env: str,
    question_order_seed: int,
) -> list[str]:
    command = [
        *COMMAND_PREFIX,
        "--source-run-dir",
        source["run_dir"],
        "--output-dir",
        str(output_dir),
        "--official-repo",
        str(official_repo),
        "--data-root",
        str(data_root),
        "--reader-api-key-file",
        str(reader_api_key_file),
        "--evaluator-api-key-env",
        evaluator_api_key_env,
        "--question-order-seed",
        str(question_order_seed),
    ]
    if source["question_set_from"]:
        command.extend(["--question-set-from-run", source["question_set_from"]])
    return command


def require_plan(plan: dict[str, Any], *, plan_path: Path) -> list[dict[str, Any]]:
    plan_path = plan_path.expanduser().resolve()
    if load_json(plan_path) != plan or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("Replication plan path or schema is invalid")
    expected_protocol = {
        "configurations": list(CONFIGURATIONS),
        "domains": list(DOMAINS),
        "passes": list(PASS_IDS),
        "new_question_order_seeds": list(NEW_PASS_SEEDS),
        "fixed_sample_no_score_based_stopping": True,
        "max_workers": DEFAULT_MAX_WORKERS,
    }
    if plan.get("protocol") != expected_protocol:
        raise ValueError("Replication protocol changed after planning")
    expected_analysis = {
        "unit": "question",
        "decision_passes": list(PASS_IDS[1:]),
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "confidence_level": 0.95,
        "go_min_accuracy_delta": MIN_GO_ACCURACY_DELTA,
        "go_min_domain_delta": MAX_GO_DOMAIN_REGRESSION,
        "no_go_max_accuracy_delta": MAX_NO_GO_ACCURACY_DELTA,
    }
    if plan.get("analysis") != expected_analysis:
        raise ValueError("Replication analysis changed after planning")
    expected_integrity_contract = {
        "memory_rebuilt": False,
        "retrieval_executed": False,
        "planning_uses_scores": False,
        "all_passes_required_before_reporting": True,
        "decision_uses_fresh_passes_only": True,
        "replay_receipts_are_not_official_receipts": True,
    }
    if plan.get("integrity_contract") != expected_integrity_contract:
        raise ValueError("Replication integrity contract changed after planning")
    expected_incremental_cost = validate_sources(plan)
    validate_cost_budget(
        plan.get("cost_budget"),
        expected_incremental_cost=expected_incremental_cost,
    )
    runs = plan.get("runs")
    if not isinstance(runs, list):
        raise TypeError("Replication plan has no runs")
    validate_runs(plan, runs)
    return runs


def validate_cost_budget(raw: Any, *, expected_incremental_cost: float) -> None:
    if not isinstance(raw, dict):
        raise TypeError("Replication plan has no cost budget")
    estimate = float(raw.get("estimated_incremental_cost_usd", -1.0))
    if (
        estimate < 0.0
        or estimate > MAX_INCREMENTAL_COST_USD
        or estimate != expected_incremental_cost
        or raw.get("max_incremental_cost_usd") != MAX_INCREMENTAL_COST_USD
        or raw.get("enforcement")
        != "reserve by estimate before and detect actual spend after each wave"
        or raw.get("provider_unpriced_costs_remain_explicit") is not True
    ):
        raise ValueError("Replication cost budget changed after planning")


def validate_sources(plan: dict[str, Any]) -> float:
    sources = plan.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(CONFIGURATIONS):
        raise ValueError("Replication plan has invalid sources")
    official_repo = Path(plan["official_repo"])
    data_root = Path(plan["data_root"])
    candidate_cost = 0.0
    for domain in DOMAINS:
        current_by_configuration = {
            configuration: load_source_run(Path(sources[configuration][domain]["run_dir"]))
            for configuration in CONFIGURATIONS
        }
        candidate = current_by_configuration["candidate"]
        baseline = current_by_configuration["frozen_baseline"]
        baseline_prompts, _ = select_question_set(baseline, candidate)
        selected_ids = question_ids(candidate["prompt_rows"])
        validate_source_pair(candidate, baseline, baseline_prompts, selected_ids)
        for configuration, current in current_by_configuration.items():
            expected = sources[configuration][domain]
            verify_official_repo(official_repo, current["receipt"])
            prompts = candidate["prompt_rows"] if configuration == "candidate" else baseline_prompts
            validate_prompt_image_bytes(
                prompts,
                data_root=data_root,
                source_receipt=current["receipt"],
            )
            if (
                current["artifacts"] != expected["artifacts"]
                or current["receipt"]["official_repo"]["commit"] != expected["official_commit"]
                or len(selected_ids) != expected["question_count"]
                or sha256_question_ids(selected_ids) != expected["selected_question_ids_sha256"]
                or runtime_identity(current["run_args"]) != plan["runtime_identity"]
            ):
                raise ValueError("Replication source metadata changed after planning")
        candidate_cost += official_source_cost(candidate["receipt"])
    return candidate_cost * len(CONFIGURATIONS) * len(NEW_PASS_SEEDS)


def validate_runs(plan: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    expected_keys = {
        (pass_id, configuration, domain)
        for pass_id in PASS_IDS
        for configuration in CONFIGURATIONS
        for domain in DOMAINS
    }
    keys = {(run["pass_id"], run["configuration"], run["domain"]) for run in runs}
    if keys != expected_keys or len(keys) != len(runs):
        raise ValueError("Replication plan does not cover every run exactly once")
    output_root = Path(plan["output_root"]).resolve()
    for run in runs:
        pass_index = int(run["pass_index"])
        if run["pass_id"] != f"pass_{pass_index:02d}" or bool(run["existing"]) != (pass_index == 1):
            raise ValueError("Replication run identity is invalid")
        if run["existing"]:
            if run["command"] is not None:
                raise ValueError("Existing replication run gained a command")
            continue
        seed = NEW_PASS_SEEDS[pass_index - 2]
        expected_output = (
            output_root / "runs" / run["pass_id"] / run["configuration"] / run["domain"]
        )
        source = plan["sources"][run["configuration"]][run["domain"]]
        expected_command = replay_command(
            source=source,
            output_dir=expected_output,
            official_repo=Path(plan["official_repo"]),
            data_root=Path(plan["data_root"]),
            reader_api_key_file=Path(plan["credentials"]["reader_api_key_file"]),
            evaluator_api_key_env=plan["credentials"]["evaluator_api_key_env"],
            question_order_seed=seed,
        )
        if (
            Path(run["output_dir"]).resolve() != expected_output.resolve()
            or run["question_order_seed"] != seed
            or run["command"] != expected_command
        ):
            raise ValueError("Replication run command changed after planning")


def completed_replay_summary(plan: dict[str, Any], run: dict[str, Any]) -> dict[str, Any] | None:
    output_dir = Path(run["output_dir"]).resolve()
    receipt_path = output_dir / "longmemeval_v2_reader_replay_receipt.json"
    if not receipt_path.is_file():
        return None
    try:
        return require_completed_replay(plan, run, output_dir, receipt_path)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def require_completed_replay(
    plan: dict[str, Any],
    run: dict[str, Any],
    output_dir: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    receipt = load_json(receipt_path)
    run_args = load_json(output_dir / "run_args.json")
    source = plan["sources"][run["configuration"]][run["domain"]]
    if (
        receipt.get("schema_version") != REPLAY_SCHEMA_VERSION
        or receipt.get("status") != "PASS"
        or receipt.get("source", {}).get("artifacts") != source["artifacts"]
        or receipt.get("source", {}).get("official_commit") != source["official_commit"]
        or receipt.get("reader_inputs", {}).get("question_count") != source["question_count"]
        or receipt.get("reader_inputs", {}).get("question_ids_sha256")
        != source["selected_question_ids_sha256"]
        or run_args.get("question_order_seed") != run["question_order_seed"]
    ):
        raise ValueError("Replay receipt does not match the replication run")
    integrity = receipt["reader_inputs"]["image_integrity"]
    if integrity["embedded_image_count"] != integrity["verified_image_count"]:
        raise ValueError("Replay receipt has unverified image inputs")
    outputs = receipt["outputs"]
    expected_outputs = {
        "per_question": output_dir / "per_question.jsonl",
        "aggregated_metrics": output_dir / "aggregated_metrics.json",
    }
    if any(
        Path(outputs[key]["path"]).resolve() != expected_path
        or outputs[key]["sha256"] != sha256_file(expected_path)
        for key, expected_path in expected_outputs.items()
    ):
        raise ValueError("Replay outputs are not canonical or receipt-bound")
    scores = load_scores(expected_outputs["per_question"])
    if sha256_question_ids(list(scores)) != source["selected_question_ids_sha256"]:
        raise ValueError("Replay scores do not match the selected question set")
    coverage = receipt["accounting"]["cost_coverage_complete"]
    if not isinstance(coverage, bool):
        raise TypeError("Replay cost coverage must be boolean")
    return {
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
        "scores": scores,
        "cost_usd": float(receipt["accounting"]["provider_reported_cost_usd"]),
        "cost_coverage_complete": coverage,
    }


def load_scores(path: Path) -> dict[str, dict[str, Any]]:
    scores = {}
    for row in load_jsonl(path):
        question_id = row.get("question_id")
        question_type = row.get("question_type")
        eval_function = row.get("eval_function")
        score = row.get("score_bool")
        if (
            not isinstance(question_id, str)
            or not isinstance(question_type, str)
            or not question_type
            or not isinstance(eval_function, str)
            or not eval_function
            or not isinstance(score, bool)
        ):
            raise TypeError(f"Invalid scored replay row: {path}")
        if question_id in scores:
            raise ValueError(f"Duplicate scored replay question: {question_id}")
        scores[question_id] = {
            "score_bool": score,
            "question_type": question_type,
            "eval_function": eval_function,
        }
    return scores


def run_plan(plan: dict[str, Any], *, plan_path: Path, max_workers: int) -> dict[str, Any]:
    runs = require_plan(plan, plan_path=plan_path)
    validate_runner_inputs(plan, max_workers=max_workers)

    completed = []
    skipped = []
    failures = []
    archived_attempts = collect_archived_attempts(runs)
    cost = sum(attempt["cost_usd"] for attempt in archived_attempts)
    planned_run_cost = plan["cost_budget"]["estimated_incremental_cost_usd"] / (
        len(CONFIGURATIONS) * len(DOMAINS) * len(NEW_PASS_SEEDS)
    )
    for pass_id in PASS_IDS[1:]:
        wave = [run for run in runs if run["pass_id"] == pass_id]
        pending = []
        for run in wave:
            summary = completed_replay_summary(plan, run)
            if summary is None:
                if archived := archive_incomplete_attempt(run):
                    archived_attempts.append(archived)
                    cost += archived["cost_usd"]
                pending.append(run)
            else:
                skipped.append(run_key(run))
                cost += summary["cost_usd"]
        projected_cost = cost + planned_run_cost * len(pending)
        if projected_cost > plan["cost_budget"]["max_incremental_cost_usd"]:
            failures.append(
                {
                    "cost_budget_preflight_failed": True,
                    "projected_incremental_cost_usd": projected_cost,
                }
            )
            break
        wave_results = execute_wave(plan, pending, max_workers=max_workers) if pending else []
        for run, returncode in wave_results:
            summary = completed_replay_summary(plan, run)
            if summary is None:
                archived = archive_incomplete_attempt(run)
                if archived:
                    archived_attempts.append(archived)
                    cost += archived["cost_usd"]
                failures.append(
                    {
                        "run": run_key(run),
                        "returncode": returncode,
                        "provider_reported_cost_usd": archived["cost_usd"] if archived else 0.0,
                    }
                )
            else:
                completed.append(run_key(run))
                cost += summary["cost_usd"]
        if cost > plan["cost_budget"]["max_incremental_cost_usd"]:
            failures.append({"cost_budget_exceeded": True, "incremental_cost_usd": cost})
        if failures:
            break
    receipt = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "completed_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if not failures else "FAIL",
        "plan": {"path": str(plan_path), "sha256": sha256_file(plan_path)},
        "completed": completed,
        "skipped": skipped,
        "failures": failures,
        "archived_attempts": archived_attempts,
        "incremental_provider_reported_cost_usd": cost,
        "max_incremental_cost_usd": plan["cost_budget"]["max_incremental_cost_usd"],
    }
    output_root = Path(plan["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(output_root / "runner_receipt.json", receipt)
    return receipt


def validate_runner_inputs(plan: dict[str, Any], *, max_workers: int) -> None:
    if max_workers < 1 or max_workers > plan["protocol"]["max_workers"]:
        raise ValueError("Replication worker count is outside the frozen protocol")
    key_file = Path(plan["credentials"]["reader_api_key_file"])
    evaluator_env = plan["credentials"]["evaluator_api_key_env"]
    if not key_file.is_file() or not key_file.read_text(encoding="utf-8").strip():
        raise ValueError("Reader API key file is missing or empty")
    if not os.getenv(evaluator_env):
        raise ValueError(f"Evaluator API key environment is unset: {evaluator_env}")


def archive_incomplete_attempt(run: dict[str, Any]) -> dict[str, Any] | None:
    output_dir = Path(run["output_dir"])
    if not output_dir.exists():
        return None
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError(f"Replay output is not a directory: {output_dir}")
    if not any(output_dir.iterdir()):
        return None
    archive_path = next_attempt_archive(output_dir)
    output_dir.replace(archive_path)
    return archived_attempt_summary(run, archive_path)


def next_attempt_archive(output_dir: Path) -> Path:
    attempt = 1
    while True:
        candidate = output_dir.with_name(f"{output_dir.name}.attempt-{attempt:02d}")
        if not candidate.exists():
            return candidate
        attempt += 1


def collect_archived_attempts(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attempts = []
    for run in runs:
        if run["existing"]:
            continue
        output_dir = Path(run["output_dir"])
        for archive_path in sorted(output_dir.parent.glob(f"{output_dir.name}.attempt-*")):
            if archive_path.is_symlink() or not archive_path.is_dir():
                raise ValueError(f"Replay attempt archive is not a directory: {archive_path}")
            attempts.append(archived_attempt_summary(run, archive_path))
    return attempts


def archived_attempt_summary(run: dict[str, Any], archive_path: Path) -> dict[str, Any]:
    run_args_path = archive_path / "run_args.json"
    run_id = None
    if run_args_path.is_file():
        candidate = load_json(run_args_path).get("provider_usage_run_id")
        if isinstance(candidate, str) and candidate:
            run_id = candidate
    usage = (
        {
            role: summarize_usage(
                archive_path / "provider_usage" / f"{role}.jsonl",
                run_id=run_id,
            )
            for role in ("reader", "judge")
        }
        if run_id
        else {}
    )
    requests = sum(summary["requests"] for summary in usage.values())
    priced_requests = sum(summary["priced_requests"] for summary in usage.values())
    receipt_path = archive_path / "longmemeval_v2_reader_replay_receipt.json"
    return {
        "run": run_key(run),
        "path": str(archive_path),
        "receipt_sha256": sha256_file(receipt_path) if receipt_path.is_file() else None,
        "requests": requests,
        "priced_requests": priced_requests,
        "cost_usd": sum(summary["provider_reported_cost_usd"] for summary in usage.values()),
        "cost_coverage_complete": requests == priced_requests,
    }


def execute_wave(
    plan: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    max_workers: int,
) -> list[tuple[dict[str, Any], int]]:
    if not runs:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(runs))) as executor:
        futures = {executor.submit(execute_run, plan, run): run for run in runs}
        return [(futures[future], future.result()) for future in as_completed(futures)]


def execute_run(plan: dict[str, Any], run: dict[str, Any]) -> int:
    log_dir = Path(plan["output_root"]) / "logs" / run["pass_id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run['configuration']}-{run['domain']}.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps({"event": "start", "run": run_key(run)}) + "\n")
        log.flush()
        result = subprocess.run(  # noqa: S603
            run["command"],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log.write(
            json.dumps({"event": "complete", "run": run_key(run), "returncode": result.returncode})
            + "\n"
        )
    return result.returncode


def run_key(run: dict[str, Any]) -> str:
    return f"{run['pass_id']}:{run['configuration']}:{run['domain']}"


def build_report(plan: dict[str, Any], *, plan_path: Path) -> dict[str, Any]:
    runs = require_plan(plan, plan_path=plan_path)
    loaded: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        configuration: {} for configuration in CONFIGURATIONS
    }
    sources = {}
    archived_attempts = collect_archived_attempts(runs)
    incremental_cost = sum(attempt["cost_usd"] for attempt in archived_attempts)
    cost_coverage_complete = all(attempt["cost_coverage_complete"] for attempt in archived_attempts)
    for run in runs:
        if run["existing"]:
            summary = existing_summary(plan, run)
        else:
            summary = completed_replay_summary(plan, run)
            if summary is None:
                raise ValueError(f"Replication replay is incomplete: {run_key(run)}")
            incremental_cost += summary["cost_usd"]
            cost_coverage_complete &= summary["cost_coverage_complete"]
        loaded[run["configuration"]].setdefault(run["pass_id"], {})[run["domain"]] = summary[
            "scores"
        ]
        sources[run_key(run)] = {key: value for key, value in summary.items() if key != "scores"}

    configurations = {
        configuration: summarize_configuration(loaded[configuration])
        for configuration in CONFIGURATIONS
    }
    comparison = compare_configurations(loaded)
    within_budget = incremental_cost <= plan["cost_budget"]["max_incremental_cost_usd"]
    question_count = sum(
        plan["sources"]["candidate"][domain]["question_count"] for domain in DOMAINS
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if within_budget else "FAIL",
        "claim_boundary": (
            f"Paired reader and judge replication on the frozen {question_count}-question "
            "development set. "
            "Retrieval and memory are not rerun; this is not a new official leaderboard score."
        ),
        "protocol": plan["protocol"],
        "analysis": plan["analysis"],
        "configurations": configurations,
        "comparison": comparison,
        "costs": {
            "incremental_provider_reported_cost_usd": incremental_cost,
            "max_incremental_cost_usd": plan["cost_budget"]["max_incremental_cost_usd"],
            "within_budget": within_budget,
            "cost_coverage_complete": cost_coverage_complete,
            "archived_attempts": archived_attempts,
        },
        "source_artifacts": {
            "plan": {"path": str(plan_path), "sha256": sha256_file(plan_path)},
            "runs": sources,
        },
    }


def existing_summary(plan: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    source = plan["sources"][run["configuration"]][run["domain"]]
    selected_ids = selected_question_ids(plan, run["domain"])
    all_scores = load_scores(Path(source["run_dir"]) / "per_question.jsonl")
    try:
        scores = {question_id: all_scores[question_id] for question_id in selected_ids}
    except KeyError as error:
        raise ValueError(f"Existing source is missing question: {error.args[0]}") from error
    return {
        "scores": scores,
        "source_path": source["run_dir"],
        "source_per_question_sha256": source["artifacts"]["per_question.jsonl"]["sha256"],
    }


def selected_question_ids(plan: dict[str, Any], domain: str) -> list[str]:
    source = plan["sources"]["candidate"][domain]
    return question_ids(load_jsonl(Path(source["run_dir"]) / "prompt_rows.jsonl"))


def question_ids(rows: list[dict[str, Any]]) -> list[str]:
    result = []
    seen = set()
    for row in rows:
        question_id = row.get("question_id")
        if not isinstance(question_id, str) or question_id in seen:
            raise ValueError("Question IDs must be unique strings")
        seen.add(question_id)
        result.append(question_id)
    return result


def summarize_configuration(passes: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    if set(passes) != set(PASS_IDS):
        raise ValueError("Configuration is missing replication passes")
    pass_summaries = {}
    for pass_id in PASS_IDS:
        domains = passes[pass_id]
        if set(domains) != set(DOMAINS):
            raise ValueError("Replication pass is missing a domain")
        domain_accuracy = {
            domain: mean(int(row["score_bool"]) for row in domains[domain].values())
            for domain in DOMAINS
        }
        combined = [
            int(row["score_bool"]) for domain in DOMAINS for row in domains[domain].values()
        ]
        pass_summaries[pass_id] = {
            "accuracy": mean(combined),
            "correct_count": sum(combined),
            "question_count": len(combined),
            "domains": domain_accuracy,
        }
    accuracies = [summary["accuracy"] for summary in pass_summaries.values()]
    return {
        "passes": pass_summaries,
        "mean_accuracy": mean(accuracies),
        "accuracy_stdev": stdev(accuracies),
        "min_accuracy": min(accuracies),
        "max_accuracy": max(accuracies),
    }


def compare_configurations(
    loaded: dict[str, dict[str, dict[str, dict[str, Any]]]],
) -> dict[str, Any]:
    decision_passes = PASS_IDS[1:]
    effects = []
    effect_values: list[float] = []
    domain_effects: dict[str, list[float]] = {domain: [] for domain in DOMAINS}
    per_pass = {}
    for pass_id in PASS_IDS:
        pass_effects = []
        for domain in DOMAINS:
            baseline = loaded["frozen_baseline"][pass_id][domain]
            candidate = loaded["candidate"][pass_id][domain]
            if set(baseline) != set(candidate):
                raise ValueError("Configurations differ in question coverage")
            pass_effects.extend(
                int(candidate[question_id]["score_bool"]) - int(baseline[question_id]["score_bool"])
                for question_id in baseline
            )
        per_pass[pass_id] = mean(pass_effects)

    for domain in DOMAINS:
        baseline_ids = set(loaded["frozen_baseline"][PASS_IDS[0]][domain])
        for pass_id in PASS_IDS:
            if (
                set(loaded["frozen_baseline"][pass_id][domain]) != baseline_ids
                or set(loaded["candidate"][pass_id][domain]) != baseline_ids
            ):
                raise ValueError("Replication passes differ in question coverage")
        for question_id in sorted(baseline_ids):
            question_descriptors = {
                (
                    loaded[configuration][pass_id][domain][question_id]["question_type"],
                    loaded[configuration][pass_id][domain][question_id]["eval_function"],
                )
                for configuration in CONFIGURATIONS
                for pass_id in PASS_IDS
            }
            if len(question_descriptors) != 1:
                raise ValueError("Replication runs disagree on question metadata")
            question_type, eval_function = question_descriptors.pop()
            baseline_scores = [
                int(loaded["frozen_baseline"][pass_id][domain][question_id]["score_bool"])
                for pass_id in decision_passes
            ]
            candidate_scores = [
                int(loaded["candidate"][pass_id][domain][question_id]["score_bool"])
                for pass_id in decision_passes
            ]
            accuracy_effect = mean(candidate_scores) - mean(baseline_scores)
            effect_values.append(accuracy_effect)
            domain_effects[domain].append(accuracy_effect)
            effects.append(
                {
                    "domain": domain,
                    "question_id": question_id,
                    "question_type": question_type,
                    "eval_function": eval_function,
                    "evaluator": eval_function.partition("|")[0],
                    "baseline_success_rate": mean(baseline_scores),
                    "candidate_success_rate": mean(candidate_scores),
                    "accuracy_effect": accuracy_effect,
                }
            )
    bootstrap = cluster_bootstrap(effect_values)
    domain_deltas = {domain: mean(domain_effects[domain]) for domain in DOMAINS}
    question_type_effects = {
        question_type: summarize_question_effects(
            [effect for effect in effects if effect["question_type"] == question_type]
        )
        for question_type in sorted({effect["question_type"] for effect in effects})
    }
    evaluator_effects = {
        evaluator: summarize_question_effects(
            [effect for effect in effects if effect["evaluator"] == evaluator]
        )
        for evaluator in sorted({effect["evaluator"] for effect in effects})
    }
    return {
        "candidate": "candidate",
        "baseline": "frozen_baseline",
        "decision_passes": list(decision_passes),
        "per_pass_accuracy_deltas": per_pass,
        "all_passes_mean_accuracy_delta": mean(per_pass.values()),
        "domain_mean_accuracy_deltas": domain_deltas,
        "question_type_effects": question_type_effects,
        "evaluator_effects": evaluator_effects,
        "paired_outcomes": summarize_question_effects(effects),
        "question_effects": effects,
        "cluster_bootstrap": bootstrap,
        "decision": replication_decision(
            mean_delta=bootstrap["mean_accuracy_delta"],
            confidence_interval=bootstrap["confidence_interval"],
            domain_deltas=domain_deltas,
        ),
    }


def summarize_question_effects(effects: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "question_count": len(effects),
        "baseline_mean_accuracy": mean(effect["baseline_success_rate"] for effect in effects),
        "candidate_mean_accuracy": mean(effect["candidate_success_rate"] for effect in effects),
        "mean_accuracy_delta": mean(effect["accuracy_effect"] for effect in effects),
        "improved_question_count": sum(effect["accuracy_effect"] > 0 for effect in effects),
        "regressed_question_count": sum(effect["accuracy_effect"] < 0 for effect in effects),
        "unchanged_question_count": sum(effect["accuracy_effect"] == 0 for effect in effects),
    }


def cluster_bootstrap(effects: list[float]) -> dict[str, Any]:
    generator = random.Random(BOOTSTRAP_SEED)  # noqa: S311
    count = len(effects)
    samples = sorted(mean(generator.choices(effects, k=count)) for _ in range(BOOTSTRAP_SAMPLES))
    return {
        "unit": "question",
        "samples": BOOTSTRAP_SAMPLES,
        "seed": BOOTSTRAP_SEED,
        "mean_accuracy_delta": mean(effects),
        "confidence_interval": {
            "lower": percentile(samples, 0.025),
            "upper": percentile(samples, 0.975),
        },
        "probability_positive": sum(value > 0.0 for value in samples) / BOOTSTRAP_SAMPLES,
    }


def percentile(values: list[float], probability: float) -> float:
    position = (len(values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def replication_decision(
    *,
    mean_delta: float,
    confidence_interval: dict[str, float],
    domain_deltas: dict[str, float],
) -> dict[str, str]:
    if (
        mean_delta >= MIN_GO_ACCURACY_DELTA
        and confidence_interval["lower"] > 0.0
        and all(delta >= MAX_GO_DOMAIN_REGRESSION for delta in domain_deltas.values())
    ):
        return {"outcome": "GO", "reason": "practical positive effect with no domain regression"}
    if mean_delta <= MAX_NO_GO_ACCURACY_DELTA or confidence_interval["upper"] < 0.0:
        return {"outcome": "NO-GO", "reason": "practical negative effect or negative interval"}
    return {"outcome": "RESEARCH-MORE", "reason": "effect remains inside frozen boundaries"}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "plan":
        plan = build_plan(args)
        plan_path = Path(args.plan_path).expanduser().resolve()
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        if plan_path.exists():
            raise FileExistsError(f"Replication plan already exists: {plan_path}")
        write_json(plan_path, plan)
        print(json.dumps(plan, indent=2, sort_keys=True))  # noqa: T201
        return 0
    plan_path = Path(args.plan_path).expanduser().resolve()
    plan = load_json(plan_path)
    if args.command == "run":
        receipt = run_plan(plan, plan_path=plan_path, max_workers=args.max_workers)
        print(json.dumps(receipt, indent=2, sort_keys=True))  # noqa: T201
        return 0 if receipt["status"] == "PASS" else 1
    report = build_report(plan, plan_path=plan_path)
    output_path = Path(args.output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
