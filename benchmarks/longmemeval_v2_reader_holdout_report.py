"""Build the preregistered LongMemEval-V2 reader holdout decision report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from math import isclose
from pathlib import Path
from statistics import mean
from typing import Any

from benchmarks.longmemeval_v2_reader_holdout import (
    HOLDOUT_CONFIGURATIONS,
    PASS_COUNT,
    TOKEN_BOUNDED_MAX_RATIO,
    require_reader_holdout_plan,
)
from benchmarks.longmemeval_v2_reader_replication_report import (
    RUNTIME_IDENTITY_KEYS,
    cluster_bootstrap,
    compare_passes,
    question_histories,
    summarize_configuration,
)
from benchmarks.longmemeval_v2_reader_report import (
    DOMAINS,
    expected_run_config,
    load_json,
    load_jsonl,
    load_reader_run,
    sha256_file,
)

HOLDOUT_REPORT_SCHEMA_VERSION = "sibyl-longmemeval-v2-reader-holdout-report-v1"
BASELINE_CONFIGURATION = "baseline_fixed_reader"
CANDIDATE_CONFIGURATION = "winner_fixed_reader"
BOUNDED_CONFIGURATION = "winner_token_bounded_1_2x"
PROMPT_ROWS_FILE = "prompt_rows.jsonl"


def build_reader_holdout_report(
    *,
    plan: dict[str, Any],
    plan_path: Path,
) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    if load_json(plan_path) != plan:
        raise ValueError("Reader holdout plan path does not match the loaded plan")
    runs = require_reader_holdout_plan(plan)
    loaded, prompts, sources, runtime = load_holdout_runs(runs)
    configurations = {
        name: summarize_holdout_configuration(
            passes=loaded[name],
            prompt_passes=prompts[name],
        )
        for name in HOLDOUT_CONFIGURATIONS
    }
    comparisons = {
        "winner_token_bounded_1_2x_vs_baseline_fixed_reader": compare_holdout_configurations(
            candidate=loaded[BOUNDED_CONFIGURATION],
            baseline=loaded[BASELINE_CONFIGURATION],
            candidate_prompts=prompts[BOUNDED_CONFIGURATION],
            baseline_prompts=prompts[BASELINE_CONFIGURATION],
            candidate_summary=configurations[BOUNDED_CONFIGURATION],
            baseline_summary=configurations[BASELINE_CONFIGURATION],
        ),
        "winner_fixed_reader_vs_baseline_fixed_reader": compare_holdout_configurations(
            candidate=loaded[CANDIDATE_CONFIGURATION],
            baseline=loaded[BASELINE_CONFIGURATION],
            candidate_prompts=prompts[CANDIDATE_CONFIGURATION],
            baseline_prompts=prompts[BASELINE_CONFIGURATION],
            candidate_summary=configurations[CANDIDATE_CONFIGURATION],
            baseline_summary=configurations[BASELINE_CONFIGURATION],
        ),
        "winner_fixed_reader_vs_winner_token_bounded_1_2x": (
            compare_holdout_configurations(
                candidate=loaded[CANDIDATE_CONFIGURATION],
                baseline=loaded[BOUNDED_CONFIGURATION],
                candidate_prompts=prompts[CANDIDATE_CONFIGURATION],
                baseline_prompts=prompts[BOUNDED_CONFIGURATION],
                candidate_summary=configurations[CANDIDATE_CONFIGURATION],
                baseline_summary=configurations[BOUNDED_CONFIGURATION],
            )
        ),
    }
    bounded_budget = summarize_bounded_budget(prompts[BOUNDED_CONFIGURATION])
    costs = summarize_holdout_costs(plan, loaded=loaded)
    decision = holdout_decision(
        analysis=plan["analysis"],
        bounded_vs_baseline=comparisons["winner_token_bounded_1_2x_vs_baseline_fixed_reader"],
        candidate_vs_baseline=comparisons["winner_fixed_reader_vs_baseline_fixed_reader"],
        candidate_vs_bounded=comparisons["winner_fixed_reader_vs_winner_token_bounded_1_2x"],
        bounded_binding_rate=bounded_budget["binding_rate"],
        costs_within_budget=costs["within_budget"],
    )
    valid_experiment = bool(decision["valid_experiment"])
    return {
        "schema_version": HOLDOUT_REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if valid_experiment else "FAIL",
        "promotion_eligible": valid_experiment and decision["outcome"] == "GO",
        "claim_boundary": (
            "Fresh 96-question, question-level LongMemEval-V2 holdout on shared "
            "small-tier domain haystacks. This report does not establish leaderboard "
            "performance or corpus-level generalization."
        ),
        "protocol": plan["protocol"],
        "analysis": plan["analysis"],
        "runtime": runtime,
        "configurations": configurations,
        "comparisons": comparisons,
        "bounded_context_budget": bounded_budget,
        "costs": costs,
        "decision": decision,
        "source_artifacts": {
            "holdout_plan": {"path": str(plan_path), "sha256": sha256_file(plan_path)},
            "runs": sources,
        },
    }


def load_holdout_runs(
    runs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    loaded: dict[str, Any] = {name: {} for name in HOLDOUT_CONFIGURATIONS}
    prompts: dict[str, Any] = {name: {} for name in HOLDOUT_CONFIGURATIONS}
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
        run_dir = Path(run["output_dir"])
        summary, source = load_reader_run(run_dir=run_dir, expected=expected)
        require_holdout_receipt_identity(run_dir=run_dir, expected=expected)
        prompt_summary, prompt_source = load_holdout_prompt_rows(
            run_dir=run_dir,
            expected=expected,
            bounded=run["configuration"] == BOUNDED_CONFIGURATION,
        )
        if not isclose(
            prompt_summary["mean_memory_context_tokens"],
            summary["average_final_memory_tokens"],
            abs_tol=1e-9,
        ):
            raise ValueError(f"Reader holdout prompt token aggregate changed: {run_dir}")
        configuration = str(run["configuration"])
        pass_name = str(run["pass_id"])
        domain = str(run["domain"])
        loaded[configuration].setdefault(pass_name, {})[domain] = summary
        prompts[configuration].setdefault(pass_name, {})[domain] = prompt_summary
        source["artifacts"][PROMPT_ROWS_FILE] = prompt_source
        sources.setdefault(pass_name, {}).setdefault(configuration, {})[domain] = source
        collect_run_identity(
            expected=expected,
            summary=summary,
            source=source,
            runtime_fingerprints=runtime_fingerprints,
            reader_models=reader_models,
            evaluator_models=evaluator_models,
            temperatures=temperatures,
            official_repo_commits=official_repo_commits,
            questions_hashes=questions_hashes,
            trajectories_hashes=trajectories_hashes,
            haystack_hashes=haystack_hashes,
            domain=domain,
        )
    runtime = require_shared_run_identity(
        runtime_fingerprints=runtime_fingerprints,
        reader_models=reader_models,
        evaluator_models=evaluator_models,
        temperatures=temperatures,
        official_repo_commits=official_repo_commits,
        questions_hashes=questions_hashes,
        trajectories_hashes=trajectories_hashes,
        haystack_hashes=haystack_hashes,
    )
    return loaded, prompts, sources, runtime


def require_holdout_receipt_identity(
    *,
    run_dir: Path,
    expected: dict[str, Any],
) -> None:
    receipt = load_json(run_dir / "longmemeval_v2_official_receipt.json")
    dataset = receipt.get("dataset")
    if (
        not isinstance(dataset, dict)
        or dataset.get("selected_question_ids_sha256") != expected["selected_question_ids_sha256"]
    ):
        raise ValueError(f"Reader holdout receipt has no exact question identity: {run_dir}")


def load_holdout_prompt_rows(
    *,
    run_dir: Path,
    expected: dict[str, Any],
    bounded: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    path = run_dir / PROMPT_ROWS_FILE
    if not path.is_file():
        raise FileNotFoundError(f"Reader holdout run is missing {path}")
    rows = load_jsonl(path)
    question_ids = [str(row.get("question_id") or "") for row in rows]
    if len(question_ids) != len(set(question_ids)) or set(question_ids) != set(
        expected["question_ids"]
    ):
        raise ValueError(f"Reader holdout prompt rows do not match the plan: {run_dir}")
    token_counts = {}
    budgets = {}
    for row in rows:
        question_id = str(row["question_id"])
        token_count = row.get("memory_context_token_count")
        if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
            raise TypeError(f"Reader holdout prompt row has invalid token count: {run_dir}")
        budget = nested_context_budget(row)
        validate_context_budget(budget, bounded=bounded, run_dir=run_dir)
        token_counts[question_id] = token_count
        budgets[question_id] = budget
    return (
        {
            "question_count": len(rows),
            "memory_context_tokens": token_counts,
            "mean_memory_context_tokens": mean(token_counts.values()),
            "context_budgets": budgets,
        },
        {"path": str(path.resolve()), "sha256": sha256_file(path)},
    )


def nested_context_budget(row: dict[str, Any]) -> dict[str, Any]:
    post_query = row.get("memory_post_query_metadata")
    search = post_query.get("search_metadata") if isinstance(post_query, dict) else None
    assembly = search.get("adapter_assembly") if isinstance(search, dict) else None
    budget = assembly.get("context_expansion_budget") if isinstance(assembly, dict) else None
    if not isinstance(budget, dict):
        raise TypeError("Reader holdout prompt row has no context expansion budget telemetry")
    return budget


def validate_context_budget(
    budget: dict[str, Any],
    *,
    bounded: bool,
    run_dir: Path,
) -> None:
    if not bounded:
        if (
            budget.get("enabled") is not False
            or budget.get("max_ratio") is not None
            or budget.get("binding") is not False
            or budget.get("dropped_item_count") != 0
        ):
            raise ValueError(f"Unbounded reader run contains active budget telemetry: {run_dir}")
        return
    if budget.get("enabled") is not True or not isclose(
        numeric(budget.get("max_ratio"), "max_ratio"),
        TOKEN_BOUNDED_MAX_RATIO,
        abs_tol=1e-12,
    ):
        raise ValueError(f"Bounded reader run changed its expansion ratio: {run_dir}")
    integers = {
        key: nonnegative_int(budget.get(key), key)
        for key in (
            "base_item_count",
            "unbounded_item_count",
            "final_item_count",
            "base_token_count",
            "max_token_count",
            "unbounded_token_count",
            "final_token_count",
            "dropped_item_count",
        )
    }
    dropped_keys = budget.get("dropped_chunk_keys")
    binding = budget.get("binding")
    if not isinstance(dropped_keys, list) or not isinstance(binding, bool):
        raise TypeError(f"Bounded reader run has invalid budget telemetry: {run_dir}")
    valid = (
        integers["max_token_count"] == int(integers["base_token_count"] * TOKEN_BOUNDED_MAX_RATIO)
        and integers["base_item_count"] <= integers["final_item_count"]
        and integers["final_item_count"] <= integers["unbounded_item_count"]
        and integers["final_item_count"]
        == integers["unbounded_item_count"] - integers["dropped_item_count"]
        and integers["final_token_count"] <= integers["max_token_count"]
        and integers["dropped_item_count"] == len(dropped_keys)
        and binding == (integers["dropped_item_count"] > 0)
    )
    if binding:
        valid = valid and integers["unbounded_token_count"] > integers["max_token_count"]
    else:
        valid = valid and (integers["final_token_count"] == integers["unbounded_token_count"])
    if not valid:
        raise ValueError(f"Bounded reader run has inconsistent budget telemetry: {run_dir}")


def numeric(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"Reader holdout budget has invalid {name}")
    return float(value)


def nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"Reader holdout budget has invalid {name}")
    return value


def collect_run_identity(
    *,
    expected: dict[str, Any],
    summary: dict[str, Any],
    source: dict[str, Any],
    runtime_fingerprints: set[str],
    reader_models: set[str],
    evaluator_models: set[str],
    temperatures: set[float],
    official_repo_commits: set[str],
    questions_hashes: set[str],
    trajectories_hashes: set[str],
    haystack_hashes: dict[str, set[str]],
    domain: str,
) -> None:
    identity = source.get("input_identity")
    if not isinstance(identity, dict):
        raise TypeError("Reader holdout run has no input identity")
    official_repo_commits.add(str(identity.get("official_repo_commit") or ""))
    questions_hashes.add(str(identity.get("questions_sha256") or ""))
    trajectories_hashes.add(str(identity.get("trajectories_sha256") or ""))
    haystack_hashes[domain].add(str(identity.get("haystack_sha256") or ""))
    runtime_fingerprints.add(
        json.dumps({key: expected[key] for key in RUNTIME_IDENTITY_KEYS}, sort_keys=True)
    )
    reader_models.add(str(summary["reader_model"]).casefold())
    evaluator_models.add(str(summary["evaluator_model"]).casefold())
    temperatures.add(float(summary["reader_temperature"]))


def require_shared_run_identity(
    *,
    runtime_fingerprints: set[str],
    reader_models: set[str],
    evaluator_models: set[str],
    temperatures: set[float],
    official_repo_commits: set[str],
    questions_hashes: set[str],
    trajectories_hashes: set[str],
    haystack_hashes: dict[str, set[str]],
) -> dict[str, Any]:
    if (
        len(runtime_fingerprints) != 1
        or len(reader_models) != 1
        or len(evaluator_models) != 1
        or len(temperatures) != 1
        or "" in reader_models
        or "" in evaluator_models
    ):
        raise ValueError("Reader holdout runs do not share one runtime configuration")
    if (
        len(official_repo_commits) != 1
        or len(questions_hashes) != 1
        or len(trajectories_hashes) != 1
        or "" in official_repo_commits
        or "" in questions_hashes
        or "" in trajectories_hashes
        or any(len(values) != 1 or "" in values for values in haystack_hashes.values())
    ):
        raise ValueError("Reader holdout runs do not share one source content identity")
    runtime = json.loads(next(iter(runtime_fingerprints)))
    runtime["reader_model"] = next(iter(reader_models))
    runtime["evaluator_model"] = next(iter(evaluator_models))
    runtime["stochastic_reader"] = next(iter(temperatures)) > 0
    return runtime


def summarize_holdout_configuration(
    *,
    passes: dict[str, dict[str, dict[str, Any]]],
    prompt_passes: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    summary = summarize_configuration(passes)
    token_counts = flatten_prompt_token_counts(prompt_passes)
    summary["memory_context_tokens"] = {
        "source_metric": "prompt_rows.memory_context_token_count",
        "question_run_count": len(token_counts),
        "total": sum(token_counts),
        "mean": mean(token_counts),
    }
    return summary


def flatten_prompt_token_counts(
    passes: dict[str, dict[str, dict[str, Any]]],
) -> list[int]:
    if len(passes) != PASS_COUNT:
        raise ValueError("Reader holdout prompt data is missing passes")
    values = []
    for pass_name in sorted(passes):
        if set(passes[pass_name]) != set(DOMAINS):
            raise ValueError("Reader holdout prompt pass does not cover both domains")
        for domain in DOMAINS:
            values.extend(passes[pass_name][domain]["memory_context_tokens"].values())
    return values


def compare_holdout_configurations(
    *,
    candidate: dict[str, dict[str, dict[str, Any]]],
    baseline: dict[str, dict[str, dict[str, Any]]],
    candidate_prompts: dict[str, dict[str, dict[str, Any]]],
    baseline_prompts: dict[str, dict[str, dict[str, Any]]],
    candidate_summary: dict[str, Any],
    baseline_summary: dict[str, Any],
) -> dict[str, Any]:
    baseline_histories = question_histories(baseline)
    candidate_histories = question_histories(candidate)
    if set(baseline_histories) != set(candidate_histories):
        raise ValueError("Reader holdout configurations differ in question coverage")
    question_effects = []
    for (domain, question_id), baseline_history in sorted(baseline_histories.items()):
        candidate_history = candidate_histories[(domain, question_id)]
        if baseline_history["question_type"] != candidate_history["question_type"]:
            raise ValueError("Reader holdout configurations disagree on question type")
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
    bootstrap = cluster_bootstrap([float(row["accuracy_effect"]) for row in question_effects])
    baseline_tokens = flatten_prompt_token_counts(baseline_prompts)
    candidate_tokens = flatten_prompt_token_counts(candidate_prompts)
    baseline_token_mean = mean(baseline_tokens)
    if baseline_token_mean <= 0:
        raise ValueError("Reader holdout baseline has no memory context tokens")
    return {
        "question_effects": question_effects,
        "domain_mean_accuracy_deltas": {
            domain: mean(
                row["accuracy_effect"] for row in question_effects if row["domain"] == domain
            )
            for domain in DOMAINS
        },
        "cluster_bootstrap": bootstrap,
        "per_pass": compare_passes(baseline=baseline, candidate=candidate),
        "majority_vote_accuracy_delta": (
            candidate_summary["majority_vote"]["combined"]["accuracy"]
            - baseline_summary["majority_vote"]["combined"]["accuracy"]
        ),
        "memory_context_tokens": {
            "source_metric": "prompt_rows.memory_context_token_count",
            "baseline_mean": baseline_token_mean,
            "candidate_mean": mean(candidate_tokens),
            "inflation": mean(candidate_tokens) / baseline_token_mean - 1.0,
        },
        "model_cost_usd_delta": (
            candidate_summary["aggregate"]["total_model_cost_usd"]
            - baseline_summary["aggregate"]["total_model_cost_usd"]
        ),
    }


def summarize_bounded_budget(
    prompt_passes: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    budgets = [
        budget
        for pass_name in sorted(prompt_passes)
        for domain in DOMAINS
        for budget in prompt_passes[pass_name][domain]["context_budgets"].values()
    ]
    binding_count = sum(bool(budget["binding"]) for budget in budgets)
    return {
        "enabled": True,
        "max_ratio": TOKEN_BOUNDED_MAX_RATIO,
        "question_run_count": len(budgets),
        "binding_count": binding_count,
        "binding_rate": binding_count / len(budgets),
        "dropped_item_count": sum(int(budget["dropped_item_count"]) for budget in budgets),
        "max_observed_final_to_budget_ratio": max(
            int(budget["final_token_count"]) / int(budget["max_token_count"])
            if int(budget["max_token_count"]) > 0
            else 0.0
            for budget in budgets
        ),
    }


def summarize_holdout_costs(
    plan: dict[str, Any],
    *,
    loaded: dict[str, dict[str, dict[str, dict[str, Any]]]],
) -> dict[str, Any]:
    by_pass = {}
    by_configuration = dict.fromkeys(HOLDOUT_CONFIGURATIONS, 0.0)
    run_costs = []
    for pass_name in sorted(next(iter(loaded.values()))):
        by_pass[pass_name] = 0.0
        for configuration in HOLDOUT_CONFIGURATIONS:
            for domain in DOMAINS:
                summary = loaded[configuration][pass_name][domain]
                model_cost = float(summary["model_cost_usd"])
                by_pass[pass_name] += model_cost
                by_configuration[configuration] += model_cost
                run_costs.append(
                    {
                        "pass_id": pass_name,
                        "configuration": configuration,
                        "domain": domain,
                        "model_cost_usd": model_cost,
                        "question_count": summary["question_count"],
                        "cost_per_question_run_usd": model_cost / summary["question_count"],
                    }
                )
    budget = plan["cost_budget"]
    total = sum(by_pass.values())
    max_per_question = float(budget["pilot_cost_per_question_run_usd"]) * float(
        budget["max_pilot_cost_multiplier"]
    )
    maximum_observed = max(float(row["cost_per_question_run_usd"]) for row in run_costs)
    return {
        "by_pass_model_cost_usd": by_pass,
        "by_configuration_model_cost_usd": by_configuration,
        "runs": run_costs,
        "total_model_cost_usd": total,
        "estimated_total_model_cost_usd": budget["estimated_total_model_cost_usd"],
        "max_total_model_cost_usd": budget["max_total_model_cost_usd"],
        "max_run_cost_per_question_usd": max_per_question,
        "maximum_observed_run_cost_per_question_usd": maximum_observed,
        "within_total_budget": total <= float(budget["max_total_model_cost_usd"]),
        "within_per_run_budget": maximum_observed <= max_per_question,
        "within_budget": (
            total <= float(budget["max_total_model_cost_usd"])
            and maximum_observed <= max_per_question
        ),
    }


def treatment_gate(
    comparison: dict[str, Any],
    *,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    bootstrap = comparison["cluster_bootstrap"]
    checks = {
        "mean_accuracy_delta": (
            bootstrap["mean_accuracy_delta"] >= analysis["go_min_mean_accuracy_delta"]
        ),
        "probability_positive": (
            bootstrap["probability_positive"] >= analysis["go_min_probability_positive"]
        ),
        "confidence_interval_lower": (
            bootstrap["confidence_interval"]["lower"]
            >= analysis["go_min_confidence_interval_lower"]
        ),
        "memory_token_inflation": (
            comparison["memory_context_tokens"]["inflation"]
            <= analysis["go_max_memory_token_inflation"]
        ),
    }
    return {"passes": all(checks.values()), "checks": checks}


def holdout_decision(
    *,
    analysis: dict[str, Any],
    bounded_vs_baseline: dict[str, Any],
    candidate_vs_baseline: dict[str, Any],
    candidate_vs_bounded: dict[str, Any],
    bounded_binding_rate: float,
    costs_within_budget: bool,
) -> dict[str, Any]:
    bounded_gate = treatment_gate(bounded_vs_baseline, analysis=analysis)
    candidate_gate = treatment_gate(candidate_vs_baseline, analysis=analysis)
    binding_valid = bounded_binding_rate >= analysis["minimum_bounded_binding_rate"]
    valid_experiment = binding_valid and costs_within_budget
    candidate_beats_bounded = (
        candidate_vs_bounded["cluster_bootstrap"]["mean_accuracy_delta"]
        >= analysis["candidate_selection_min_delta_over_bounded"]
    )
    if not valid_experiment:
        outcome = "RESEARCH-MORE"
        selected = None
        reason = "holdout validity gate failed before promotion"
    elif candidate_gate["passes"] and candidate_beats_bounded:
        outcome = "GO"
        selected = CANDIDATE_CONFIGURATION
        reason = "unchanged candidate passed every gate and beat the bounded arm"
    elif bounded_gate["passes"]:
        outcome = "GO"
        selected = BOUNDED_CONFIGURATION
        reason = "bounded candidate passed every preregistered promotion gate"
    elif all(
        comparison["cluster_bootstrap"]["mean_accuracy_delta"] <= 0.0
        or comparison["cluster_bootstrap"]["probability_positive"]
        <= analysis["no_go_max_probability_positive"]
        for comparison in (bounded_vs_baseline, candidate_vs_baseline)
    ):
        outcome = "NO-GO"
        selected = None
        reason = "both treatment arms crossed the preregistered rejection boundary"
    else:
        outcome = str(analysis["otherwise"])
        selected = None
        reason = "holdout evidence crossed neither promotion nor rejection boundary"
    return {
        "outcome": outcome,
        "selected_configuration": selected,
        "reason": reason,
        "valid_experiment": valid_experiment,
        "validity": {
            "bounded_binding_rate": bounded_binding_rate,
            "bounded_binding_rate_passes": binding_valid,
            "costs_within_budget": costs_within_budget,
        },
        "treatment_gates": {
            BOUNDED_CONFIGURATION: bounded_gate,
            CANDIDATE_CONFIGURATION: candidate_gate,
        },
        "candidate_beats_bounded_by_required_delta": candidate_beats_bounded,
    }
