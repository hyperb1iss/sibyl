from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks import (
    longmemeval_v2_causal_ablation as causal,
)
from benchmarks import (
    longmemeval_v2_causal_ablation_report as report,
)
from benchmarks.longmemeval_v2_reader_report import expected_run_config

EXPECTED_DEVELOPMENT_RUNS = (
    causal.PASS_COUNT * len(causal.DOMAINS) * len(causal.NEW_DEVELOPMENT_CONFIGURATIONS)
)
EXPECTED_EXISTING_RUNS = causal.PASS_COUNT * len(causal.DOMAINS) * 2
EXPECTED_CONFIRMATION_TEMPLATES = (
    causal.PASS_COUNT * len(causal.DOMAINS) * len(causal.CAUSAL_CONFIGURATIONS)
)
EXPECTED_CONFIRMATION_RUNS = causal.PASS_COUNT * len(causal.DOMAINS) * 2
BASE_CONTEXT_ITEMS = 8
EXPANDED_CONTEXT_ITEMS = 10
NEIGHBOR_STITCH_ITEMS = 2


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")


def _source_command(
    tmp_path: Path,
    *,
    domain: str,
    configuration: str,
    question_ids: list[str],
    pass_index: int,
) -> list[str]:
    factors = (
        causal.CONFIGURATION_FACTORS[causal.BASELINE_CONFIGURATION]
        if configuration == "baseline_fixed_reader"
        else causal.CONFIGURATION_FACTORS[causal.BUNDLE_CONFIGURATION]
    )
    command = [
        "uv",
        "run",
        "python",
        "benchmarks/longmemeval_v2_official.py",
        "--official-repo",
        str(tmp_path / "official"),
        "--data-root",
        str(tmp_path / "data"),
        "--domain",
        domain,
        "--tier",
        "small",
        "--output-dir",
        str(tmp_path / "source" / f"pass_{pass_index:02d}" / configuration / domain),
        "--load-memory-dir",
        str(tmp_path / "memory" / domain),
        "--reader-model",
        "Qwen/Qwen3.5-9B",
        "--reader-temperature",
        "0.6",
        "--reader-top-p",
        "0.95",
        "--reader-top-k",
        "20",
        "--memory-context-max-tokens",
        "200000",
        "--question-ids",
        *question_ids,
        "--search-limit",
        "12",
        "--max-context-items",
        "10" if factors["neighbor_stitching"] else "8",
        "--max-chunks-per-trajectory",
        "8",
        "--neighbor-stitch-items",
        "2" if factors["neighbor_stitching"] else "0",
        "--neighbor-stitch-span",
        "1" if factors["neighbor_stitching"] else "0",
        "--state-part-completion-items",
        "0",
        "--reader-base-url",
        "https://openrouter.ai/api/v1",
        "--reader-api-key-env",
        "OPENROUTER_API_KEY",
        "--reader-max-concurrent-requests",
        "16",
        "--max-completion-tokens",
        "20000",
        "--timeout-seconds",
        "43200",
        "--evaluator-model",
        "openai/gpt-5.2",
        "--evaluator-base-url",
        "https://openrouter.ai/api/v1",
        "--evaluator-api-key-env",
        "OPENROUTER_API_KEY",
        "--evaluator-reasoning-effort",
        "medium",
        "--evaluator-max-completion-tokens",
        "4096",
        "--evaluator-timeout-seconds",
        "43200",
        "--prompt-build-max-workers",
        "1",
        "--context-expansion-max-ratio",
        "0.0" if configuration == "baseline_fixed_reader" else "1.2",
        (
            "--state-part-refinement"
            if factors["state_part_refinement"]
            else "--no-state-part-refinement"
        ),
    ]
    if causal.PASS_SEEDS[pass_index - 1] is not None:
        command.extend(["--shuffle-questions-seed", str(causal.PASS_SEEDS[pass_index - 1])])
    return command


