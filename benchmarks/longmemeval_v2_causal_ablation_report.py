"""Report causal LongMemEval-V2 development and confirmation experiments."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from math import isclose
from pathlib import Path
from statistics import mean
from typing import Any

from benchmarks.longmemeval_v2_causal_ablation import (
    BASELINE_CONFIGURATION,
    BUNDLE_CONFIGURATION,
    CAUSAL_CONFIGURATIONS,
    CAUSAL_REPORT_SCHEMA_VERSION,
    CONFIRMATION_REPORT_SCHEMA_VERSION,
    NEIGHBOR_CONFIGURATION,
    NEW_DEVELOPMENT_CONFIGURATIONS,
    REFINEMENT_CONFIGURATION,
    TREATMENT_CONFIGURATIONS,
    require_causal_ablation_plan,
    require_causal_confirmation_plan,
)
from benchmarks.longmemeval_v2_reader_holdout_report import (
    PROMPT_ROWS_FILE,
    collect_run_identity,
    compare_holdout_configurations,
    nested_context_budget,
    require_shared_run_identity,
    summarize_bounded_budget,
    summarize_holdout_configuration,
    validate_context_budget,
)
from benchmarks.longmemeval_v2_reader_replication_report import (
    cluster_bootstrap,
    question_histories,
)
from benchmarks.longmemeval_v2_reader_report import (
    DOMAINS,
    expected_run_config,
    load_json,
    load_jsonl,
    load_reader_run,
    sha256_file,
)


def build_causal_ablation_report(
    *,
    plan: dict[str, Any],
    plan_path: Path,
) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    if load_json(plan_path) != plan:
        raise ValueError("Causal ablation plan path does not match the loaded plan")
    require_causal_ablation_plan(plan)
    loaded, prompts, sources, runtime = load_causal_development_runs(plan)
    raw_configurations = {
        name: summarize_holdout_configuration(
            passes=loaded[name],
            prompt_passes=prompts[name],
        )
        for name in CAUSAL_CONFIGURATIONS
    }
    comparisons: dict[str, dict[str, Any]] = {
        str(f"{configuration}_vs_{BASELINE_CONFIGURATION}"): causal_comparison(
            candidate=loaded[configuration],
            baseline=loaded[BASELINE_CONFIGURATION],
            candidate_prompts=prompts[configuration],
            baseline_prompts=prompts[BASELINE_CONFIGURATION],
            candidate_summary=raw_configurations[configuration],
            baseline_summary=raw_configurations[BASELINE_CONFIGURATION],
        )
        for configuration in TREATMENT_CONFIGURATIONS
    }
    budgets = {
        configuration: summarize_bounded_budget(prompts[configuration])
        for configuration in TREATMENT_CONFIGURATIONS
    }
    costs = development_costs(plan, loaded=loaded)
    decision = select_development_candidate(
        comparisons=comparisons,
        factors=plan["configuration_factors"],
        analysis=plan["analysis"],
        costs_within_budget=costs["within_budget"],
    )
    return {
        "schema_version": CAUSAL_REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if costs["within_budget"] else "FAIL",
        "claim_boundary": (
            "Development-only causal ablation on 40 previously consumed holdout questions. "
            "This report is candidate selection evidence, not promotion evidence."
        ),
        "protocol": plan["protocol"],
        "analysis": plan["analysis"],
        "runtime": runtime,
        "configurations": {
            name: strip_incomparable_costs(summary) for name, summary in raw_configurations.items()
        },
        "comparisons": comparisons,
        "factorial_effects": summarize_factorial_effects(loaded),
        "bounded_context_budgets": budgets,
        "costs": costs,
        "decision": decision,
        "source_artifacts": {
            "causal_plan": {"path": str(plan_path), "sha256": sha256_file(plan_path)},
            "runs": sources,
        },
    }


def load_causal_development_runs(
    plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    loaded: dict[str, Any] = {name: {} for name in CAUSAL_CONFIGURATIONS}
    prompts: dict[str, Any] = {name: {} for name in CAUSAL_CONFIGURATIONS}
    sources: dict[str, Any] = {}
    identity_sets = shared_identity_sets()
    holdout_report = load_json(Path(plan["source_artifacts"]["holdout_report"]["path"]))
    for run in plan["existing_runs"]:
        expected = expected_run_config(run["command"])
        run_dir = Path(run["output_dir"])
        full_summary, source = load_reader_run(run_dir=run_dir, expected=expected)
        prompt_summary, prompt_source = load_prompt_rows_subset(
            run_dir=run_dir,
            expected_question_ids=expected["question_ids"],
            selected_question_ids=run["selected_question_ids"],
            bounded=run["configuration"] == BUNDLE_CONFIGURATION,
        )
        source["artifacts"][PROMPT_ROWS_FILE] = prompt_source
        expected_source = holdout_report["source_artifacts"]["runs"][run["pass_id"]][
            run["source_configuration"]
        ][run["domain"]]
        if source != expected_source:
            raise ValueError("Existing causal development artifact changed after planning")
        summary = subset_reader_summary(
            run_dir=run_dir,
            full_summary=full_summary,
            selected_question_ids=run["selected_question_ids"],
        )
        store_loaded_run(
            run=run,
            expected=expected,
            summary=summary,
            prompt_summary=prompt_summary,
            source=source,
            loaded=loaded,
            prompts=prompts,
            sources=sources,
            identity_sets=identity_sets,
        )
    for run in plan["runs"]:
        expected = expected_run_config(run["command"])
        run_dir = Path(run["output_dir"])
        summary, source = load_reader_run(run_dir=run_dir, expected=expected)
        prompt_summary, prompt_source = load_prompt_rows_subset(
            run_dir=run_dir,
            expected_question_ids=expected["question_ids"],
            selected_question_ids=expected["question_ids"],
            bounded=True,
        )
        source["artifacts"][PROMPT_ROWS_FILE] = prompt_source
        store_loaded_run(
            run=run,
            expected=expected,
            summary=summary,
            prompt_summary=prompt_summary,
            source=source,
            loaded=loaded,
            prompts=prompts,
            sources=sources,
            identity_sets=identity_sets,
        )
    runtime = require_shared_run_identity(**identity_sets)
    return loaded, prompts, sources, runtime


def shared_identity_sets() -> dict[str, Any]:
    return {
        "runtime_fingerprints": set(),
        "reader_models": set(),
        "evaluator_models": set(),
        "temperatures": set(),
        "official_repo_commits": set(),
        "questions_hashes": set(),
        "trajectories_hashes": set(),
        "haystack_hashes": {domain: set() for domain in DOMAINS},
    }


def store_loaded_run(
    *,
    run: dict[str, Any],
    expected: dict[str, Any],
    summary: dict[str, Any],
    prompt_summary: dict[str, Any],
    source: dict[str, Any],
    loaded: dict[str, Any],
    prompts: dict[str, Any],
    sources: dict[str, Any],
    identity_sets: dict[str, Any],
) -> None:
    if not isclose(
        prompt_summary["mean_memory_context_tokens"],
        summary["average_final_memory_tokens"],
        abs_tol=1e-9,
    ):
        raise ValueError("Causal prompt token aggregate changed")
    configuration = str(run["configuration"])
    pass_name = str(run["pass_id"])
    domain = str(run["domain"])
    loaded[configuration].setdefault(pass_name, {})[domain] = summary
    prompts[configuration].setdefault(pass_name, {})[domain] = prompt_summary
    sources.setdefault(pass_name, {}).setdefault(configuration, {})[domain] = source
    collect_run_identity(
        expected=expected,
        summary=summary,
        source=source,
        domain=domain,
        **identity_sets,
    )


def load_prompt_rows_subset(
    *,
    run_dir: Path,
    expected_question_ids: list[str],
    selected_question_ids: list[str],
    bounded: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    path = run_dir / PROMPT_ROWS_FILE
    rows = load_jsonl(path)
    row_ids = [str(row.get("question_id") or "") for row in rows]
    if len(row_ids) != len(set(row_ids)) or set(row_ids) != set(expected_question_ids):
        raise ValueError(f"Causal prompt rows do not match their source run: {run_dir}")
    selected = set(selected_question_ids)
    if not selected or not selected <= set(row_ids):
        raise ValueError(f"Causal prompt subset is invalid: {run_dir}")
    token_counts = {}
    budgets = {}
    for row in rows:
        question_id = str(row["question_id"])
        if question_id not in selected:
            continue
        token_count = row.get("memory_context_token_count")
        if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
            raise TypeError(f"Causal prompt row has invalid token count: {run_dir}")
        budget = nested_context_budget(row)
        validate_context_budget(budget, bounded=bounded, run_dir=run_dir)
        token_counts[question_id] = token_count
        budgets[question_id] = budget
    return (
        {
            "question_count": len(selected),
            "memory_context_tokens": token_counts,
            "mean_memory_context_tokens": mean(token_counts.values()),
            "context_budgets": budgets,
        },
        {"path": str(path.resolve()), "sha256": sha256_file(path)},
    )


def subset_reader_summary(
    *,
    run_dir: Path,
    full_summary: dict[str, Any],
    selected_question_ids: list[str],
) -> dict[str, Any]:
    selected = set(selected_question_ids)
    scores = {
        question_id: row
        for question_id, row in full_summary["scores"].items()
        if question_id in selected
    }
    rows = {
        str(row.get("question_id") or ""): row
        for row in load_jsonl(run_dir / "per_question.jsonl")
        if str(row.get("question_id") or "") in selected
    }
    if set(scores) != selected or set(rows) != selected:
        raise ValueError(f"Causal score subset is incomplete: {run_dir}")
    prompt_tokens = 0
    completion_tokens = 0
    for row in rows.values():
        usage = row.get("usage")
        if not isinstance(usage, dict):
            raise TypeError(f"Causal score row has no usage: {run_dir}")
        prompt_tokens += int(usage["prompt_tokens"])
        completion_tokens += int(usage["completion_tokens"])
    return {
        "question_count": len(selected),
        "correct_count": sum(bool(row["score_bool"]) for row in scores.values()),
        "accuracy": sum(bool(row["score_bool"]) for row in scores.values()) / len(selected),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "average_original_memory_tokens": mean(
            int(row["memory_context_original_token_count"]) for row in rows.values()
        ),
        "average_final_memory_tokens": mean(
            int(row["memory_context_token_count"]) for row in rows.values()
        ),
        "truncated_question_count": sum(
            bool(row["memory_context_was_truncated"]) for row in rows.values()
        ),
        "average_memory_query_seconds": mean(
            float(row["memory_query_duration_seconds"]) for row in rows.values()
        ),
        "model_cost_usd": 0.0,
        "reader_model": full_summary["reader_model"],
        "evaluator_model": full_summary["evaluator_model"],
        "reader_temperature": full_summary["reader_temperature"],
        "scores": scores,
    }


def causal_comparison(**kwargs: Any) -> dict[str, Any]:
    result = compare_holdout_configurations(**kwargs)
    result.pop("model_cost_usd_delta")
    return result


def strip_incomparable_costs(summary: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(summary)
    result["aggregate"].pop("total_model_cost_usd")
    result["aggregate"].pop("mean_model_cost_usd")
    for domain in result["domains"].values():
        domain.pop("total_model_cost_usd")
    for pass_summary in result["passes"].values():
        pass_summary["combined"].pop("model_cost_usd")
        for domain in pass_summary["domains"].values():
            domain.pop("model_cost_usd")
    result["cost_accounting"] = "existing source costs excluded; new spend reported separately"
    return result


def development_costs(
    plan: dict[str, Any],
    *,
    loaded: dict[str, dict[str, dict[str, dict[str, Any]]]],
) -> dict[str, Any]:
    by_configuration = {
        configuration: sum(
            loaded[configuration][pass_name][domain]["model_cost_usd"]
            for pass_name in loaded[configuration]
            for domain in DOMAINS
        )
        for configuration in NEW_DEVELOPMENT_CONFIGURATIONS
    }
    total = sum(by_configuration.values())
    maximum = plan["cost_budget"]["max_development_model_cost_usd"]
    limits = observed_cost_limits(
        loaded=loaded,
        configurations=NEW_DEVELOPMENT_CONFIGURATIONS,
        pilot_cost_per_question_run_usd=plan["cost_budget"]["pilot_cost_per_question_run_usd"],
        max_pilot_cost_multiplier=plan["cost_budget"]["max_pilot_cost_multiplier"],
        max_total_model_cost_usd=maximum,
    )
    return {
        "existing_source_model_cost_usd": 0.0,
        "existing_source_cost_status": "sunk and excluded",
        "by_new_configuration_model_cost_usd": by_configuration,
        "new_model_cost_usd": total,
        "max_new_model_cost_usd": maximum,
        **limits,
    }


def select_development_candidate(
    *,
    comparisons: dict[str, dict[str, Any]],
    factors: dict[str, dict[str, bool]],
    analysis: dict[str, Any],
    costs_within_budget: bool,
) -> dict[str, Any]:
    treatments = {}
    eligible = []
    for configuration in TREATMENT_CONFIGURATIONS:
        comparison = comparisons[f"{configuration}_vs_{BASELINE_CONFIGURATION}"]
        bootstrap = comparison["cluster_bootstrap"]
        inflation = comparison["memory_context_tokens"]["inflation"]
        checks = {
            "mean_accuracy_delta": bootstrap["mean_accuracy_delta"]
            >= analysis["minimum_mean_accuracy_delta"],
            "probability_positive": bootstrap["probability_positive"]
            >= analysis["minimum_probability_positive"],
            "majority_vote_delta": comparison["majority_vote_accuracy_delta"]
            >= analysis["minimum_majority_vote_delta"],
            "memory_token_inflation": inflation <= analysis["maximum_memory_token_inflation"],
            "domain_deltas": all(
                delta >= analysis["minimum_domain_delta"]
                for delta in comparison["domain_mean_accuracy_deltas"].values()
            ),
        }
        passes = all(checks.values())
        treatments[configuration] = {
            "checks": checks,
            "passes": passes,
            "mean_accuracy_delta": bootstrap["mean_accuracy_delta"],
            "probability_positive": bootstrap["probability_positive"],
            "majority_vote_accuracy_delta": comparison["majority_vote_accuracy_delta"],
            "memory_token_inflation": inflation,
            "active_factor_count": sum(factors[configuration].values()),
        }
        if passes:
            eligible.append(configuration)
    selected = None
    if costs_within_budget and eligible:
        best_delta = max(treatments[name]["mean_accuracy_delta"] for name in eligible)
        shortlist = [
            name
            for name in eligible
            if treatments[name]["mean_accuracy_delta"] >= best_delta - analysis["simplicity_margin"]
        ]
        selected = min(
            shortlist,
            key=lambda name: (
                treatments[name]["active_factor_count"],
                treatments[name]["memory_token_inflation"],
                name,
            ),
        )
    return {
        "outcome": "CONFIRM" if selected else "STOP_WITHOUT_CONFIRMATION",
        "selected_configuration": selected,
        "costs_within_budget": costs_within_budget,
        "treatments": treatments,
    }


def summarize_factorial_effects(loaded: dict[str, Any]) -> dict[str, Any]:
    histories = {name: question_histories(loaded[name]) for name in CAUSAL_CONFIGURATIONS}
    keys = set(histories[BASELINE_CONFIGURATION])
    if any(set(items) != keys for items in histories.values()):
        raise ValueError("Causal factorial configurations differ in question coverage")
    effects = {"neighbor_main": [], "refinement_main": [], "interaction": []}
    domains = {name: {domain: [] for domain in DOMAINS} for name in effects}
    for key in sorted(keys):
        rates = {name: mean(histories[name][key]["scores"]) for name in CAUSAL_CONFIGURATIONS}
        values = {
            "neighbor_main": (
                (rates[NEIGHBOR_CONFIGURATION] - rates[BASELINE_CONFIGURATION])
                + (rates[BUNDLE_CONFIGURATION] - rates[REFINEMENT_CONFIGURATION])
            )
            / 2.0,
            "refinement_main": (
                (rates[REFINEMENT_CONFIGURATION] - rates[BASELINE_CONFIGURATION])
                + (rates[BUNDLE_CONFIGURATION] - rates[NEIGHBOR_CONFIGURATION])
            )
            / 2.0,
            "interaction": (
                rates[BUNDLE_CONFIGURATION]
                - rates[NEIGHBOR_CONFIGURATION]
                - rates[REFINEMENT_CONFIGURATION]
                + rates[BASELINE_CONFIGURATION]
            ),
        }
        for name, value in values.items():
            effects[name].append(value)
            domains[name][key[0]].append(value)
    return {
        name: {
            "cluster_bootstrap": cluster_bootstrap(values),
            "domain_mean_effects": {
                domain: mean(domain_values) for domain, domain_values in domains[name].items()
            },
        }
        for name, values in effects.items()
    }


def build_causal_confirmation_report(
    *,
    plan: dict[str, Any],
    plan_path: Path,
) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    if load_json(plan_path) != plan:
        raise ValueError("Causal confirmation plan path does not match the loaded plan")
    runs = require_causal_confirmation_plan(plan)
    loaded, prompts, sources, runtime = load_confirmation_runs(runs)
    baseline = BASELINE_CONFIGURATION
    candidate = str(plan["selected_configuration"])
    configurations = {
        name: summarize_holdout_configuration(passes=loaded[name], prompt_passes=prompts[name])
        for name in (baseline, candidate)
    }
    comparison = compare_holdout_configurations(
        candidate=loaded[candidate],
        baseline=loaded[baseline],
        candidate_prompts=prompts[candidate],
        baseline_prompts=prompts[baseline],
        candidate_summary=configurations[candidate],
        baseline_summary=configurations[baseline],
    )
    budget = summarize_bounded_budget(prompts[candidate])
    costs = confirmation_costs(plan, loaded=loaded)
    decision = confirmation_decision(
        comparison=comparison,
        analysis=plan["analysis"],
        costs_within_budget=costs["within_budget"],
    )
    valid = costs["within_budget"]
    return {
        "schema_version": CONFIRMATION_REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if valid else "FAIL",
        "promotion_eligible": valid and decision["outcome"] == "GO",
        "claim_boundary": (
            "Fresh 96-question, question-level confirmation on shared small-tier domain "
            "haystacks. This report does not establish leaderboard performance."
        ),
        "protocol": plan["protocol"],
        "analysis": plan["analysis"],
        "runtime": runtime,
        "configurations": configurations,
        "comparison": comparison,
        "bounded_context_budget": budget,
        "costs": costs,
        "decision": decision,
        "source_artifacts": {
            "confirmation_plan": {"path": str(plan_path), "sha256": sha256_file(plan_path)},
            "runs": sources,
        },
    }


def load_confirmation_runs(
    runs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    configurations = sorted({str(run["configuration"]) for run in runs})
    loaded: dict[str, Any] = {name: {} for name in configurations}
    prompts: dict[str, Any] = {name: {} for name in configurations}
    sources: dict[str, Any] = {}
    identity_sets = shared_identity_sets()
    for run in runs:
        expected = expected_run_config(run["command"])
        run_dir = Path(run["output_dir"])
        summary, source = load_reader_run(run_dir=run_dir, expected=expected)
        bounded = run["configuration"] != BASELINE_CONFIGURATION
        prompt_summary, prompt_source = load_prompt_rows_subset(
            run_dir=run_dir,
            expected_question_ids=expected["question_ids"],
            selected_question_ids=expected["question_ids"],
            bounded=bounded,
        )
        source["artifacts"][PROMPT_ROWS_FILE] = prompt_source
        store_loaded_run(
            run=run,
            expected=expected,
            summary=summary,
            prompt_summary=prompt_summary,
            source=source,
            loaded=loaded,
            prompts=prompts,
            sources=sources,
            identity_sets=identity_sets,
        )
    return loaded, prompts, sources, require_shared_run_identity(**identity_sets)


def confirmation_costs(
    plan: dict[str, Any],
    *,
    loaded: dict[str, dict[str, dict[str, dict[str, Any]]]],
) -> dict[str, Any]:
    by_configuration = {
        configuration: sum(
            loaded[configuration][pass_name][domain]["model_cost_usd"]
            for pass_name in loaded[configuration]
            for domain in DOMAINS
        )
        for configuration in loaded
    }
    total = sum(by_configuration.values())
    maximum = plan["cost_budget"]["max_total_model_cost_usd"]
    limits = observed_cost_limits(
        loaded=loaded,
        configurations=tuple(loaded),
        pilot_cost_per_question_run_usd=plan["cost_budget"]["pilot_cost_per_question_run_usd"],
        max_pilot_cost_multiplier=plan["cost_budget"]["max_pilot_cost_multiplier"],
        max_total_model_cost_usd=maximum,
    )
    return {
        "by_configuration_model_cost_usd": by_configuration,
        "total_model_cost_usd": total,
        "max_total_model_cost_usd": maximum,
        **limits,
    }


def observed_cost_limits(
    *,
    loaded: dict[str, dict[str, dict[str, dict[str, Any]]]],
    configurations: tuple[str, ...],
    pilot_cost_per_question_run_usd: float,
    max_pilot_cost_multiplier: float,
    max_total_model_cost_usd: float,
) -> dict[str, Any]:
    runs = []
    for configuration in configurations:
        for pass_name, domains in loaded[configuration].items():
            for domain in DOMAINS:
                summary = domains[domain]
                question_count = int(summary["question_count"])
                if question_count <= 0:
                    raise ValueError("Causal cost accounting requires non-empty runs")
                model_cost = float(summary["model_cost_usd"])
                runs.append(
                    {
                        "pass_id": pass_name,
                        "configuration": configuration,
                        "domain": domain,
                        "model_cost_usd": model_cost,
                        "question_count": question_count,
                        "cost_per_question_run_usd": model_cost / question_count,
                    }
                )
    total = sum(float(run["model_cost_usd"]) for run in runs)
    maximum_per_question = pilot_cost_per_question_run_usd * max_pilot_cost_multiplier
    maximum_observed = max(float(run["cost_per_question_run_usd"]) for run in runs)
    within_total = total <= max_total_model_cost_usd
    within_per_run = maximum_observed <= maximum_per_question
    return {
        "runs": runs,
        "max_run_cost_per_question_usd": maximum_per_question,
        "maximum_observed_run_cost_per_question_usd": maximum_observed,
        "within_total_budget": within_total,
        "within_per_run_budget": within_per_run,
        "within_budget": within_total and within_per_run,
    }


def confirmation_decision(
    *,
    comparison: dict[str, Any],
    analysis: dict[str, Any],
    costs_within_budget: bool,
) -> dict[str, Any]:
    bootstrap = comparison["cluster_bootstrap"]
    checks = {
        "mean_accuracy_delta": bootstrap["mean_accuracy_delta"]
        >= analysis["minimum_mean_accuracy_delta"],
        "probability_positive": bootstrap["probability_positive"]
        >= analysis["minimum_probability_positive"],
        "confidence_interval_lower": bootstrap["confidence_interval"]["lower"]
        >= analysis["minimum_confidence_interval_lower"],
        "memory_token_inflation": comparison["memory_context_tokens"]["inflation"]
        <= analysis["maximum_memory_token_inflation"],
        "domain_deltas": all(
            delta >= analysis["minimum_domain_delta"]
            for delta in comparison["domain_mean_accuracy_deltas"].values()
        ),
        "costs_within_budget": costs_within_budget,
    }
    if all(checks.values()):
        outcome = "GO"
        reason = "candidate crossed every preregistered confirmation gate"
    elif (
        bootstrap["mean_accuracy_delta"] <= 0
        or bootstrap["probability_positive"] <= analysis["no_go_max_probability_positive"]
    ):
        outcome = "NO-GO"
        reason = "candidate crossed the preregistered rejection boundary"
    else:
        outcome = analysis["otherwise"]
        reason = "candidate crossed neither promotion nor rejection boundary"
    return {"outcome": outcome, "reason": reason, "checks": checks}
