"""Aggregate receipt-bound LongMemEval-V2 reader replications."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from benchmarks.longmemeval_v2_reader_replication import (
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    MAX_GO_DOMAIN_REGRESSION,
    MAX_NO_GO_ACCURACY_DELTA,
    MIN_GO_ACCURACY_DELTA,
    PASS_COUNT,
    PRIMARY_CONFIGURATIONS,
    require_reader_replication_plan,
)
from benchmarks.longmemeval_v2_reader_report import (
    DOMAINS,
    expected_run_config,
    load_json,
    load_reader_run,
    sha256_file,
)

REPLICATION_REPORT_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-replication-report-v1"
RUNTIME_IDENTITY_KEYS = (
    "reader_model",
    "reader_temperature",
    "reader_top_p",
    "reader_top_k",
    "reader_base_url",
    "reader_api_key_env",
    "reader_max_concurrent_requests",
    "max_completion_tokens",
    "timeout_seconds",
    "evaluator_model",
    "evaluator_base_url",
    "evaluator_api_key_env",
    "evaluator_reasoning_effort",
    "evaluator_max_completion_tokens",
    "evaluator_timeout_seconds",
    "prompt_build_max_workers",
)


def build_reader_replication_report(
    *,
    plan: dict[str, Any],
    plan_path: Path,
) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    if load_json(plan_path) != plan:
        raise ValueError("Reader replication plan path does not match the loaded plan")
    planned_runs = require_reader_replication_plan(plan)
    loaded, sources, runtime = load_replication_runs(plan, runs=planned_runs)
    configurations = {
        name: summarize_configuration(loaded[name]) for name in PRIMARY_CONFIGURATIONS
    }
    comparison = compare_primary_configurations(
        baseline=loaded[PRIMARY_CONFIGURATIONS[0]],
        candidate=loaded[PRIMARY_CONFIGURATIONS[1]],
        baseline_summary=configurations[PRIMARY_CONFIGURATIONS[0]],
        candidate_summary=configurations[PRIMARY_CONFIGURATIONS[1]],
    )
    costs = summarize_costs(plan, loaded=loaded)
    return {
        "schema_version": REPLICATION_REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if costs["within_incremental_budget"] else "FAIL",
        "promotion_eligible": costs["within_incremental_budget"],
        "claim_boundary": (
            "Replicated reader evidence on the frozen 32-question diagnostic slice. "
            "This report does not establish leaderboard performance or generalization."
        ),
        "protocol": plan["protocol"],
        "analysis": plan["analysis"],
        "runtime": runtime,
        "configurations": configurations,
        "comparison": comparison,
        "costs": costs,
        "source_artifacts": {
            "replication_plan": {"path": str(plan_path), "sha256": sha256_file(plan_path)},
            "runs": sources,
        },
    }


def load_replication_runs(
    plan: dict[str, Any],
    *,
    runs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    loaded: dict[str, Any] = {name: {} for name in PRIMARY_CONFIGURATIONS}
    sources: dict[str, Any] = {}
    runtime_fingerprints = set()
    reader_models = set()
    evaluator_models = set()
    temperatures = set()
    official_repo_commits = set()
    questions_hashes = set()
    trajectories_hashes = set()
    haystack_hashes = {domain: set() for domain in DOMAINS}
    for run in runs:
        expected = expected_run_config(run["command"])
        summary, source = load_reader_run(
            run_dir=Path(run["output_dir"]),
            expected=expected,
        )
        validate_existing_source_binding(plan, run=run, source=source)
        configuration = str(run["configuration"])
        pass_name = str(run["pass_id"])
        domain = str(run["domain"])
        loaded[configuration].setdefault(pass_name, {})[domain] = summary
        sources.setdefault(pass_name, {}).setdefault(configuration, {})[domain] = source
        input_identity = source.get("input_identity")
        if not isinstance(input_identity, dict):
            raise TypeError("Reader replication run has no input identity")
        official_repo_commits.add(input_identity.get("official_repo_commit"))
        questions_hashes.add(input_identity.get("questions_sha256"))
        trajectories_hashes.add(input_identity.get("trajectories_sha256"))
        haystack_hashes[domain].add(input_identity.get("haystack_sha256"))
        runtime_fingerprints.add(
            json.dumps({key: expected[key] for key in RUNTIME_IDENTITY_KEYS}, sort_keys=True)
        )
        reader_models.add(summary["reader_model"].casefold())
        evaluator_models.add(summary["evaluator_model"].casefold())
        temperatures.add(summary["reader_temperature"])
    if (
        len(runtime_fingerprints) != 1
        or len(reader_models) != 1
        or len(evaluator_models) != 1
        or len(temperatures) != 1
        or "" in reader_models
        or "" in evaluator_models
    ):
        raise ValueError("Reader replication runs do not share one runtime configuration")
    if (
        len(official_repo_commits) != 1
        or len(questions_hashes) != 1
        or len(trajectories_hashes) != 1
        or any(len(values) != 1 for values in haystack_hashes.values())
    ):
        raise ValueError("Reader replication runs do not share one source content identity")
    runtime = json.loads(next(iter(runtime_fingerprints)))
    runtime["reader_model"] = next(iter(reader_models))
    runtime["evaluator_model"] = next(iter(evaluator_models))
    runtime["stochastic_reader"] = next(iter(temperatures)) > 0
    return loaded, sources, runtime


def validate_existing_source_binding(
    plan: dict[str, Any],
    *,
    run: dict[str, Any],
    source: dict[str, Any],
) -> None:
    if not run["existing"]:
        return
    expected = plan["source_artifacts"]["existing_runs"][run["configuration"]][run["domain"]]
    if source != expected:
        raise ValueError("Existing reader run changed after replication planning")


def summarize_configuration(passes: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    if len(passes) != PASS_COUNT:
        raise ValueError("Reader configuration is missing replication passes")
    pass_summaries = {
        pass_name: summarize_pass(domains) for pass_name, domains in sorted(passes.items())
    }
    combined = [item["combined"] for item in pass_summaries.values()]
    domain_summaries = {
        domain: summarize_domain_passes(
            [pass_summaries[pass_name]["domains"][domain] for pass_name in pass_summaries]
        )
        for domain in DOMAINS
    }
    histories = question_histories(passes)
    majority = summarize_majority_vote(histories)
    return {
        "passes": pass_summaries,
        "aggregate": {
            **distribution([item["accuracy"] for item in combined]),
            "question_count_per_pass": combined[0]["question_count"],
            "total_model_cost_usd": sum(item["model_cost_usd"] for item in combined),
            "mean_model_cost_usd": mean(item["model_cost_usd"] for item in combined),
            "total_prompt_tokens": sum(item["prompt_tokens"] for item in combined),
            "total_completion_tokens": sum(item["completion_tokens"] for item in combined),
            "mean_memory_query_seconds": mean(
                item["average_memory_query_seconds"] for item in combined
            ),
        },
        "domains": domain_summaries,
        "majority_vote": majority,
    }


def summarize_pass(domains: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if set(domains) != set(DOMAINS):
        raise ValueError("Reader pass does not cover both domains")
    question_count = sum(item["question_count"] for item in domains.values())
    correct_count = sum(item["correct_count"] for item in domains.values())
    combined = {
        "question_count": question_count,
        "correct_count": correct_count,
        "accuracy": correct_count / question_count,
        "model_cost_usd": sum(item["model_cost_usd"] for item in domains.values()),
        "prompt_tokens": sum(item["prompt_tokens"] for item in domains.values()),
        "completion_tokens": sum(item["completion_tokens"] for item in domains.values()),
        "average_final_memory_tokens": weighted_mean(
            domains.values(), key="average_final_memory_tokens"
        ),
        "average_memory_query_seconds": weighted_mean(
            domains.values(), key="average_memory_query_seconds"
        ),
    }
    return {
        "domains": {domain: public_run_summary(summary) for domain, summary in domains.items()},
        "combined": combined,
    }


def public_run_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in summary.items()
        if key not in {"scores", "reader_model", "evaluator_model", "reader_temperature"}
    }


def weighted_mean(summaries: Any, *, key: str) -> float:
    items = list(summaries)
    total = sum(item["question_count"] for item in items)
    return sum(item[key] * item["question_count"] for item in items) / total


def summarize_domain_passes(passes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **distribution([item["accuracy"] for item in passes]),
        "question_count_per_pass": passes[0]["question_count"],
        "total_model_cost_usd": sum(item["model_cost_usd"] for item in passes),
        "mean_memory_query_seconds": mean(item["average_memory_query_seconds"] for item in passes),
    }


def distribution(values: list[float]) -> dict[str, float]:
    return {
        "mean_accuracy": mean(values),
        "accuracy_stdev": stdev(values),
        "min_accuracy": min(values),
        "max_accuracy": max(values),
    }


def question_histories(
    passes: dict[str, dict[str, dict[str, Any]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    histories: dict[tuple[str, str], dict[str, Any]] = {}
    pass_names = sorted(passes)
    for pass_name in pass_names:
        current_keys = set()
        for domain in DOMAINS:
            for question_id, row in passes[pass_name][domain]["scores"].items():
                key = (domain, question_id)
                current_keys.add(key)
                history = histories.setdefault(
                    key,
                    {"question_type": row["question_type"], "scores": []},
                )
                if history["question_type"] != row["question_type"]:
                    raise ValueError("Reader replication changed a question type")
                history["scores"].append(row["score_bool"])
        if set(histories) != current_keys:
            raise ValueError("Reader replication passes differ in question coverage")
    if any(len(item["scores"]) != PASS_COUNT for item in histories.values()):
        raise ValueError("Reader replication question history is incomplete")
    return histories


def summarize_majority_vote(
    histories: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    rows = []
    for (domain, question_id), history in sorted(histories.items()):
        correct_count = sum(history["scores"])
        rows.append(
            {
                "domain": domain,
                "question_id": question_id,
                "question_type": history["question_type"],
                "success_rate": correct_count / PASS_COUNT,
                "majority_correct": correct_count > PASS_COUNT // 2,
                "unanimous": correct_count in {0, PASS_COUNT},
            }
        )
    return {
        "combined": majority_summary(rows),
        "domains": {
            domain: majority_summary([row for row in rows if row["domain"] == domain])
            for domain in DOMAINS
        },
        "questions": rows,
    }


def majority_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    correct_count = sum(row["majority_correct"] for row in rows)
    unanimous_count = sum(row["unanimous"] for row in rows)
    return {
        "question_count": len(rows),
        "correct_count": correct_count,
        "accuracy": correct_count / len(rows),
        "unanimous_question_count": unanimous_count,
        "unanimous_rate": unanimous_count / len(rows),
    }


def compare_primary_configurations(
    *,
    baseline: dict[str, dict[str, dict[str, Any]]],
    candidate: dict[str, dict[str, dict[str, Any]]],
    baseline_summary: dict[str, Any],
    candidate_summary: dict[str, Any],
) -> dict[str, Any]:
    baseline_histories = question_histories(baseline)
    candidate_histories = question_histories(candidate)
    if set(baseline_histories) != set(candidate_histories):
        raise ValueError("Reader configurations differ in question coverage")
    question_effects = []
    for (domain, question_id), baseline_history in sorted(baseline_histories.items()):
        candidate_history = candidate_histories[(domain, question_id)]
        if baseline_history["question_type"] != candidate_history["question_type"]:
            raise ValueError("Reader configurations disagree on question type")
        baseline_rate = mean(baseline_history["scores"])
        candidate_rate = mean(candidate_history["scores"])
        question_effects.append(
            {
                "domain": domain,
                "question_id": question_id,
                "question_type": baseline_history["question_type"],
                "baseline_success_rate": baseline_rate,
                "candidate_success_rate": candidate_rate,
                "accuracy_effect": candidate_rate - baseline_rate,
            }
        )
    bootstrap = cluster_bootstrap([item["accuracy_effect"] for item in question_effects])
    domain_deltas = {
        domain: mean(
            item["accuracy_effect"] for item in question_effects if item["domain"] == domain
        )
        for domain in DOMAINS
    }
    decision = replication_decision(
        mean_delta=bootstrap["mean_accuracy_delta"],
        confidence_interval=bootstrap["confidence_interval"],
        domain_deltas=domain_deltas,
    )
    return {
        "candidate": PRIMARY_CONFIGURATIONS[1],
        "baseline": PRIMARY_CONFIGURATIONS[0],
        "per_pass": compare_passes(baseline=baseline, candidate=candidate),
        "question_effects": question_effects,
        "domain_mean_accuracy_deltas": domain_deltas,
        "cluster_bootstrap": bootstrap,
        "majority_vote_accuracy_delta": (
            candidate_summary["majority_vote"]["combined"]["accuracy"]
            - baseline_summary["majority_vote"]["combined"]["accuracy"]
        ),
        "model_cost_usd_delta": (
            candidate_summary["aggregate"]["total_model_cost_usd"]
            - baseline_summary["aggregate"]["total_model_cost_usd"]
        ),
        "decision": decision,
    }


def compare_passes(
    *,
    baseline: dict[str, dict[str, dict[str, Any]]],
    candidate: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    result = {}
    for pass_name in sorted(baseline):
        baseline_scores = pass_score_map(baseline[pass_name])
        candidate_scores = pass_score_map(candidate[pass_name])
        if set(baseline_scores) != set(candidate_scores):
            raise ValueError("Reader configurations differ in pass question coverage")
        recoveries = []
        regressions = []
        for key, baseline_row in baseline_scores.items():
            candidate_row = candidate_scores[key]
            if baseline_row["score_bool"] == candidate_row["score_bool"]:
                continue
            change = {
                "domain": key[0],
                "question_id": key[1],
                "question_type": baseline_row["question_type"],
            }
            (recoveries if candidate_row["score_bool"] else regressions).append(change)
        result[pass_name] = {
            "accuracy_delta": mean(
                int(candidate_scores[key]["score_bool"]) - int(baseline_scores[key]["score_bool"])
                for key in baseline_scores
            ),
            "recovery_count": len(recoveries),
            "regression_count": len(regressions),
            "recoveries": sorted(
                recoveries, key=lambda item: (item["domain"], item["question_id"])
            ),
            "regressions": sorted(
                regressions,
                key=lambda item: (item["domain"], item["question_id"]),
            ),
        }
    return result


def pass_score_map(domains: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (domain, question_id): row
        for domain in DOMAINS
        for question_id, row in domains[domain]["scores"].items()
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
        "confidence_level": 0.95,
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
) -> dict[str, Any]:
    lower = confidence_interval["lower"]
    upper = confidence_interval["upper"]
    if (
        mean_delta >= MIN_GO_ACCURACY_DELTA
        and lower > 0.0
        and all(delta >= -MAX_GO_DOMAIN_REGRESSION for delta in domain_deltas.values())
    ):
        outcome = "GO"
        reason = "positive mean, positive confidence bound, and no domain regression"
    elif mean_delta <= MAX_NO_GO_ACCURACY_DELTA or upper < 0.0:
        outcome = "NO-GO"
        reason = "negative practical effect or wholly negative confidence interval"
    else:
        outcome = "RESEARCH-MORE"
        reason = "replicated evidence does not cross the frozen promotion or rejection boundary"
    return {"outcome": outcome, "reason": reason}


def summarize_costs(
    plan: dict[str, Any],
    *,
    loaded: dict[str, dict[str, dict[str, dict[str, Any]]]],
) -> dict[str, Any]:
    by_pass = {
        pass_name: sum(
            loaded[configuration][pass_name][domain]["model_cost_usd"]
            for configuration in PRIMARY_CONFIGURATIONS
            for domain in DOMAINS
        )
        for pass_name in sorted(next(iter(loaded.values())))
    }
    existing = by_pass["pass_01"]
    incremental = sum(cost for wave_name, cost in by_pass.items() if wave_name != "pass_01")
    maximum = plan["cost_budget"]["max_incremental_model_cost_usd"]
    return {
        "by_pass_model_cost_usd": by_pass,
        "existing_model_cost_usd": existing,
        "incremental_model_cost_usd": incremental,
        "total_model_cost_usd": existing + incremental,
        "estimated_incremental_model_cost_usd": plan["cost_budget"][
            "estimated_incremental_model_cost_usd"
        ],
        "max_incremental_model_cost_usd": maximum,
        "within_incremental_budget": incremental <= maximum,
    }