def _source_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    monkeypatch.setattr(causal, "require_reader_holdout_plan", lambda _plan: [])
    rows = []
    holdout_ids = {}
    holdout_types = {}
    excluded_ids = {}
    for domain in causal.DOMAINS:
        domain_rows = []
        for index in range(120):
            question_id = f"{domain}-{index:03d}"
            question_type = "procedure" if index % 3 else "errors-gotchas"
            domain_rows.append(
                {
                    "id": question_id,
                    "domain": domain,
                    "question_type": question_type,
                    "question": f"question sentinel {index}",
                    "answer": f"answer sentinel {index}",
                }
            )
        rows.extend(domain_rows)
        excluded_ids[domain] = [row["id"] for row in domain_rows[:16]]
        selected = domain_rows[16:64]
        holdout_ids[domain] = [row["id"] for row in selected]
        holdout_types[domain] = {row["id"]: row["question_type"] for row in selected}
    questions_path = tmp_path / "data" / "questions.jsonl"
    _write_jsonl(questions_path, rows)
    selection = {
        "excluded_question_ids_by_domain": excluded_ids,
        "question_ids_by_domain": holdout_ids,
        "question_types_by_domain": holdout_types,
    }
    runs = []
    for pass_index in range(1, causal.PASS_COUNT + 1):
        for configuration in ("baseline_fixed_reader", "winner_token_bounded_1_2x"):
            for domain in causal.DOMAINS:
                command = _source_command(
                    tmp_path,
                    domain=domain,
                    configuration=configuration,
                    question_ids=holdout_ids[domain],
                    pass_index=pass_index,
                )
                runs.append(
                    {
                        "pass_id": causal.pass_id(pass_index),
                        "pass_index": pass_index,
                        "configuration": configuration,
                        "domain": domain,
                        "shuffle_questions_seed": causal.PASS_SEEDS[pass_index - 1],
                        "output_dir": expected_run_config(command)["output_dir"],
                        "command": command,
                    }
                )
    holdout_plan = {
        "selection": selection,
        "runs": runs,
        "source_artifacts": {
            "questions": {
                "path": str(questions_path),
                "sha256": causal.sha256_file(questions_path),
            }
        },
    }
    holdout_plan_path = tmp_path / "holdout-plan.json"
    _write_json(holdout_plan_path, holdout_plan)
    holdout_report = {
        "schema_version": causal.HOLDOUT_REPORT_SCHEMA_VERSION,
        "status": "PASS",
        "decision": {"valid_experiment": True},
        "protocol": {"question_count": 96, "passes_per_configuration": 5},
        "configurations": {"baseline": {}, "candidate": {}, "bounded": {}},
        "costs": {"total_model_cost_usd": 9.98300569},
        "source_artifacts": {
            "holdout_plan": {
                "path": str(holdout_plan_path),
                "sha256": causal.sha256_file(holdout_plan_path),
            }
        },
    }
    holdout_report_path = tmp_path / "holdout-report.json"
    _write_json(holdout_report_path, holdout_report)
    monkeypatch.setattr(
        causal,
        "build_reader_holdout_report",
        lambda **_kwargs: holdout_report,
    )
    return holdout_plan, holdout_plan_path, holdout_report, holdout_report_path


