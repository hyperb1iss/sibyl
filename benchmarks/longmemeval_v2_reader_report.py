"""Build a receipt-bound, descriptive report for LongMemEval-V2 reader runs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from math import isclose
from pathlib import Path
from typing import Any

READER_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-plan-v1"
READER_REPORT_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-report-v1"
READER_CONFIGURATION_COUNT = 3
DOMAINS = ("web", "enterprise")
QUERY_OVERRIDE_KEYS = (
    "search_limit",
    "max_context_items",
    "max_chunks_per_trajectory",
    "neighbor_stitch_items",
    "neighbor_stitch_span",
    "state_part_completion_items",
    "state_part_refinement",
)
REQUIRED_RUN_FILES = (
    "aggregated_metrics.json",
    "exit_code",
    "longmemeval_v2_official_receipt.json",
    "per_question.jsonl",
    "run_args.json",
)


def build_reader_report(
    *,
    reader_plan: dict[str, Any],
    run_roots: dict[str, Path],
) -> dict[str, Any]:
    configurations = require_reader_plan(reader_plan)
    if set(run_roots) != set(configurations):
        raise ValueError("Reader report runs must match reader plan configurations")

    planned = planned_runs(reader_plan)
    runs: dict[str, dict[str, dict[str, Any]]] = {}
    source_runs: dict[str, dict[str, dict[str, Any]]] = {}
    temperatures: set[float] = set()
    reader_models: set[str] = set()
    evaluator_models: set[str] = set()
    for configuration in configurations:
        runs[configuration] = {}
        source_runs[configuration] = {}
        for domain in DOMAINS:
            run_dir = run_roots[configuration] / domain
            summary, source = load_reader_run(
                run_dir=run_dir,
                expected=planned[(configuration, domain)],
            )
            runs[configuration][domain] = summary
            source_runs[configuration][domain] = source
            temperatures.add(summary["reader_temperature"])
            reader_models.add(summary["reader_model"])
            evaluator_models.add(summary["evaluator_model"])

    if "" in reader_models or "" in evaluator_models:
        raise ValueError("Reader runs are missing model pins")
    if len(temperatures) != 1 or len(reader_models) != 1 or len(evaluator_models) != 1:
        raise ValueError("Reader runs do not share one model and generation configuration")

    summaries = {
        configuration: summarize_configuration(runs[configuration])
        for configuration in configurations
    }
    baseline = configurations[0]
    comparisons = {
        f"{configuration}_vs_{baseline}": compare_configurations(
            candidate=runs[configuration],
            baseline=runs[baseline],
            candidate_summary=summaries[configuration],
            baseline_summary=summaries[baseline],
        )
        for configuration in configurations[1:]
    }
    comparisons[f"{configurations[2]}_vs_{configurations[1]}"] = compare_configurations(
        candidate=runs[configurations[2]],
        baseline=runs[configurations[1]],
        candidate_summary=summaries[configurations[2]],
        baseline_summary=summaries[configurations[1]],
    )

    return {
        "schema_version": READER_REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "claim_boundary": (
            "Descriptive single-pass reader evidence only. The official reader uses "
            "stochastic sampling, so this report does not make a promotion decision or "
            "establish statistical significance."
        ),
        "replication": {
            "passes_per_configuration": 1,
            "reader_temperature": next(iter(temperatures)),
            "stochastic_reader": next(iter(temperatures)) > 0,
            "promotion_decision": None,
        },
        "models": {
            "reader": next(iter(reader_models)),
            "evaluator": next(iter(evaluator_models)),
        },
        "configurations": summaries,
        "comparisons": comparisons,
        "source_artifacts": {"runs": source_runs},
    }


def require_reader_plan(reader_plan: dict[str, Any]) -> list[str]:
    if reader_plan.get("schema_version") != READER_PLAN_SCHEMA_VERSION:
        raise ValueError("Invalid reader plan")
    configurations = reader_plan.get("configurations")
    if not isinstance(configurations, list) or len(configurations) != READER_CONFIGURATION_COUNT:
        raise ValueError("Reader plan must contain exactly three configurations")
    if any(not isinstance(item, str) or not item for item in configurations):
        raise TypeError("Reader plan contains an invalid configuration")
    if len(configurations) != len(set(configurations)):
        raise ValueError("Reader plan configurations must be unique")
    return configurations


def planned_runs(reader_plan: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    commands = reader_plan.get("commands")
    if not isinstance(commands, list):
        raise TypeError("Reader plan is missing commands")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in commands:
        if not isinstance(item, dict) or not isinstance(item.get("command"), list):
            raise TypeError("Reader plan contains an invalid command")
        key = (str(item.get("configuration") or ""), str(item.get("domain") or ""))
        if key in result:
            raise ValueError(f"Duplicate reader plan command: {key}")
        result[key] = expected_run_config(item["command"])
    expected_keys = {
        (configuration, domain)
        for configuration in require_reader_plan(reader_plan)
        for domain in DOMAINS
    }
    if set(result) != expected_keys:
        raise ValueError("Reader plan commands do not cover every configuration and domain")
    return result


def expected_run_config(command: list[Any]) -> dict[str, Any]:
    values = [str(item) for item in command]
    result: dict[str, Any] = {
        "domain": option_value(values, "--domain"),
        "output_dir": option_value(values, "--output-dir"),
        "load_memory_dir": option_value(values, "--load-memory-dir"),
        "reader_model": option_value(values, "--reader-model"),
        "reader_temperature": float(option_value(values, "--reader-temperature")),
        "reader_top_p": float(option_value(values, "--reader-top-p")),
        "reader_top_k": int(option_value(values, "--reader-top-k")),
        "question_ids": option_values(values, "--question-ids"),
        "memory_context_max_tokens": int(option_value(values, "--memory-context-max-tokens")),
    }
    for key in QUERY_OVERRIDE_KEYS:
        flag = f"--{key.replace('_', '-')}"
        if key == "state_part_refinement":
            result[key] = flag in values
        else:
            result[key] = int(option_value(values, flag))
    return result


def load_reader_run(
    *,
    run_dir: Path,
    expected: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = run_dir.resolve()
    require_reader_run_files(run_dir)
    receipt = load_json(run_dir / "longmemeval_v2_official_receipt.json")
    run_args = load_json(run_dir / "run_args.json")
    metrics = load_json(run_dir / "aggregated_metrics.json")
    rows = load_jsonl(run_dir / "per_question.jsonl")
    reader_model, evaluator_model = validate_reader_run_identity(
        run_dir=run_dir,
        expected=expected,
        receipt=receipt,
        run_args=run_args,
    )
    scored_rows = validate_reader_scores(
        run_dir=run_dir,
        expected=expected,
        rows=rows,
        metrics=metrics,
    )
    summary = summarize_reader_run(
        metrics=metrics,
        scored_rows=scored_rows,
        model_cost=reader_model_cost(run_dir=run_dir, receipt=receipt),
        reader_model=reader_model,
        evaluator_model=evaluator_model,
        reader_temperature=float(run_args["temperature"]),
    )
    source = {
        "path": str(run_dir),
        "artifacts": {
            name: {"path": str(run_dir / name), "sha256": sha256_file(run_dir / name)}
            for name in REQUIRED_RUN_FILES
        },
    }
    return summary, source


def require_reader_run_files(run_dir: Path) -> None:
    for name in REQUIRED_RUN_FILES:
        if not (run_dir / name).is_file():
            raise FileNotFoundError(f"Reader run is missing {run_dir / name}")
    if (run_dir / "exit_code").read_text(encoding="utf-8").strip() != "0":
        raise ValueError(f"Reader run did not exit cleanly: {run_dir}")


def validate_reader_run_identity(
    *,
    run_dir: Path,
    expected: dict[str, Any],
    receipt: dict[str, Any],
    run_args: dict[str, Any],
) -> tuple[str, str]:
    domain = str(receipt.get("domain") or "")
    if domain != expected["domain"] or run_args.get("domain") != expected["domain"]:
        raise ValueError(f"Reader run does not match planned domain: {run_dir}")
    if Path(run_args.get("output_dir", "")).resolve() != Path(expected["output_dir"]).resolve():
        raise ValueError(f"Reader run does not match planned output directory: {run_dir}")
    if (
        Path(run_args.get("load_memory_dir", "")).resolve()
        != Path(expected["load_memory_dir"]).resolve()
    ):
        raise ValueError(f"Reader run does not match planned memory build: {run_dir}")
    source_domain = receipt.get("source_runs", {}).get("domains", {}).get(domain, {})
    memory_params = source_domain.get("effective_memory_config", {}).get("memory_params", {})
    if not isinstance(memory_params, dict):
        raise TypeError(f"Reader receipt has no effective memory config: {run_dir}")

    actual_config = {key: memory_params.get(key) for key in QUERY_OVERRIDE_KEYS}
    expected_config = {key: expected[key] for key in QUERY_OVERRIDE_KEYS}
    if actual_config != expected_config:
        raise ValueError(f"Reader run does not match planned retrieval config: {run_dir}")
    if run_args.get("memory_context_max_tokens") != expected["memory_context_max_tokens"]:
        raise ValueError(f"Reader run does not match planned context limit: {run_dir}")
    receipt_models = receipt.get("models")
    if not isinstance(receipt_models, dict):
        raise TypeError(f"Reader receipt has no model pins: {run_dir}")
    actual_reader_model = str(receipt_models.get("reader_model") or "")
    if (
        actual_reader_model.casefold() != expected["reader_model"].casefold()
        or str(run_args.get("model") or "").casefold() != expected["reader_model"].casefold()
    ):
        raise ValueError(f"Reader run does not match planned reader model: {run_dir}")
    generation_config = {
        "reader_temperature": float(run_args["temperature"]),
        "reader_top_p": float(run_args["top_p"]),
        "reader_top_k": int(run_args["top_k"]),
    }
    if generation_config != {
        key: expected[key] for key in ("reader_temperature", "reader_top_p", "reader_top_k")
    }:
        raise ValueError(f"Reader run does not match planned generation config: {run_dir}")
    source_runs = receipt.get("source_runs")
    if not isinstance(source_runs, dict) or any(
        source_runs.get(key) is not True
        for key in ("complete", "integrity_complete", "api_runtime_consistent")
    ):
        raise ValueError(f"Reader run has incomplete source integrity: {run_dir}")
    return actual_reader_model, str(receipt_models.get("evaluator_model") or "")


def validate_reader_scores(
    *,
    run_dir: Path,
    expected: dict[str, Any],
    rows: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    question_ids = [str(row.get("question_id") or "") for row in rows]
    if len(question_ids) != len(set(question_ids)) or set(question_ids) != set(
        expected["question_ids"]
    ):
        raise ValueError(f"Reader run question set does not match the plan: {run_dir}")
    if not all(isinstance(row.get("score_bool"), bool) for row in rows):
        raise TypeError(f"Reader run is missing official Boolean scores: {run_dir}")
    overall = metrics.get("overall")
    if not isinstance(overall, dict):
        raise TypeError(f"Reader run metrics are incomplete: {run_dir}")
    correct_count = sum(row["score_bool"] for row in rows)
    official_accuracy = float(overall["overall_full_set"])
    if not isclose(official_accuracy, correct_count / len(rows), abs_tol=1e-12):
        raise ValueError(
            f"Reader run aggregate score disagrees with per-question scores: {run_dir}"
        )
    return {
        str(row["question_id"]): {
            "question_type": str(row.get("question_type") or "unknown"),
            "score_bool": row["score_bool"],
        }
        for row in rows
    }


def reader_model_cost(*, run_dir: Path, receipt: dict[str, Any]) -> float:
    accounting = receipt.get("accounting")
    if not isinstance(accounting, dict):
        raise TypeError(f"Reader receipt has no accounting: {run_dir}")
    model_cost = 0.0
    for role in ("reader", "judge"):
        role_accounting = accounting.get(role)
        if (
            not isinstance(role_accounting, dict)
            or role_accounting.get("cost_coverage_complete") is not True
        ):
            raise ValueError(f"Reader run has incomplete {role} cost coverage: {run_dir}")
        role_cost = role_accounting.get("provider_reported_cost_usd")
        if isinstance(role_cost, bool) or not isinstance(role_cost, int | float):
            raise TypeError(f"Reader run has invalid {role} provider cost: {run_dir}")
        model_cost += float(role_cost)
    return model_cost


def summarize_reader_run(
    *,
    metrics: dict[str, Any],
    scored_rows: dict[str, dict[str, Any]],
    model_cost: float,
    reader_model: str,
    evaluator_model: str,
    reader_temperature: float,
) -> dict[str, Any]:
    overall = metrics.get("overall")
    token_metrics = metrics.get("tokens")
    memory_context = metrics.get("memory_context")
    memory_query = metrics.get("memory_query")
    metric_groups = (overall, token_metrics, memory_context, memory_query)
    if not all(isinstance(item, dict) for item in metric_groups):
        raise TypeError("Reader run metrics are incomplete")
    correct_count = sum(row["score_bool"] for row in scored_rows.values())
    official_accuracy = float(overall["overall_full_set"])
    return {
        "question_count": len(scored_rows),
        "correct_count": correct_count,
        "accuracy": official_accuracy,
        "prompt_tokens": int(token_metrics["prompt_tokens"]),
        "completion_tokens": int(token_metrics["completion_tokens"]),
        "average_original_memory_tokens": float(memory_context["avg_original_tokens"]),
        "average_final_memory_tokens": float(memory_context["avg_final_tokens"]),
        "truncated_question_count": int(memory_context["num_truncated_sequences"]),
        "average_memory_query_seconds": float(memory_query["avg_seconds"]),
        "model_cost_usd": model_cost,
        "reader_model": reader_model,
        "evaluator_model": evaluator_model,
        "reader_temperature": reader_temperature,
        "scores": scored_rows,
    }


def summarize_configuration(domains: dict[str, dict[str, Any]]) -> dict[str, Any]:
    question_count = sum(item["question_count"] for item in domains.values())
    correct_count = sum(item["correct_count"] for item in domains.values())
    model_cost = sum(item["model_cost_usd"] for item in domains.values())
    prompt_tokens = sum(item["prompt_tokens"] for item in domains.values())
    completion_tokens = sum(item["completion_tokens"] for item in domains.values())
    return {
        "domains": {
            domain: {
                key: value
                for key, value in item.items()
                if key not in {"scores", "reader_model", "evaluator_model", "reader_temperature"}
            }
            for domain, item in domains.items()
        },
        "combined": {
            "question_count": question_count,
            "correct_count": correct_count,
            "accuracy": correct_count / question_count,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model_cost_usd": model_cost,
            "average_final_memory_tokens": sum(
                item["average_final_memory_tokens"] * item["question_count"]
                for item in domains.values()
            )
            / question_count,
        },
    }


def compare_configurations(
    *,
    candidate: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]],
    candidate_summary: dict[str, Any],
    baseline_summary: dict[str, Any],
) -> dict[str, Any]:
    recoveries = []
    regressions = []
    agreements = 0
    total = 0
    for domain in DOMAINS:
        candidate_scores = candidate[domain]["scores"]
        baseline_scores = baseline[domain]["scores"]
        if set(candidate_scores) != set(baseline_scores):
            raise ValueError(f"Reader configurations differ in {domain} question coverage")
        for question_id, candidate_row in candidate_scores.items():
            baseline_row = baseline_scores[question_id]
            total += 1
            if candidate_row["score_bool"] == baseline_row["score_bool"]:
                agreements += 1
                continue
            change = {
                "domain": domain,
                "question_id": question_id,
                "question_type": candidate_row["question_type"],
            }
            (recoveries if candidate_row["score_bool"] else regressions).append(change)

    candidate_combined = candidate_summary["combined"]
    baseline_combined = baseline_summary["combined"]
    return {
        "accuracy_delta": candidate_combined["accuracy"] - baseline_combined["accuracy"],
        "correct_count_delta": (
            candidate_combined["correct_count"] - baseline_combined["correct_count"]
        ),
        "model_cost_usd_delta": (
            candidate_combined["model_cost_usd"] - baseline_combined["model_cost_usd"]
        ),
        "prompt_tokens_delta": (
            candidate_combined["prompt_tokens"] - baseline_combined["prompt_tokens"]
        ),
        "agreement_rate": agreements / total,
        "recoveries": sorted(recoveries, key=lambda item: (item["domain"], item["question_id"])),
        "regressions": sorted(regressions, key=lambda item: (item["domain"], item["question_id"])),
    }


def option_value(command: list[str], flag: str) -> str:
    try:
        index = command.index(flag)
        value = command[index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Reader command is missing {flag}") from exc
    if value.startswith("--"):
        raise ValueError(f"Reader command has no value for {flag}")
    return value


def option_values(command: list[str], flag: str) -> list[str]:
    try:
        index = command.index(flag) + 1
    except ValueError as exc:
        raise ValueError(f"Reader command is missing {flag}") from exc
    values = []
    while index < len(command) and not command[index].startswith("--"):
        values.append(command[index])
        index += 1
    if not values:
        raise ValueError(f"Reader command has no values for {flag}")
    return values


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"