def test_causal_plan_freezes_disjoint_development_and_confirmation_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout_plan, holdout_plan_path, holdout_report, holdout_report_path = _source_inputs(
        tmp_path, monkeypatch
    )
    plan = causal.build_causal_ablation_plan(
        holdout_plan=holdout_plan,
        holdout_plan_path=holdout_plan_path,
        holdout_report=holdout_report,
        holdout_report_path=holdout_report_path,
        output_root=tmp_path / "causal",
    )

    assert len(plan["existing_runs"]) == EXPECTED_EXISTING_RUNS
    assert len(plan["runs"]) == EXPECTED_DEVELOPMENT_RUNS
    assert len(plan["confirmation_templates"]) == EXPECTED_CONFIRMATION_TEMPLATES
    assert plan["cost_budget"]["estimated_development_model_cost_usd"] == pytest.approx(
        2.773057136111111
    )
    for domain in causal.DOMAINS:
        development = set(plan["development_selection"]["question_ids_by_domain"][domain])
        confirmation = set(plan["confirmation_selection"]["question_ids_by_domain"][domain])
        consumed = set(holdout_plan["selection"]["question_ids_by_domain"][domain])
        assert len(development) == causal.DEVELOPMENT_QUESTIONS_PER_DOMAIN
        assert len(confirmation) == causal.CONFIRMATION_QUESTIONS_PER_DOMAIN
        assert development <= consumed
        assert not confirmation & consumed
    serialized = json.dumps(plan)
    assert "question sentinel" not in serialized
    assert "answer sentinel" not in serialized

    for run in plan["runs"]:
        expected = expected_run_config(run["command"])
        assert len(expected["question_ids"]) == causal.DEVELOPMENT_QUESTIONS_PER_DOMAIN
        assert expected["context_expansion_max_ratio"] == causal.TOKEN_BOUNDED_MAX_RATIO
        if run["configuration"] == causal.NEIGHBOR_CONFIGURATION:
            assert expected["max_context_items"] == EXPANDED_CONTEXT_ITEMS
            assert expected["neighbor_stitch_items"] == NEIGHBOR_STITCH_ITEMS
            assert expected["state_part_refinement"] is False
        else:
            assert expected["max_context_items"] == BASE_CONTEXT_ITEMS
            assert expected["neighbor_stitch_items"] == 0
            assert expected["state_part_refinement"] is True

    tampered = json.loads(json.dumps(plan))
    tampered["development_selection"]["seed"] += 1
    with pytest.raises(ValueError, match="development selection changed"):
        causal.require_causal_ablation_plan(tampered)

    source_runs = causal.source_run_map(json.loads(json.dumps(holdout_plan))["runs"])
    winner = source_runs[("pass_01", "winner_token_bounded_1_2x", "enterprise")]
    winner["command"] = causal.replace_option(winner["command"], "--search-limit", "13")
    with pytest.raises(ValueError, match="differ outside factors"):
        causal.require_factorial_source_isolation(source_runs)

    source_runs = causal.source_run_map(json.loads(json.dumps(holdout_plan))["runs"])
    winner = source_runs[("pass_01", "winner_token_bounded_1_2x", "enterprise")]
    winner["command"].extend(("--unknown-retrieval-knob", "1"))
    with pytest.raises(ValueError, match="raw commands differ outside factors"):
        causal.require_factorial_source_isolation(source_runs)


def _comparison(
    delta: float,
    probability: float,
    *,
    majority: float = 0.0,
    inflation: float = 0.1,
) -> dict[str, Any]:
    return {
        "cluster_bootstrap": {
            "mean_accuracy_delta": delta,
            "probability_positive": probability,
        },
        "majority_vote_accuracy_delta": majority,
        "memory_context_tokens": {"inflation": inflation},
        "domain_mean_accuracy_deltas": {"enterprise": delta, "web": delta},
    }


def test_development_selection_prefers_simpler_candidate_within_margin() -> None:
    comparisons: dict[str, dict[str, Any]] = {
        f"{causal.NEIGHBOR_CONFIGURATION}_vs_{causal.BASELINE_CONFIGURATION}": _comparison(
            0.04, 0.8, inflation=0.15
        ),
        f"{causal.REFINEMENT_CONFIGURATION}_vs_{causal.BASELINE_CONFIGURATION}": _comparison(
            0.035, 0.8, inflation=0.0
        ),
        f"{causal.BUNDLE_CONFIGURATION}_vs_{causal.BASELINE_CONFIGURATION}": _comparison(
            0.045, 0.8, inflation=0.18
        ),
    }

    decision = report.select_development_candidate(
        comparisons=comparisons,
        factors=causal.CONFIGURATION_FACTORS,
        analysis=causal.causal_analysis(),
        costs_within_budget=True,
    )

    assert decision["outcome"] == "CONFIRM"
    assert decision["selected_configuration"] == causal.REFINEMENT_CONFIGURATION


def test_development_selection_stops_when_no_treatment_is_eligible() -> None:
    comparisons: dict[str, dict[str, Any]] = {
        f"{configuration}_vs_{causal.BASELINE_CONFIGURATION}": _comparison(-0.01, 0.3)
        for configuration in causal.TREATMENT_CONFIGURATIONS
    }

    decision = report.select_development_candidate(
        comparisons=comparisons,
        factors=causal.CONFIGURATION_FACTORS,
        analysis=causal.causal_analysis(),
        costs_within_budget=True,
    )

    assert decision["outcome"] == "STOP_WITHOUT_CONFIRMATION"
    assert decision["selected_configuration"] is None


def test_factorial_effects_isolate_neighbor_main_effect() -> None:
    loaded = {configuration: {} for configuration in causal.CAUSAL_CONFIGURATIONS}
    scores = {
        causal.BASELINE_CONFIGURATION: False,
        causal.NEIGHBOR_CONFIGURATION: True,
        causal.REFINEMENT_CONFIGURATION: False,
        causal.BUNDLE_CONFIGURATION: True,
    }
    for configuration, score in scores.items():
        for pass_index in range(1, causal.PASS_COUNT + 1):
            loaded[configuration][causal.pass_id(pass_index)] = {
                domain: {
                    "scores": {
                        f"{domain}-question": {
                            "question_type": "procedure",
                            "score_bool": score,
                        }
                    }
                }
                for domain in causal.DOMAINS
            }

    effects = report.summarize_factorial_effects(loaded)

    assert effects["neighbor_main"]["cluster_bootstrap"]["mean_accuracy_delta"] == 1.0
    assert effects["refinement_main"]["cluster_bootstrap"]["mean_accuracy_delta"] == 0.0
    assert effects["interaction"]["cluster_bootstrap"]["mean_accuracy_delta"] == 0.0


def test_confirmation_plan_is_bound_to_frozen_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout_plan, holdout_plan_path, holdout_report, holdout_report_path = _source_inputs(
        tmp_path, monkeypatch
    )
    plan = causal.build_causal_ablation_plan(
        holdout_plan=holdout_plan,
        holdout_plan_path=holdout_plan_path,
        holdout_report=holdout_report,
        holdout_report_path=holdout_report_path,
        output_root=tmp_path / "causal",
    )
    plan_path = tmp_path / "causal-plan.json"
    _write_json(plan_path, plan)
    causal_report = {
        "schema_version": causal.CAUSAL_REPORT_SCHEMA_VERSION,
        "status": "PASS",
        "decision": {"selected_configuration": causal.NEIGHBOR_CONFIGURATION},
        "source_artifacts": {
            "causal_plan": {
                "path": str(plan_path),
                "sha256": causal.sha256_file(plan_path),
            }
        },
    }
    report_path = tmp_path / "causal-report.json"
    _write_json(report_path, causal_report)
    monkeypatch.setattr(
        causal,
        "rebuild_causal_ablation_report",
        lambda **_kwargs: causal_report,
    )

    confirmation = causal.build_causal_confirmation_plan(
        causal_plan=plan,
        causal_plan_path=plan_path,
        causal_report=causal_report,
        causal_report_path=report_path,
    )

    assert len(confirmation["runs"]) == EXPECTED_CONFIRMATION_RUNS
    assert confirmation["selected_configuration"] == causal.NEIGHBOR_CONFIGURATION
    assert {run["configuration"] for run in confirmation["runs"]} == {
        causal.BASELINE_CONFIGURATION,
        causal.NEIGHBOR_CONFIGURATION,
    }

    confirmation_path = tmp_path / "confirmation-plan.json"
    _write_json(confirmation_path, confirmation)
    assert len(causal.require_causal_confirmation_plan(confirmation)) == EXPECTED_CONFIRMATION_RUNS

    tampered = json.loads(json.dumps(confirmation))
    tampered["runs"][0]["command"].append("--rogue")
    with pytest.raises(ValueError, match="commands changed"):
        causal.require_causal_confirmation_plan(tampered)

    tampered = json.loads(json.dumps(confirmation))
    tampered["analysis"]["minimum_mean_accuracy_delta"] = 0.0
    with pytest.raises(ValueError, match="analysis changed"):
        causal.require_causal_confirmation_plan(tampered)

    tampered_report = json.loads(json.dumps(causal_report))
    tampered_report["decision"]["selected_configuration"] = causal.REFINEMENT_CONFIGURATION
    with pytest.raises(ValueError, match="changed after scoring"):
        causal.require_causal_report_binding(tampered_report, causal_plan_path=plan_path)


def test_confirmation_decision_applies_frozen_go_and_no_go_rules() -> None:
    analysis = causal.causal_analysis()["confirmation"]
    comparison = _comparison(0.04, 0.9, inflation=0.2)
    comparison["cluster_bootstrap"]["confidence_interval"] = {
        "lower": -0.01,
        "upper": 0.08,
    }
    assert (
        report.confirmation_decision(
            comparison=comparison,
            analysis=analysis,
            costs_within_budget=True,
        )["outcome"]
        == "GO"
    )

    comparison["cluster_bootstrap"]["mean_accuracy_delta"] = -0.01
    assert (
        report.confirmation_decision(
            comparison=comparison,
            analysis=analysis,
            costs_within_budget=True,
        )["outcome"]
        == "NO-GO"
    )


def test_report_costs_reject_per_run_spike_below_total_cap() -> None:
    loaded = {
        configuration: {
            "pass_01": {
                domain: {"model_cost_usd": 0.001, "question_count": 1} for domain in causal.DOMAINS
            }
        }
        for configuration in causal.NEW_DEVELOPMENT_CONFIGURATIONS
    }
    loaded[causal.NEIGHBOR_CONFIGURATION]["pass_01"]["enterprise"]["model_cost_usd"] = 0.06
    costs = report.development_costs(
        {
            "cost_budget": {
                "max_development_model_cost_usd": 3.0,
                "pilot_cost_per_question_run_usd": 0.01,
                "max_pilot_cost_multiplier": 5.0,
            }
        },
        loaded=loaded,
    )

    assert costs["within_total_budget"] is True
    assert costs["within_per_run_budget"] is False
    assert costs["within_budget"] is False


def test_causal_report_loaders_accept_paid_artifact_shapes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    question_ids = ["question-1", "question-2"]
    final_memory_tokens = 110
    prompt_tokens_per_question = 100
    prompt_budget = {
        "enabled": True,
        "max_ratio": causal.TOKEN_BOUNDED_MAX_RATIO,
        "base_item_count": 1,
        "unbounded_item_count": 2,
        "final_item_count": 2,
        "base_token_count": 100,
        "max_token_count": 120,
        "unbounded_token_count": 110,
        "final_token_count": 110,
        "dropped_item_count": 0,
        "dropped_chunk_keys": [],
        "binding": False,
    }
    _write_jsonl(
        run_dir / report.PROMPT_ROWS_FILE,
        [
            {
                "question_id": question_id,
                "memory_context_token_count": final_memory_tokens,
                "memory_post_query_metadata": {
                    "search_metadata": {
                        "adapter_assembly": {"context_expansion_budget": prompt_budget}
                    }
                },
            }
            for question_id in question_ids
        ],
    )
    _write_jsonl(
        run_dir / "per_question.jsonl",
        [
            {
                "question_id": question_id,
                "usage": {
                    "prompt_tokens": prompt_tokens_per_question,
                    "completion_tokens": 10,
                },
                "memory_context_original_token_count": 100,
                "memory_context_token_count": final_memory_tokens,
                "memory_context_was_truncated": False,
                "memory_query_duration_seconds": 0.25,
            }
            for question_id in question_ids
        ],
    )
    full_summary = {
        "scores": {
            question_id: {
                "question_type": "procedure",
                "score_bool": index == 0,
            }
            for index, question_id in enumerate(question_ids)
        },
        "reader_model": "qwen/qwen3.5-9b",
        "evaluator_model": "openai/gpt-5.2",
        "reader_temperature": 0.6,
    }

    prompt_summary, _ = report.load_prompt_rows_subset(
        run_dir=run_dir,
        expected_question_ids=question_ids,
        selected_question_ids=question_ids,
        bounded=True,
    )
    summary = report.subset_reader_summary(
        run_dir=run_dir,
        full_summary=full_summary,
        selected_question_ids=question_ids,
    )

    assert prompt_summary["mean_memory_context_tokens"] == final_memory_tokens
    assert summary["question_count"] == len(question_ids)
    assert summary["correct_count"] == 1
    assert summary["prompt_tokens"] == prompt_tokens_per_question * len(question_ids)
    assert summary["average_final_memory_tokens"] == final_memory_tokens


def test_ci_runs_benchmark_gate_for_benchmark_only_changes() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text()
    runtime_case = next(
        block.split("esac", maxsplit=1)[0]
        for block in workflow.split('case "$file" in')[1:]
        if "runtime_changed=true" in block.split("esac", maxsplit=1)[0]
    )

    assert "benchmarks/*" in runtime_case
