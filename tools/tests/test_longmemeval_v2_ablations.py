from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

EXPECTED_ARM_COUNT = 5
EXPECTED_REPRESENTATION_COUNT = 3
EXPECTED_READER_COMMAND_COUNT = 6
EXPECTED_PROVENANCE_ATTEMPTS = 2
EXPECTED_MATCHED_CONTEXT_TOKENS = 250
EXPECTED_EMBEDDING_COST_USD = 0.03
EXPECTED_NEIGHBOR_STITCH_ITEMS = 2
EXPECTED_NEIGHBOR_STITCH_SPAN = 1
EXPECTED_STATE_PART_COMPLETION_ITEMS = 2
EXPECTED_CONTEXT_EXPANSION_MAX_RATIO = 1.2
EXPECTED_WINNER_CONTEXT_ITEMS = 10
EXPECTED_READER_REPORT_COST_USD = 0.44
EXPECTED_READER_REPORT_CORRECT = 3
EXPECTED_READER_REPORT_ACCURACY = 0.75
EXPECTED_READER_REPORT_ACCURACY_DELTA = 0.25
EXPECTED_READER_REPORT_RECOVERIES = 2
EXPECTED_REPLICATION_RUN_COUNT = 20
EXPECTED_REPLICATION_PASS_COUNT = 5
EXPECTED_REPLICATION_WAVE_RUNS = 4
EXPECTED_HOLDOUT_RUN_COUNT = 30
EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN = 48
EXPECTED_PRIMARY_STRATUM_COUNT = 45
EXPECTED_SOURCE_QUESTION_COUNT = 62
EXPECTED_BOOTSTRAP_SAMPLES = 20_000
STANDARD_CONTEXT_TOKENS = 200_000


def _load_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_ablations.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_ablations", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_official_runner_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_official.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_official", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_replication_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_reader_replication.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_reader_replication", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_replication_report_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_reader_replication_report.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_reader_replication_report", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_holdout_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_reader_holdout.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_reader_holdout", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_holdout_report_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_reader_holdout_report.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_reader_holdout_report", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ablation_plan_freezes_five_arms_and_three_reader_configs(tmp_path: Path) -> None:
    module = _load_module()
    data_root, slice_path = _write_inputs(tmp_path)
    output_root = tmp_path / "output"
    official_repo = tmp_path / "official"
    official_repo.mkdir()

    plan = module.build_experiment_plan(
        data_root=data_root,
        official_repo=official_repo,
        output_root=output_root,
        slice_path=slice_path,
        api_url="http://127.0.0.1:3434/api",
        tier="small",
        allow_localhost=True,
        query_workers=4,
    )
    assert len(plan["representations"]) == EXPECTED_REPRESENTATION_COUNT
    assert len(plan["retrieval_arms"]) == EXPECTED_ARM_COUNT
    assert plan["reader_phase"]["configurations"] == list(module.READER_CONFIGURATIONS)
    assert plan["slice"]["record_sha256"] == module.sha256_json(module.load_json(slice_path))
    assert len(plan["reader_phase"]["baseline_commands"]) == len(module.DOMAINS)
    assert plan["cost_accounting"]["basis"] == "provider-reported usage and cost only"
    assert plan["integrity_contract"]["retrieval_rows_contain_answers"] is False
    serialized = json.dumps(plan).casefold()
    assert '"answer":' not in serialized
    assert "gold-web-sentinel" not in serialized
    assert "gold-enterprise-sentinel" not in serialized
    assert all(
        "--skip-evaluation" in build["command"]
        for representation in plan["representations"]
        for build in representation["memory_builds"].values()
    )
    assert all(
        "--checkpoint-dir" in build["command"]
        for representation in plan["representations"]
        for build in representation["memory_builds"].values()
    )
    assert all(
        "--question-ids" in build["command"]
        for representation in plan["representations"]
        for build in representation["memory_builds"].values()
    )
    for representation in plan["representations"]:
        for domain, build in representation["memory_builds"].items():
            question_id_index = build["command"].index("--question-ids")
            output_index = build["command"].index("--output-dir")
            assert build["command"][question_id_index + 1 : output_index] == [f"q-{domain}"]
    assert all(
        run["command"].count("retrieve") == 1
        for arm in plan["retrieval_arms"]
        for run in arm["retrieval_runs"].values()
    )


def test_retrieval_resume_skips_completed_questions_and_hides_gold(tmp_path: Path) -> None:
    module = _load_module()
    output_path = tmp_path / "per_question.jsonl"
    completed = {
        "question_id": "q1",
        "question_type": "static",
        "memory_context": [],
    }
    output_path.write_text(json.dumps(completed) + "\n", encoding="utf-8")
    memory = _FakeMemory()
    questions = [
        {
            "id": "q1",
            "question_type": "static",
            "question": "first question",
            "image": None,
            "answer": "gold one",
        },
        {
            "id": "q2",
            "question_type": "dynamic",
            "question": "second question",
            "image": "question.png",
            "answer": "gold two",
        },
    ]

    records = module.execute_retrieval(
        memory,
        questions,
        output_path=output_path,
        max_workers=2,
    )

    assert memory.queries == [("second question", "question.png")]
    assert memory.contexts == [
        {"question_item": {"question": "second question", "image": "question.png"}}
    ]
    assert [record["question_id"] for record in records] == ["q1", "q2"]
    assert "gold one" not in output_path.read_text(encoding="utf-8")
    assert "gold two" not in output_path.read_text(encoding="utf-8")


def test_retrieve_cli_overrides_context_assembly_without_changing_named_arm() -> None:
    module = _load_module()
    args = module.parse_args(
        [
            "retrieve",
            "--data-root",
            "data",
            "--official-repo",
            "official",
            "--memory-dir",
            "memory",
            "--output-dir",
            "output",
            "--domain",
            "web",
            "--arm",
            "trajectory_18k",
            "--neighbor-stitch-items",
            str(EXPECTED_NEIGHBOR_STITCH_ITEMS),
            "--neighbor-stitch-span",
            str(EXPECTED_NEIGHBOR_STITCH_SPAN),
            "--state-part-completion-items",
            str(EXPECTED_STATE_PART_COMPLETION_ITEMS),
            "--state-part-refinement",
            "--context-expansion-max-ratio",
            str(EXPECTED_CONTEXT_EXPANSION_MAX_RATIO),
        ]
    )

    arm = module.retrieval_arm_from_args(args)

    assert arm["name"] == "trajectory_18k"
    assert arm["neighbor_stitch_items"] == EXPECTED_NEIGHBOR_STITCH_ITEMS
    assert arm["neighbor_stitch_span"] == EXPECTED_NEIGHBOR_STITCH_SPAN
    assert arm["state_part_completion_items"] == EXPECTED_STATE_PART_COMPLETION_ITEMS
    assert arm["state_part_refinement"] is True
    assert arm["context_expansion_max_ratio"] == EXPECTED_CONTEXT_EXPANSION_MAX_RATIO
    assert module.arm_by_name("trajectory_18k")["neighbor_stitch_items"] == 0
    assert module.arm_by_name("trajectory_18k")["state_part_completion_items"] == 0
    assert module.arm_by_name("trajectory_18k")["state_part_refinement"] is False


def test_retrieve_cli_defaults_context_expansion_budget_to_disabled() -> None:
    module = _load_module()
    args = module.parse_args(
        [
            "retrieve",
            "--data-root",
            "data",
            "--official-repo",
            "official",
            "--memory-dir",
            "memory",
            "--output-dir",
            "output",
            "--domain",
            "web",
            "--arm",
            "trajectory_18k",
        ]
    )

    arm = module.retrieval_arm_from_args(args)

    assert arm["context_expansion_max_ratio"] == 0.0


def test_reader_command_round_trips_state_part_overrides_through_official_runner(
    tmp_path: Path,
) -> None:
    module = _load_module()
    official_runner = _load_official_runner_module()
    data_root, slice_path = _write_inputs(tmp_path)
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    plan = module.build_experiment_plan(
        data_root=data_root,
        official_repo=official_repo,
        output_root=tmp_path / "output",
        slice_path=slice_path,
        api_url="http://127.0.0.1:3434/api",
        tier="small",
        allow_localhost=True,
        query_workers=4,
    )
    arm = {
        **module.arm_by_name("trajectory_18k"),
        "max_context_items": 10,
        "neighbor_stitch_items": EXPECTED_NEIGHBOR_STITCH_ITEMS,
        "neighbor_stitch_span": EXPECTED_NEIGHBOR_STITCH_SPAN,
        "state_part_completion_items": EXPECTED_STATE_PART_COMPLETION_ITEMS,
        "state_part_refinement": True,
        "context_expansion_max_ratio": EXPECTED_CONTEXT_EXPANSION_MAX_RATIO,
    }

    command = module.reader_command(
        experiment_plan=plan,
        arm=arm,
        domain="web",
        configuration="winner_fixed_reader",
        memory_context_max_tokens=200_000,
    )
    runner_args = official_runner.parse_args(command[command.index("--") + 1 :])

    assert "--state-part-refinement" in command
    assert "True" not in command
    assert runner_args.state_part_completion_items == EXPECTED_STATE_PART_COMPLETION_ITEMS
    assert runner_args.state_part_refinement is True
    assert runner_args.context_expansion_max_ratio == EXPECTED_CONTEXT_EXPANSION_MAX_RATIO

    baseline_command = module.reader_command(
        experiment_plan=plan,
        arm=module.arm_by_name("trajectory_18k"),
        domain="web",
        configuration="baseline_fixed_reader",
        memory_context_max_tokens=200_000,
    )
    baseline_args = official_runner.parse_args(baseline_command[baseline_command.index("--") + 1 :])
    assert "--no-state-part-refinement" in baseline_command
    assert baseline_args.state_part_refinement is False
    assert baseline_args.context_expansion_max_ratio == 0.0


def test_ablation_gate_enforces_go_no_go_and_research_more(tmp_path: Path) -> None:
    module = _load_module()
    _, slice_path = _write_inputs(tmp_path)
    slice_record = module.load_json(slice_path)
    paths = _write_reports(
        tmp_path / "go",
        module,
        exact={"state_8k_diverse_neighbors": 0.36},
        multi={"state_8k_diverse_neighbors": 0.22},
    )

    gate = module.evaluate_ablation_reports(paths, slice_record=slice_record)

    assert gate["decision"] == "GO"
    assert gate["reader_phase_allowed"] is True
    assert gate["winner_arm"] == "state_8k_diverse_neighbors"

    masked_paths = _write_reports(
        tmp_path / "masked",
        module,
        exact={
            "state_8k_diverse": 0.40,
            "state_8k_diverse_neighbors": 0.36,
        },
        multi={
            "state_8k_diverse": 0.15,
            "state_8k_diverse_neighbors": 0.20,
        },
    )
    masked = module.evaluate_ablation_reports(masked_paths, slice_record=slice_record)
    assert masked["decision"] == "GO"
    assert masked["winner_arm"] == "state_8k_diverse_neighbors"

    no_go_paths = _write_reports(tmp_path / "no-go", module, exact={}, multi={})
    no_go = module.evaluate_ablation_reports(no_go_paths, slice_record=slice_record)
    assert no_go["decision"] == "NO-GO"

    research_paths = _write_reports(
        tmp_path / "research",
        module,
        exact={"state_8k_diverse": 0.30},
        multi={"state_8k_diverse": 0.14},
    )
    research = module.evaluate_ablation_reports(research_paths, slice_record=slice_record)
    assert research["decision"] == "RESEARCH-MORE"


def test_reader_plan_has_exactly_three_configs_with_observed_match(tmp_path: Path) -> None:
    module = _load_module()
    data_root, slice_path = _write_inputs(tmp_path)
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    plan = module.build_experiment_plan(
        data_root=data_root,
        official_repo=official_repo,
        output_root=tmp_path / "output",
        slice_path=slice_path,
        api_url="http://127.0.0.1:3434/api",
        tier="small",
        allow_localhost=True,
        query_workers=4,
    )
    for arm in plan["retrieval_arms"]:
        arm.pop("state_part_completion_items")
        arm.pop("state_part_refinement")
    gate = {
        "schema_version": module.GATE_SCHEMA_VERSION,
        "decision": "GO",
        "winner_arm": "state_8k_diverse_neighbors",
    }
    baseline_runs = {}
    for domain, values in {"web": (100, 300), "enterprise": (200, 400)}.items():
        run_dir = tmp_path / "baseline" / domain
        run_dir.mkdir(parents=True)
        _write_jsonl(
            run_dir / "per_question.jsonl",
            [{"memory_context_token_count": value} for value in values],
        )
        baseline_runs[domain] = run_dir

    reader_plan = module.build_reader_plan(
        experiment_plan=plan,
        gate=gate,
        baseline_runs=baseline_runs,
    )

    assert reader_plan["configurations"] == list(module.READER_CONFIGURATIONS)
    assert reader_plan["matched_context_tokens"] == EXPECTED_MATCHED_CONTEXT_TOKENS
    assert len(reader_plan["commands"]) == EXPECTED_READER_COMMAND_COUNT
    matched = [
        item
        for item in reader_plan["commands"]
        if item["configuration"] == "winner_matched_context"
    ]
    assert all(str(EXPECTED_MATCHED_CONTEXT_TOKENS) in item["command"] for item in matched)
    assert reader_plan["winner_arm_config"]["state_part_completion_items"] == 0
    assert reader_plan["winner_arm_config"]["state_part_refinement"] is False
    for domain, run_dir in baseline_runs.items():
        baseline_source = reader_plan["source_artifacts"]["baseline_runs"][domain]
        assert baseline_source["path"] == str(run_dir)
        assert baseline_source["per_question_sha256"] == module.sha256_file(
            run_dir / "per_question.jsonl"
        )

    with pytest.raises(ValueError, match="requires a GO"):
        module.build_reader_plan(
            experiment_plan=plan,
            gate={**gate, "decision": "NO-GO"},
            baseline_runs=baseline_runs,
        )


def test_reader_plan_binds_treatment_gate_to_frozen_preregistration(tmp_path: Path) -> None:
    module = _load_module()
    official_runner = _load_official_runner_module()
    data_root, slice_path = _write_inputs(tmp_path)
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    plan = module.build_experiment_plan(
        data_root=data_root,
        official_repo=official_repo,
        output_root=tmp_path / "output",
        slice_path=slice_path,
        api_url="http://127.0.0.1:3434/api",
        tier="small",
        allow_localhost=True,
        query_workers=4,
    )
    treatment_name = "state_part_refinement_plus_radius_one_v2"
    gate = {
        "schema_version": module.TREATMENT_GATE_SCHEMA_VERSION,
        "treatment": treatment_name,
        "decision": "GO",
        "reader_phase_allowed": True,
    }
    preregister = {
        "schema_version": module.TREATMENT_SCHEMA_VERSION,
        "treatment": {"name": treatment_name},
        "frozen_replay": {
            "slice_sha256": plan["slice"]["record_sha256"],
            "memory_representation": "trajectory_18k",
            "search_limit": 12,
            "max_context_items": EXPECTED_WINNER_CONTEXT_ITEMS,
            "max_chunks_per_trajectory": 8,
            "neighbor_stitch_items": EXPECTED_NEIGHBOR_STITCH_ITEMS,
            "neighbor_stitch_span": EXPECTED_NEIGHBOR_STITCH_SPAN,
            "state_part_completion_items": 0,
            "state_part_refinement": True,
            "context_expansion_max_ratio": 0.0,
        },
    }
    gate["winner_arm_config"] = {
        "name": treatment_name,
        "representation": "trajectory_18k",
        **{key: preregister["frozen_replay"][key] for key in module.QUERY_OVERRIDE_KEYS},
    }
    gate["treatment_preregister_record_sha256"] = module.sha256_json(preregister)
    baseline_runs = {}
    for domain, values in {"web": (100, 300), "enterprise": (200, 400)}.items():
        run_dir = tmp_path / "baseline" / domain
        run_dir.mkdir(parents=True)
        _write_jsonl(
            run_dir / "per_question.jsonl",
            [{"memory_context_token_count": value} for value in values],
        )
        baseline_runs[domain] = run_dir

    reader_plan = module.build_reader_plan(
        experiment_plan=plan,
        gate=gate,
        baseline_runs=baseline_runs,
        winner_preregister=preregister,
    )

    assert reader_plan["winner_arm"] == treatment_name
    assert reader_plan["winner_source"]["kind"] == "preregistered_treatment"
    assert reader_plan["winner_arm_config"]["max_context_items"] == EXPECTED_WINNER_CONTEXT_ITEMS
    assert reader_plan["winner_arm_config"]["state_part_refinement"] is True
    winner_commands = [
        item["command"]
        for item in reader_plan["commands"]
        if item["configuration"].startswith("winner_")
    ]
    assert winner_commands
    for command in winner_commands:
        args = official_runner.parse_args(command[command.index("--") + 1 :])
        assert args.max_context_items == EXPECTED_WINNER_CONTEXT_ITEMS
        assert args.state_part_refinement is True

    with pytest.raises(ValueError, match="requires --winner-preregister"):
        module.build_reader_plan(
            experiment_plan=plan,
            gate=gate,
            baseline_runs=baseline_runs,
        )
    modified_preregister = {
        **preregister,
        "frozen_replay": {
            **preregister["frozen_replay"],
            "slice_sha256": "sha256:different",
        },
    }
    with pytest.raises(ValueError, match="does not bind"):
        module.build_reader_plan(
            experiment_plan=plan,
            gate=gate,
            baseline_runs=baseline_runs,
            winner_preregister=modified_preregister,
        )
    with pytest.raises(ValueError, match="different frozen slices"):
        module.build_reader_plan(
            experiment_plan=plan,
            gate={
                **gate,
                "treatment_preregister_record_sha256": module.sha256_json(modified_preregister),
            },
            baseline_runs=baseline_runs,
            winner_preregister=modified_preregister,
        )


def test_reader_report_is_receipt_bound_and_descriptive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    plan, run_roots = _write_reader_report_inputs(tmp_path)

    report = module.build_reader_report(reader_plan=plan, run_roots=run_roots)

    assert report["status"] == "PASS"
    assert report["replication"]["promotion_decision"] is None
    assert report["replication"]["stochastic_reader"] is True
    fixed = report["configurations"]["winner_fixed_reader"]["combined"]
    assert fixed["correct_count"] == EXPECTED_READER_REPORT_CORRECT
    assert fixed["accuracy"] == EXPECTED_READER_REPORT_ACCURACY
    assert fixed["model_cost_usd"] == pytest.approx(EXPECTED_READER_REPORT_COST_USD)
    comparison = report["comparisons"]["winner_fixed_reader_vs_baseline_fixed_reader"]
    assert comparison["correct_count_delta"] == 1
    assert comparison["accuracy_delta"] == EXPECTED_READER_REPORT_ACCURACY_DELTA
    assert len(comparison["recoveries"]) == EXPECTED_READER_REPORT_RECOVERIES
    assert len(comparison["regressions"]) == 1
    serialized = json.dumps(report)
    assert "answer_gold" not in serialized
    assert "gold-sentinel" not in serialized

    plan_path = tmp_path / "reader_plan.json"
    output_path = tmp_path / "reader_report.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    arguments = ["reader-report", "--plan", str(plan_path)]
    for configuration, run_root in run_roots.items():
        arguments.extend(("--run", f"{configuration}={run_root}"))
    arguments.extend(("--output", str(output_path)))
    assert module.main(arguments) == 0
    capsys.readouterr()
    cli_report = json.loads(output_path.read_text(encoding="utf-8"))
    assert cli_report["source_artifacts"]["reader_plan"]["sha256"] == module.sha256_file(plan_path)

    receipt_path = run_roots["winner_fixed_reader"] / "web" / "longmemeval_v2_official_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_runs"]["domains"]["web"]["effective_memory_config"]["memory_params"][
        "max_context_items"
    ] = 9
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="planned retrieval config"):
        module.build_reader_report(reader_plan=plan, run_roots=run_roots)

    duplicate_plan = {
        **plan,
        "configurations": [
            "baseline_fixed_reader",
            "baseline_fixed_reader",
            "winner_matched_context",
        ],
    }
    with pytest.raises(ValueError, match="must be unique"):
        module.build_reader_report(reader_plan=duplicate_plan, run_roots=run_roots)


@pytest.mark.parametrize(
    ("plan_has_ratio", "receipt_has_ratio"),
    [(False, False), (False, True), (True, False)],
)
def test_reader_report_normalizes_disabled_expansion_budget_for_old_artifacts(
    tmp_path: Path,
    *,
    plan_has_ratio: bool,
    receipt_has_ratio: bool,
) -> None:
    module = _load_module()
    plan, run_roots = _write_reader_report_inputs(tmp_path)
    if not plan_has_ratio:
        for item in plan["commands"]:
            command = item["command"]
            index = command.index("--context-expansion-max-ratio")
            del command[index : index + 2]
    if not receipt_has_ratio:
        for run_root in run_roots.values():
            for domain in module.DOMAINS:
                receipt_path = run_root / domain / "longmemeval_v2_official_receipt.json"
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                memory_params = receipt["source_runs"]["domains"][domain][
                    "effective_memory_config"
                ]["memory_params"]
                memory_params.pop("context_expansion_max_ratio")
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    report = module.build_reader_report(reader_plan=plan, run_roots=run_roots)

    assert report["status"] == "PASS"


def test_reader_replication_plan_is_fixed_receipt_bound_and_score_blind(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    output_root = tmp_path / "replication"

    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=output_root,
    )

    assert plan["protocol"]["passes_per_configuration"] == EXPECTED_REPLICATION_PASS_COUNT
    assert plan["protocol"]["fixed_sample_no_sequential_stopping"] is True
    assert len(plan["runs"]) == EXPECTED_REPLICATION_RUN_COUNT
    assert sum(run["existing"] for run in plan["runs"]) == EXPECTED_REPLICATION_WAVE_RUNS
    assert plan["integrity_contract"]["configuration_selection_uses_reader_scores"] is False
    assert plan["integrity_contract"]["planning_uses_official_scoring_outputs"] is False
    assert "answer_gold" not in json.dumps(plan)
    replication.require_reader_replication_plan(plan)
    second_pass_id = replication.pass_id(2)
    second_pass = next(run for run in plan["runs"] if run["pass_id"] == second_pass_id)
    assert second_pass["shuffle_questions_seed"] == replication.PASS_SEEDS[1]
    assert "--shuffle-questions-seed" in second_pass["command"]

    reader_plan_path = tmp_path / "reader_plan.json"
    replication_plan_path = tmp_path / "replication_plan.json"
    reader_plan_path.write_text(json.dumps(reader_plan), encoding="utf-8")
    arguments = [
        "reader-replication-plan",
        "--reader-plan",
        str(reader_plan_path),
    ]
    for name in replication.PRIMARY_CONFIGURATIONS:
        arguments.extend(("--source-run", f"{name}={run_roots[name]}"))
    arguments.extend(
        (
            "--output-root",
            str(output_root),
            "--output",
            str(replication_plan_path),
        )
    )
    assert module.main(arguments) == 0
    capsys.readouterr()
    cli_plan = json.loads(replication_plan_path.read_text(encoding="utf-8"))
    assert cli_plan["source_artifacts"]["reader_plan"]["sha256"] == module.sha256_file(
        reader_plan_path
    )


def test_reader_replication_plan_rejects_tampering(tmp_path: Path) -> None:
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )
    second_pass_id = replication.pass_id(2)

    tampered = json.loads(json.dumps(plan))
    tampered_run = next(run for run in tampered["runs"] if run["pass_id"] == second_pass_id)
    seed_index = tampered_run["command"].index("--shuffle-questions-seed") + 1
    tampered_run["command"][seed_index] = "999"
    with pytest.raises(ValueError, match="question order"):
        replication.require_reader_replication_plan(tampered)

    tampered = json.loads(json.dumps(plan))
    tampered["runs"][0]["command"][0] = "python"
    with pytest.raises(ValueError, match="executable"):
        replication.require_reader_replication_plan(tampered)

    tampered = json.loads(json.dumps(plan))
    tampered_run = next(run for run in tampered["runs"] if run["pass_id"] == second_pass_id)
    tampered_run["command"].extend(("--limit", "1"))
    with pytest.raises(ValueError, match="derived command"):
        replication.require_reader_replication_plan(tampered)

    tampered = json.loads(json.dumps(plan))
    tampered_run = next(run for run in tampered["runs"] if run["pass_id"] == second_pass_id)
    tampered_run["command"].extend(("--reader-model", "other-model"))
    with pytest.raises(ValueError, match="repeats options"):
        replication.require_reader_replication_plan(tampered)

    tampered = json.loads(json.dumps(plan))
    tampered_run = next(run for run in tampered["runs"] if run["pass_id"] == second_pass_id)
    output_index = tampered_run["command"].index("--output-dir") + 1
    tampered_run["command"][output_index] = str(tmp_path / "escaped")
    tampered_run["output_dir"] = str(tmp_path / "escaped")
    with pytest.raises(ValueError, match="escaped its output root"):
        replication.require_reader_replication_plan(tampered)


def test_reader_replication_plan_rejects_contract_tampering(tmp_path: Path) -> None:
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )

    tampered = json.loads(json.dumps(plan))
    tampered["analysis"]["cluster_bootstrap_samples"] = 1
    with pytest.raises(ValueError, match="analysis contract"):
        replication.require_reader_replication_plan(tampered)

    tampered = json.loads(json.dumps(plan))
    del tampered["source_artifacts"]["reader_plan"]
    with pytest.raises(TypeError, match="reader plan binding"):
        replication.require_reader_replication_plan(tampered)

    tampered = json.loads(json.dumps(plan))
    tampered["cost_budget"]["estimated_incremental_model_cost_usd"] = 0.01
    with pytest.raises(ValueError, match="cost budget"):
        replication.require_reader_replication_plan(tampered)


def test_reader_replication_planning_does_not_parse_official_scores(tmp_path: Path) -> None:
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    score_path = run_roots["baseline_fixed_reader"] / "web" / "per_question.jsonl"
    rows = [json.loads(line) for line in score_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["score_bool"] = "not-a-score"
    _write_jsonl(score_path, rows)

    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )

    assert plan["integrity_contract"]["planning_uses_official_scoring_outputs"] is False


def test_reader_replication_planning_accepts_default_evaluator_endpoint(tmp_path: Path) -> None:
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    for configuration in replication.PRIMARY_CONFIGURATIONS:
        for domain in ("web", "enterprise"):
            run_args_path = run_roots[configuration] / domain / "run_args.json"
            run_args = json.loads(run_args_path.read_text(encoding="utf-8"))
            run_args["evaluator_base_url"] = ""
            run_args_path.write_text(json.dumps(run_args), encoding="utf-8")

    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )

    assert all("--evaluator-base-url" in run["command"] for run in plan["runs"])


def test_reader_replication_runner_rejects_changed_source_artifact(tmp_path: Path) -> None:
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )
    score_path = run_roots["baseline_fixed_reader"] / "web" / "per_question.jsonl"
    rows = [json.loads(line) for line in score_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["question_type"] = "changed-after-planning"
    _write_jsonl(score_path, rows)

    with pytest.raises(ValueError, match="changed after planning"):
        replication.run_reader_replication_plan(plan)


def test_reader_replication_runner_resumes_valid_receipts(tmp_path: Path) -> None:
    module = _load_module()
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )
    for run in plan["runs"]:
        if run["existing"]:
            continue
        expected = replication.expected_run_config(run["command"])
        config = {key: expected[key] for key in module.QUERY_OVERRIDE_KEYS}
        _write_reader_report_run(
            Path(run["output_dir"]),
            domain=run["domain"],
            question_ids=expected["question_ids"],
            scores=(True, False),
            config=config,
            memory_limit=expected["memory_context_max_tokens"],
            memory_dir=Path(expected["load_memory_dir"]),
            model_cost=0.10,
            shuffle_questions_seed=run["shuffle_questions_seed"],
        )

    result = replication.run_reader_replication_plan(plan)

    assert result["status"] == "PASS"
    assert result["completed"] == []
    assert len(result["skipped"]) == EXPECTED_REPLICATION_RUN_COUNT - EXPECTED_REPLICATION_WAVE_RUNS
    assert result["failures"] == []


@pytest.mark.parametrize("returncode", [0, 1])
def test_reader_replication_executor_finalizes_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    replication = _load_replication_module()
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "exit_code").write_text("9\n", encoding="utf-8")
    run = {
        "command": ["ignored"],
        "configuration": "baseline_fixed_reader",
        "domain": "web",
        "output_dir": str(output_dir),
        "pass_id": "pass_02",
    }
    monkeypatch.setattr(
        replication.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode),
    )

    assert replication.execute_run(run) == returncode
    assert (output_dir / "exit_code").read_text(encoding="utf-8") == f"{returncode}\n"
    assert not (output_dir / "exit_code.tmp").exists()
    events = [
        json.loads(line)
        for line in (output_dir / "replication_runner.log").read_text(encoding="utf-8").splitlines()
    ]
    assert events == [
        {"event": "start", "run": replication.run_key(run)},
        {
            "event": "complete",
            "returncode": returncode,
            "run": replication.run_key(run),
        },
    ]


def test_reader_replication_executor_clears_stale_receipt_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replication = _load_replication_module()
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "exit_code").write_text("0\n", encoding="utf-8")
    run = {
        "command": ["ignored"],
        "configuration": "winner_fixed_reader",
        "domain": "enterprise",
        "output_dir": str(output_dir),
        "pass_id": "pass_03",
    }

    def fail_to_start(*_args: object, **_kwargs: object) -> None:
        raise OSError("could not start")

    monkeypatch.setattr(replication.subprocess, "run", fail_to_start)

    with pytest.raises(OSError, match="could not start"):
        replication.execute_run(run)

    assert not (output_dir / "exit_code").exists()
    assert json.loads((output_dir / "replication_runner.log").read_text(encoding="utf-8")) == {
        "event": "start",
        "run": replication.run_key(run),
    }


def test_reader_replication_runner_stops_after_failed_wave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )
    waves = []

    def fail_wave(
        runs: list[dict[str, Any]],
        *,
        max_workers: int,
    ) -> list[tuple[dict[str, Any], int]]:
        waves.append((runs, max_workers))
        return [(run, 1) for run in runs]

    monkeypatch.setattr(replication, "execute_wave", fail_wave)

    result = replication.run_reader_replication_plan(plan)

    assert result["status"] == "FAIL"
    assert len(waves) == 1
    assert len(result["failures"]) == EXPECTED_REPLICATION_WAVE_RUNS


def test_reader_replication_runner_enforces_worker_and_actual_cost_caps(tmp_path: Path) -> None:
    module = _load_module()
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path, replication_costs=True)
    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )
    with pytest.raises(ValueError, match="predeclared cap"):
        replication.run_reader_replication_plan(plan, max_workers=5)

    for run in plan["runs"]:
        if run["existing"]:
            continue
        expected = replication.expected_run_config(run["command"])
        _write_reader_report_run(
            Path(run["output_dir"]),
            domain=run["domain"],
            question_ids=expected["question_ids"],
            scores=(True, False),
            config={key: expected[key] for key in module.QUERY_OVERRIDE_KEYS},
            memory_limit=expected["memory_context_max_tokens"],
            memory_dir=Path(expected["load_memory_dir"]),
            model_cost=0.20,
            shuffle_questions_seed=run["shuffle_questions_seed"],
        )

    result = replication.run_reader_replication_plan(plan)

    assert result["status"] == "FAIL"
    assert result["failures"] == [
        {
            "cost_budget_exceeded": True,
            "incremental_model_cost_usd": pytest.approx(2.64),
        }
    ]
    assert len(result["skipped"]) == 3 * EXPECTED_REPLICATION_WAVE_RUNS

    plan_path = tmp_path / "replication_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    report = _load_replication_report_module().build_reader_replication_report(
        plan=plan,
        plan_path=plan_path,
    )
    assert report["status"] == "FAIL"
    assert report["promotion_eligible"] is False


def test_reader_replication_bootstrap_is_deterministic_and_nontrivial() -> None:
    replication_report = _load_replication_report_module()
    effects = [-1.0, 0.0, 0.5, 1.0]

    first = replication_report.cluster_bootstrap(effects)
    second = replication_report.cluster_bootstrap(effects)

    assert first == second
    assert first["confidence_interval"]["lower"] < first["mean_accuracy_delta"]
    assert first["confidence_interval"]["upper"] > first["mean_accuracy_delta"]
    assert 0.0 < first["probability_positive"] < 1.0


@pytest.mark.parametrize(
    ("baseline_scores", "candidate_scores", "expected_outcome"),
    [
        ((False, False), (True, True), "GO"),
        ((True, True), (False, False), "NO-GO"),
        ((True, False), (True, False), "RESEARCH-MORE"),
    ],
)
def test_reader_replication_report_applies_frozen_decision_rule(
    tmp_path: Path,
    baseline_scores: tuple[bool, bool],
    candidate_scores: tuple[bool, bool],
    expected_outcome: str,
) -> None:
    replication_report = _load_replication_report_module()
    plan, plan_path = _write_replication_report_inputs(
        tmp_path,
        baseline_scores=baseline_scores,
        candidate_scores=candidate_scores,
    )

    report = replication_report.build_reader_replication_report(
        plan=plan,
        plan_path=plan_path,
    )

    assert report["status"] == "PASS"
    assert report["comparison"]["decision"]["outcome"] == expected_outcome
    assert report["comparison"]["cluster_bootstrap"]["samples"] == EXPECTED_BOOTSTRAP_SAMPLES
    assert len(report["comparison"]["per_pass"]) == EXPECTED_REPLICATION_PASS_COUNT
    assert report["costs"]["within_incremental_budget"] is True
    assert "answer_gold" not in json.dumps(report)
    assert "gold-sentinel" not in json.dumps(report)


def test_reader_replication_report_cli_and_incomplete_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    plan, plan_path = _write_replication_report_inputs(
        tmp_path,
        baseline_scores=(False, False),
        candidate_scores=(True, True),
    )
    output_path = tmp_path / "replication_report.json"

    assert (
        module.main(
            [
                "reader-replication-report",
                "--plan",
                str(plan_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["source_artifacts"]["replication_plan"]["sha256"] == module.sha256_file(plan_path)

    incomplete = next(run for run in plan["runs"] if not run["existing"])
    (Path(incomplete["output_dir"]) / "exit_code").unlink()
    with pytest.raises(FileNotFoundError, match="exit_code"):
        module.build_reader_replication_report(plan=plan, plan_path=plan_path)


def test_reader_replication_report_rejects_source_content_drift(tmp_path: Path) -> None:
    module = _load_module()
    plan, plan_path = _write_replication_report_inputs(
        tmp_path,
        baseline_scores=(False, False),
        candidate_scores=(True, True),
    )
    run = next(item for item in plan["runs"] if not item["existing"])
    receipt_path = Path(run["output_dir"]) / "longmemeval_v2_official_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["dataset"]["questions_sha256"] = "sha256:changed"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ValueError, match="source content identity"):
        module.build_reader_replication_report(plan=plan, plan_path=plan_path)


def test_reader_holdout_selection_is_stratified_deterministic_and_fresh(
    tmp_path: Path,
) -> None:
    holdout = _load_holdout_module()
    questions_path = tmp_path / "questions.jsonl"
    rows = []
    excluded = {}
    for domain in holdout.DOMAINS:
        excluded[domain] = [f"{domain}-excluded-{index}" for index in range(2)]
        rows.extend(
            {
                "id": question_id,
                "domain": domain,
                "question_type": "procedure",
                "question": "must not influence sampling",
                "answer": "must not influence sampling",
            }
            for question_id in excluded[domain]
        )
        rows.extend(
            {
                "id": f"{domain}-question-{index:03d}",
                "domain": domain,
                "question_type": (
                    "procedure" if index < EXPECTED_PRIMARY_STRATUM_COUNT else "errors-gotchas"
                ),
                "question": f"question sentinel {index}",
                "answer": f"gold sentinel {index}",
            }
            for index in range(60)
        )
    _write_jsonl(questions_path, rows)

    first = holdout.select_holdout_questions(questions_path, excluded_ids=excluded)
    second = holdout.select_holdout_questions(questions_path, excluded_ids=excluded)

    assert first == second
    assert first["seed"] == holdout.HOLDOUT_SAMPLE_SEED
    for domain in holdout.DOMAINS:
        selected = first["question_ids_by_domain"][domain]
        assert len(selected) == EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN
        assert not set(selected) & set(excluded[domain])
        assert set(first["selected_question_type_counts_by_domain"][domain]) == {
            "errors-gotchas",
            "procedure",
        }
    serialized = json.dumps(first)
    assert "question sentinel" not in serialized
    assert "gold sentinel" not in serialized


def test_reader_holdout_plan_freezes_fresh_three_arm_five_pass_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout = _load_holdout_module()
    replication_plan, replication_plan_path, report, report_path, source_runs = (
        _write_holdout_source_inputs(tmp_path, holdout=holdout)
    )
    monkeypatch.setattr(
        holdout,
        "require_reader_replication_plan",
        lambda _plan: source_runs,
    )

    plan = holdout.build_reader_holdout_plan(
        replication_plan=replication_plan,
        replication_plan_path=replication_plan_path,
        replication_report=report,
        replication_report_path=report_path,
        output_root=tmp_path / "holdout",
    )

    assert len(plan["runs"]) == EXPECTED_HOLDOUT_RUN_COUNT
    assert plan["protocol"]["score_visibility_during_execution"] == "none"
    assert plan["integrity_contract"]["sampling_uses_answers_or_scores"] is False
    assert plan["cost_budget"]["estimated_total_model_cost_usd"] == pytest.approx(7.2)
    for run in plan["runs"]:
        expected = holdout.expected_run_config(run["command"])
        assert len(expected["question_ids"]) == EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN
        assert expected["context_expansion_max_ratio"] == (
            EXPECTED_CONTEXT_EXPANSION_MAX_RATIO
            if run["configuration"] == "winner_token_bounded_1_2x"
            else 0.0
        )
        assert run.get("existing") is None

    changed = json.loads(json.dumps(plan))
    changed["runs"][0]["command"][-2:-2] = ["--question-ids", "leaked-question"]
    with pytest.raises(ValueError, match="repeats options"):
        holdout.require_reader_holdout_plan(changed)


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("selection", "deterministic selection changed"),
        ("search_limit", "command changed from its bound source"),
        ("memory", "command changed from its bound source"),
        ("cost", "cost budget changed"),
    ],
)
def test_reader_holdout_plan_rejects_source_derived_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    message: str,
) -> None:
    holdout = _load_holdout_module()
    replication_plan, replication_plan_path, report, report_path, source_runs = (
        _write_holdout_source_inputs(tmp_path, holdout=holdout)
    )
    monkeypatch.setattr(
        holdout,
        "require_reader_replication_plan",
        lambda _plan: source_runs,
    )
    plan = holdout.build_reader_holdout_plan(
        replication_plan=replication_plan,
        replication_plan_path=replication_plan_path,
        replication_report=report,
        replication_report_path=report_path,
        output_root=tmp_path / "holdout",
    )

    if tamper == "selection":
        domain = "web"
        question_types = plan["selection"]["question_types_by_domain"][domain]
        removed = next(
            question_id
            for question_id, question_type in question_types.items()
            if question_type == "errors-gotchas"
        )
        selected = plan["selection"]["question_ids_by_domain"][domain]
        replacement = next(
            f"web-{index:03d}"
            for index in range(
                EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN,
                EXPECTED_SOURCE_QUESTION_COUNT,
            )
            if f"web-{index:03d}" not in selected
        )
        selected[selected.index(removed)] = replacement
        selected.sort()
        del question_types[removed]
        question_types[replacement] = "errors-gotchas"
        plan["selection"]["question_types_by_domain"][domain] = dict(sorted(question_types.items()))
        for run in plan["runs"]:
            if run["domain"] == domain:
                run["command"] = holdout.replace_multi_option(
                    run["command"],
                    "--question-ids",
                    selected,
                )
    elif tamper == "search_limit":
        for run in plan["runs"]:
            if run["configuration"] == "winner_fixed_reader":
                run["command"] = holdout.replace_option(run["command"], "--search-limit", "40")
    elif tamper == "memory":
        for run in plan["runs"]:
            run["command"] = holdout.replace_option(
                run["command"], "--load-memory-dir", str(tmp_path / "rogue-memory")
            )
    else:
        plan["cost_budget"]["pilot_cost_per_question_run_usd"] = 999.0

    with pytest.raises(ValueError, match=message):
        holdout.require_reader_holdout_plan(plan)


def test_reader_holdout_resume_requires_score_blind_question_identity(tmp_path: Path) -> None:
    module = _load_module()
    holdout = _load_holdout_module()
    _, _, _, _, source_runs = _write_holdout_source_inputs(tmp_path, holdout=holdout)
    source = next(
        run
        for run in source_runs
        if run["configuration"] == "baseline_fixed_reader" and run["domain"] == "web"
    )
    output_dir = tmp_path / "completed-holdout-run"
    command = holdout.derive_holdout_command(
        _without_options(
            source["command"],
            "--official-repo",
            "--data-root",
            "--tier",
        ),
        output_dir=output_dir,
        question_ids=[f"web-{index:03d}" for index in range(EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN)],
        shuffle_seed=None,
        context_expansion_max_ratio=0.0,
    )
    expected = holdout.expected_run_config(command)
    _write_reader_report_run(
        output_dir,
        domain="web",
        question_ids=expected["question_ids"],
        scores=tuple(True for _ in expected["question_ids"]),
        config={key: expected[key] for key in module.QUERY_OVERRIDE_KEYS},
        memory_limit=expected["memory_context_max_tokens"],
        memory_dir=Path(expected["load_memory_dir"]),
        model_cost=0.10,
    )
    receipt_path = output_dir / "longmemeval_v2_official_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["dataset"]["selected_question_ids_sha256"] = holdout.sha256_question_ids(
        expected["question_ids"]
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    run = {"command": command, "output_dir": str(output_dir)}

    assert holdout.completed_run_cost(run) is not None

    receipt["dataset"]["selected_question_ids_sha256"] = "sha256:wrong"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert holdout.completed_run_cost(run) is None


def test_reader_holdout_runner_enforces_worker_cap_without_reading_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout = _load_holdout_module()
    plan = {
        "protocol": {"max_workers_cap": holdout.DEFAULT_MAX_WORKERS},
        "cost_budget": {
            "max_total_model_cost_usd": holdout.MAX_TOTAL_MODEL_COST_USD,
            "pilot_cost_per_question_run_usd": 0.01,
            "max_pilot_cost_multiplier": holdout.MAX_PILOT_COST_MULTIPLIER,
        },
    }
    runs = [
        {
            "pass_index": pass_index,
            "pass_id": holdout.pass_id(pass_index),
            "configuration": configuration,
            "domain": domain,
        }
        for pass_index in range(1, holdout.PASS_COUNT + 1)
        for configuration in holdout.HOLDOUT_CONFIGURATIONS
        for domain in holdout.DOMAINS
    ]
    monkeypatch.setattr(holdout, "require_reader_holdout_plan", lambda _plan: runs)
    monkeypatch.setattr(
        holdout,
        "completed_run_cost",
        lambda _run: {
            "model_cost_usd": 0.01,
            "question_count": EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN,
            "cost_per_question_run_usd": 0.01 / EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN,
        },
    )
    executed_waves = []

    def record_empty_wave(
        pending: list[dict[str, Any]],
        *,
        max_workers: int,
    ) -> list[tuple[dict[str, Any], int]]:
        executed_waves.append((pending, max_workers))
        return []

    monkeypatch.setattr(holdout, "execute_wave", record_empty_wave)

    with pytest.raises(ValueError, match="predeclared cap"):
        holdout.run_reader_holdout_plan(plan, max_workers=holdout.DEFAULT_MAX_WORKERS + 1)

    result = holdout.run_reader_holdout_plan(plan)

    assert result["status"] == "PASS"
    assert result["scores_read"] is False
    assert len(result["skipped"]) == EXPECTED_HOLDOUT_RUN_COUNT
    assert result["question_runs"] == (
        EXPECTED_HOLDOUT_RUN_COUNT * EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN
    )
    assert executed_waves == [([], holdout.DEFAULT_MAX_WORKERS)] * holdout.PASS_COUNT


def test_reader_holdout_report_consumes_complete_receipt_bound_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_module = _load_holdout_report_module()
    plan, plan_path = _write_holdout_report_inputs(tmp_path, report_module=report_module)
    monkeypatch.setattr(
        report_module,
        "require_reader_holdout_plan",
        lambda current: current["runs"],
    )

    report = report_module.build_reader_holdout_report(plan=plan, plan_path=plan_path)

    primary = report["comparisons"]["winner_token_bounded_1_2x_vs_baseline_fixed_reader"]
    assert report["status"] == "PASS"
    assert report["decision"]["outcome"] == "GO"
    assert report["decision"]["selected_configuration"] == ("winner_token_bounded_1_2x")
    assert primary["cluster_bootstrap"]["samples"] == EXPECTED_BOOTSTRAP_SAMPLES
    assert primary["memory_context_tokens"]["inflation"] == pytest.approx(0.2)
    assert report["bounded_context_budget"]["binding_rate"] == 1.0
    assert report["costs"]["within_budget"] is True
    assert len(report["configurations"]["baseline_fixed_reader"]["passes"]) == (
        EXPECTED_REPLICATION_PASS_COUNT
    )
    serialized = json.dumps(report)
    assert "answer_gold" not in serialized
    assert "gold-sentinel" not in serialized


def test_reader_holdout_report_accepts_real_validated_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout = importlib.import_module("benchmarks.longmemeval_v2_reader_holdout")
    report_module = _load_holdout_report_module()
    replication_plan, replication_plan_path, report, report_path, source_runs = (
        _write_holdout_source_inputs(tmp_path, holdout=holdout)
    )
    monkeypatch.setattr(
        holdout,
        "require_reader_replication_plan",
        lambda _plan: source_runs,
    )
    plan = holdout.build_reader_holdout_plan(
        replication_plan=replication_plan,
        replication_plan_path=replication_plan_path,
        replication_report=report,
        replication_report_path=report_path,
        output_root=tmp_path / "validated-holdout",
    )
    _write_holdout_plan_run_artifacts(plan, report_module=report_module)
    plan_path = tmp_path / "validated_holdout_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    result = report_module.build_reader_holdout_report(plan=plan, plan_path=plan_path)

    assert result["status"] == "PASS"
    assert result["decision"]["outcome"] == "GO"
    assert result["source_artifacts"]["holdout_plan"]["sha256"] == (
        report_module.sha256_file(plan_path)
    )


def test_reader_holdout_report_refuses_incomplete_or_invalid_budget_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_module = _load_holdout_report_module()
    plan, plan_path = _write_holdout_report_inputs(tmp_path, report_module=report_module)
    monkeypatch.setattr(
        report_module,
        "require_reader_holdout_plan",
        lambda current: current["runs"],
    )
    bounded = next(
        run for run in plan["runs"] if run["configuration"] == "winner_token_bounded_1_2x"
    )
    prompt_path = Path(bounded["output_dir"]) / "prompt_rows.jsonl"
    prompt_rows = report_module.load_jsonl(prompt_path)
    budget = prompt_rows[0]["memory_post_query_metadata"]["search_metadata"]["adapter_assembly"][
        "context_expansion_budget"
    ]
    budget["final_token_count"] = budget["max_token_count"] + 1
    _write_jsonl(prompt_path, prompt_rows)

    with pytest.raises(ValueError, match="inconsistent budget telemetry"):
        report_module.build_reader_holdout_report(plan=plan, plan_path=plan_path)

    _write_jsonl(prompt_path, prompt_rows[1:])
    with pytest.raises(ValueError, match="prompt rows do not match"):
        report_module.build_reader_holdout_report(plan=plan, plan_path=plan_path)


@pytest.mark.parametrize(
    (
        "bounded",
        "candidate",
        "candidate_vs_bounded_delta",
        "binding_rate",
        "expected_outcome",
        "expected_configuration",
        "expected_valid",
    ),
    [
        (
            (0.05, 0.90, -0.01, 0.10),
            (0.04, 0.90, -0.01, 0.10),
            -0.01,
            0.5,
            "GO",
            "winner_token_bounded_1_2x",
            True,
        ),
        (
            (0.04, 0.90, -0.01, 0.10),
            (0.08, 0.95, 0.01, 0.10),
            0.04,
            0.5,
            "GO",
            "winner_fixed_reader",
            True,
        ),
        ((0.00, 0.50, -0.04, 0.00), (-0.01, 0.40, -0.05, 0.00), -0.01, 0.5, "NO-GO", None, True),
        (
            (0.02, 0.70, -0.03, 0.10),
            (0.01, 0.60, -0.04, 0.10),
            -0.01,
            0.5,
            "RESEARCH-MORE",
            None,
            True,
        ),
        (
            (0.05, 0.90, -0.01, 0.10),
            (0.04, 0.90, -0.01, 0.10),
            -0.01,
            0.1,
            "RESEARCH-MORE",
            None,
            False,
        ),
    ],
)
def test_reader_holdout_report_applies_frozen_decision_rule(
    bounded: tuple[float, float, float, float],
    candidate: tuple[float, float, float, float],
    candidate_vs_bounded_delta: float,
    binding_rate: float,
    expected_outcome: str,
    expected_configuration: str | None,
    expected_valid: bool,
) -> None:
    report_module = _load_holdout_report_module()
    decision = report_module.holdout_decision(
        analysis=_holdout_analysis_contract(),
        bounded_vs_baseline=_holdout_comparison(*bounded),
        candidate_vs_baseline=_holdout_comparison(*candidate),
        candidate_vs_bounded=_holdout_comparison(
            candidate_vs_bounded_delta,
            0.5,
            -0.05,
            0.0,
        ),
        bounded_binding_rate=binding_rate,
        costs_within_budget=True,
    )

    assert decision["outcome"] == expected_outcome
    assert decision["selected_configuration"] == expected_configuration
    assert decision["valid_experiment"] is expected_valid


def test_reader_holdout_report_accepts_every_exact_promotion_boundary() -> None:
    report_module = _load_holdout_report_module()
    analysis = _holdout_analysis_contract()
    exact_gate = _holdout_comparison(0.03, 0.85, -0.02, 0.25)
    rejected_candidate = _holdout_comparison(0.0, 0.5, -0.05, 0.0)

    decision = report_module.holdout_decision(
        analysis=analysis,
        bounded_vs_baseline=exact_gate,
        candidate_vs_baseline=rejected_candidate,
        candidate_vs_bounded=_holdout_comparison(-0.03, 0.2, -0.1, 0.0),
        bounded_binding_rate=0.15,
        costs_within_budget=True,
    )

    assert decision["outcome"] == "GO"
    assert decision["selected_configuration"] == "winner_token_bounded_1_2x"
    assert all(decision["treatment_gates"]["winner_token_bounded_1_2x"]["checks"].values())


@pytest.mark.parametrize(
    ("tamper", "exception", "message"),
    [
        ("accuracy", ValueError, "aggregate score disagrees"),
        ("cost", ValueError, "incomplete reader cost coverage"),
        ("cost_value", TypeError, "invalid reader provider cost"),
        ("domain", ValueError, "planned domain"),
        ("evaluator_model", ValueError, "missing model pins"),
        ("generation", ValueError, "planned generation config"),
        ("memory", ValueError, "planned memory build"),
        ("runtime", ValueError, "planned runtime config"),
        ("shuffle", ValueError, "planned question order"),
        ("questions", ValueError, "question set does not match"),
        ("source_integrity", ValueError, "incomplete source integrity"),
        ("exit", ValueError, "did not exit cleanly"),
    ],
)
def test_reader_report_rejects_inconsistent_run_receipts(
    tmp_path: Path,
    tamper: str,
    exception: type[Exception],
    message: str,
) -> None:
    module = _load_module()
    plan, run_roots = _write_reader_report_inputs(tmp_path)
    run_dir = run_roots["baseline_fixed_reader"] / "web"
    _tamper_reader_run(tamper, run_dir=run_dir, tmp_path=tmp_path)

    with pytest.raises(exception, match=message):
        module.build_reader_report(reader_plan=plan, run_roots=run_roots)


def _tamper_reader_run(tamper: str, *, run_dir: Path, tmp_path: Path) -> None:
    if tamper == "accuracy":
        _tamper_reader_accuracy(run_dir)
    elif tamper in {"cost", "cost_value", "domain", "evaluator_model", "source_integrity"}:
        _tamper_reader_receipt(tamper, run_dir)
    elif tamper in {"generation", "memory", "runtime", "shuffle"}:
        _tamper_reader_args(tamper, run_dir=run_dir, tmp_path=tmp_path)
    elif tamper == "questions":
        _tamper_reader_questions(run_dir)
    else:
        (run_dir / "exit_code").write_text("1\n", encoding="utf-8")


def _tamper_reader_accuracy(run_dir: Path) -> None:
    path = run_dir / "aggregated_metrics.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["overall"]["overall_full_set"] = 0.25
    path.write_text(json.dumps(value), encoding="utf-8")


def _tamper_reader_receipt(tamper: str, run_dir: Path) -> None:
    path = run_dir / "longmemeval_v2_official_receipt.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "cost":
        value["accounting"]["reader"]["cost_coverage_complete"] = False
    elif tamper == "cost_value":
        del value["accounting"]["reader"]["provider_reported_cost_usd"]
    elif tamper == "domain":
        value["domain"] = "enterprise"
    elif tamper == "evaluator_model":
        value["models"]["evaluator_model"] = ""
    else:
        value["source_runs"]["integrity_complete"] = False
    path.write_text(json.dumps(value), encoding="utf-8")


def _tamper_reader_args(tamper: str, *, run_dir: Path, tmp_path: Path) -> None:
    path = run_dir / "run_args.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "generation":
        value["top_p"] = 0.5
    elif tamper == "memory":
        value["load_memory_dir"] = str(tmp_path / "other-memory")
    elif tamper == "runtime":
        value["reader_max_concurrent_requests"] = 4
    else:
        value["shuffle_questions_seed"] = 123
    path.write_text(json.dumps(value), encoding="utf-8")


def _tamper_reader_questions(run_dir: Path) -> None:
    path = run_dir / "per_question.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["question_id"] = "different-question"
    path.write_text("\n".join((json.dumps(row), *lines[1:])) + "\n", encoding="utf-8")


def test_retrieval_accounting_combines_saved_ingest_and_query_cost() -> None:
    module = _load_module()
    usage = {
        "requests": 1,
        "inputs": 1,
        "total_tokens": 100,
        "cost_reported_requests": 1,
        "cost_usd": 0.01,
        "provider": "openai",
        "model": "text-embedding-3-small",
    }
    records = [
        {
            "memory_query_duration_seconds": 1.0,
            "memory_post_query_metadata": {
                "ingest_embedding_usage": {
                    **usage,
                    "requests": 2,
                    "cost_reported_requests": 2,
                    "cost_usd": 0.02,
                },
                "search_metadata": {"embedding_usage": usage},
            },
        }
    ]

    accounting = module.summarize_retrieval_accounting(records)

    assert accounting["embedding_accounting"]["requests"] == EXPECTED_REPRESENTATION_COUNT
    assert (
        accounting["embedding_accounting"]["provider_reported_cost_usd"]
        == EXPECTED_EMBEDDING_COST_USD
    )
    assert accounting["embedding_accounting"]["cost_coverage_complete"] is True


def test_retrieval_resume_ignores_provenance_drift(tmp_path: Path) -> None:
    module = _load_module()
    path = tmp_path / "retrieval_run.json"
    identity = {
        "domain": "web",
        "arm": {"name": "state_8k_diverse"},
        "question_ids": ["q-web"],
        "memory_artifact_sha256": "sha256:memory",
    }

    module.ensure_run_identity(
        path,
        identity=identity,
        provenance={"runner": {"commit": "one", "dirty": True}},
    )
    module.ensure_run_identity(
        path,
        identity=identity,
        provenance={"runner": {"commit": "two", "dirty": False}},
    )

    record = module.load_json(path)
    assert record["identity"] == identity
    assert len(record["provenance_attempts"]) == EXPECTED_PROVENANCE_ATTEMPTS
    with pytest.raises(RuntimeError, match="changed identity"):
        module.ensure_run_identity(
            path,
            identity={**identity, "memory_artifact_sha256": "sha256:other"},
            provenance={},
        )


class _FakeMemory:
    def __init__(self) -> None:
        self.queries: list[tuple[str, str | None]] = []
        self.contexts: list[dict[str, Any]] = []
        self._local = threading.local()
        self._lock = threading.Lock()

    def set_query_context(self, **kwargs: Any) -> None:
        self._local.context = kwargs
        with self._lock:
            self.contexts.append(kwargs)

    def query(self, query: str, query_image: str | None = None) -> list[dict[str, str]]:
        with self._lock:
            self.queries.append((query, query_image))
        return [{"type": "text", "value": f"evidence for {query}"}]

    def post_query_hook(self, **kwargs: Any) -> dict[str, Any]:
        return {"context": self._local.context, "query": kwargs["query"]}

    def clear_query_context(self) -> None:
        del self._local.context


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    (data_root / "haystacks").mkdir(parents=True)
    questions = [
        {
            "id": "q-web",
            "domain": "web",
            "question_type": "static",
            "question": "What was selected?",
            "answer": "gold-web-sentinel",
        },
        {
            "id": "q-enterprise",
            "domain": "enterprise",
            "question_type": "dynamic",
            "question": "What changed?",
            "answer": "gold-enterprise-sentinel",
        },
    ]
    _write_jsonl(data_root / "questions.jsonl", questions)
    _write_jsonl(data_root / "trajectories.jsonl", [{"id": "t1"}])
    (data_root / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({"q-web": ["t1"], "q-enterprise": ["t1"]}),
        encoding="utf-8",
    )
    slice_path = tmp_path / "slice.json"
    slice_path.write_text(
        json.dumps(
            {
                "schema_version": "sibyl-longmemeval-v2-diagnostic-slice-v1",
                "cases": [
                    {"question_id": "q-web", "domain": "web"},
                    {"question_id": "q-enterprise", "domain": "enterprise"},
                ],
                "decision_thresholds": {
                    "selection_rule": "any_arm_meets_both",
                    "go": {
                        "exact_context_recall_at_10_absolute_gain": 0.15,
                        "multi_state_evidence_coverage_at_10_absolute_gain": 0.10,
                    },
                    "no_go": {"all_arms_exact_context_recall_gain_below": 0.08},
                    "research_more": {
                        "exact_context_recall_gain_min": 0.08,
                        "exact_context_recall_gain_max": 0.15,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return data_root, slice_path


def _write_reports(
    root: Path,
    module: ModuleType,
    *,
    exact: dict[str, float],
    multi: dict[str, float],
) -> dict[str, Path]:
    paths = {}
    for arm in module.RETRIEVAL_ARMS:
        name = arm["name"]
        path = root / name / "diagnostic_report.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "question_count": 2,
                    "metrics": {
                        "eligible_count": 2,
                        "multi_state_eligible_count": 1,
                        "exact_context_recall_at_10": exact.get(
                            name, 0.27 if name != module.BASELINE_ARM else 0.20
                        ),
                        "multi_state_evidence_coverage_at_10": multi.get(
                            name, 0.15 if name != module.BASELINE_ARM else 0.10
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )
        paths[name] = path
    return paths


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_reader_report_inputs(
    tmp_path: Path,
    *,
    replication_costs: bool = False,
) -> tuple[dict[str, Any], dict[str, Path]]:
    configurations = (
        "baseline_fixed_reader",
        "winner_fixed_reader",
        "winner_matched_context",
    )
    domains = ("web", "enterprise")
    plan = {
        "schema_version": "sibyl-longmemeval-v2-reader-plan-v1",
        "configurations": list(configurations),
        "commands": [],
    }
    run_roots = {}
    scores = {
        "baseline_fixed_reader": {"web": (True, False), "enterprise": (False, True)},
        "winner_fixed_reader": {"web": (True, True), "enterprise": (True, False)},
        "winner_matched_context": {"web": (True, False), "enterprise": (False, True)},
    }
    for configuration in configurations:
        run_root = tmp_path / configuration
        run_roots[configuration] = run_root
        for domain in domains:
            is_baseline = configuration == "baseline_fixed_reader"
            is_matched = configuration == "winner_matched_context"
            memory_limit = (
                EXPECTED_MATCHED_CONTEXT_TOKENS if is_matched else STANDARD_CONTEXT_TOKENS
            )
            config = {
                "search_limit": 12,
                "max_context_items": 8 if is_baseline else EXPECTED_WINNER_CONTEXT_ITEMS,
                "max_chunks_per_trajectory": 8,
                "neighbor_stitch_items": 0 if is_baseline else 2,
                "neighbor_stitch_span": 0 if is_baseline else 1,
                "state_part_completion_items": 0,
                "state_part_refinement": not is_baseline,
                "context_expansion_max_ratio": 0.0,
            }
            question_ids = [f"{domain}-1", f"{domain}-2"]
            run_dir = run_root / domain
            memory_dir = tmp_path / "memory" / domain
            command = [
                "moon",
                "run",
                "root:bench-longmemeval-v2-official-full",
                "--",
                "--domain",
                domain,
                "--output-dir",
                str(run_dir),
                "--load-memory-dir",
                str(memory_dir),
                "--reader-model",
                "Qwen/Qwen3.5-9B",
                "--reader-temperature",
                "0.6",
                "--reader-top-p",
                "0.95",
                "--reader-top-k",
                "20",
                "--reader-max-concurrent-requests",
                "16",
                "--memory-context-max-tokens",
                str(memory_limit),
                "--question-ids",
                *question_ids,
            ]
            for key, value in config.items():
                flag = f"--{key.replace('_', '-')}"
                if isinstance(value, bool):
                    command.append(flag if value else f"--no-{flag.removeprefix('--')}")
                else:
                    command.extend((flag, str(value)))
            command.append("--allow-localhost")
            plan["commands"].append(
                {
                    "configuration": configuration,
                    "domain": domain,
                    "command": command,
                }
            )
            _write_reader_report_run(
                run_dir,
                domain=domain,
                question_ids=question_ids,
                scores=scores[configuration][domain],
                config=config,
                memory_limit=memory_limit,
                memory_dir=memory_dir,
                model_cost=(
                    0.10 if replication_costs or configuration != "winner_fixed_reader" else 0.20
                ),
            )
    (tmp_path / "reader_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return plan, run_roots


def _write_replication_report_inputs(
    tmp_path: Path,
    *,
    baseline_scores: tuple[bool, bool],
    candidate_scores: tuple[bool, bool],
) -> tuple[dict[str, Any], Path]:
    module = _load_module()
    replication = _load_replication_module()
    reader_plan, run_roots = _write_reader_report_inputs(tmp_path)
    scores_by_configuration = {
        "baseline_fixed_reader": baseline_scores,
        "winner_fixed_reader": candidate_scores,
    }
    commands = replication.reader_command_map(reader_plan)
    for configuration in replication.PRIMARY_CONFIGURATIONS:
        for domain in module.DOMAINS:
            expected = replication.expected_run_config(commands[(configuration, domain)])
            _write_reader_report_run(
                run_roots[configuration] / domain,
                domain=domain,
                question_ids=expected["question_ids"],
                scores=scores_by_configuration[configuration],
                config={key: expected[key] for key in module.QUERY_OVERRIDE_KEYS},
                memory_limit=expected["memory_context_max_tokens"],
                memory_dir=Path(expected["load_memory_dir"]),
                model_cost=0.10,
            )
    plan = replication.build_reader_replication_plan(
        reader_plan=reader_plan,
        reader_plan_path=tmp_path / "reader_plan.json",
        source_run_roots={name: run_roots[name] for name in replication.PRIMARY_CONFIGURATIONS},
        output_root=tmp_path / "replication",
    )
    for run in plan["runs"]:
        if run["existing"]:
            continue
        expected = replication.expected_run_config(run["command"])
        _write_reader_report_run(
            Path(run["output_dir"]),
            domain=run["domain"],
            question_ids=expected["question_ids"],
            scores=scores_by_configuration[run["configuration"]],
            config={key: expected[key] for key in module.QUERY_OVERRIDE_KEYS},
            memory_limit=expected["memory_context_max_tokens"],
            memory_dir=Path(expected["load_memory_dir"]),
            model_cost=0.10,
            shuffle_questions_seed=run["shuffle_questions_seed"],
        )
    plan_path = tmp_path / "replication_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan, plan_path


def _without_options(command: list[str], *flags: str) -> list[str]:
    result = list(command)
    for flag in flags:
        index = result.index(flag)
        del result[index : index + 2]
    return result


def _write_holdout_source_inputs(
    tmp_path: Path,
    *,
    holdout: ModuleType,
) -> tuple[dict[str, Any], Path, dict[str, Any], Path, list[dict[str, Any]]]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    questions = []
    for domain in holdout.DOMAINS:
        questions.extend(
            {
                "id": f"{domain}-{index:03d}",
                "domain": domain,
                "question_type": (
                    "procedure"
                    if index < EXPECTED_HOLDOUT_QUESTIONS_PER_DOMAIN
                    else "errors-gotchas"
                ),
                "question": f"question {index}",
                "answer": f"gold {index}",
            }
            for index in range(EXPECTED_SOURCE_QUESTION_COUNT)
        )
    _write_jsonl(data_root / "questions.jsonl", questions)

    source_runs = []
    for configuration in holdout.PRIMARY_CONFIGURATIONS:
        for domain in holdout.DOMAINS:
            memory_dir = tmp_path / "memory" / domain
            memory_dir.mkdir(parents=True, exist_ok=True)
            for name in holdout.MEMORY_ARTIFACTS:
                (memory_dir / name).write_bytes(f"{name}:{domain}".encode())
            is_baseline = configuration == "baseline_fixed_reader"
            command = [
                "moon",
                "run",
                "root:bench-longmemeval-v2-official-full",
                "--",
                "--official-repo",
                str(tmp_path / "official"),
                "--data-root",
                str(data_root),
                "--domain",
                domain,
                "--tier",
                "small",
                "--output-dir",
                str(tmp_path / "pilot" / configuration / domain),
                "--load-memory-dir",
                str(memory_dir),
                "--reader-model",
                "Qwen/Qwen3.5-9B",
                "--reader-temperature",
                "0.6",
                "--reader-top-p",
                "0.95",
                "--reader-top-k",
                "20",
                "--memory-context-max-tokens",
                str(STANDARD_CONTEXT_TOKENS),
                "--question-ids",
                f"{domain}-000",
                f"{domain}-001",
                "--search-limit",
                "12",
                "--max-context-items",
                "8" if is_baseline else "10",
                "--max-chunks-per-trajectory",
                "8",
                "--neighbor-stitch-items",
                "0" if is_baseline else "2",
                "--neighbor-stitch-span",
                "0" if is_baseline else "1",
                "--state-part-completion-items",
                "0",
                "--no-state-part-refinement" if is_baseline else "--state-part-refinement",
                "--context-expansion-max-ratio",
                "0.0",
                "--allow-localhost",
            ]
            source_runs.append(
                {
                    "pass_id": "pass_01",
                    "pass_index": 1,
                    "configuration": configuration,
                    "domain": domain,
                    "command": command,
                }
            )

    replication_plan = {"schema_version": "synthetic-replication-plan"}
    replication_plan_path = tmp_path / "replication_plan.json"
    replication_plan_path.write_text(json.dumps(replication_plan), encoding="utf-8")
    report = {
        "schema_version": holdout.REPLICATION_REPORT_SCHEMA_VERSION,
        "status": "PASS",
        "configurations": {
            configuration: {"aggregate": {"question_count_per_pass": 2}}
            for configuration in holdout.PRIMARY_CONFIGURATIONS
        },
        "costs": {"total_model_cost_usd": 0.1},
        "source_artifacts": {
            "replication_plan": {
                "path": str(replication_plan_path),
                "sha256": holdout.sha256_file(replication_plan_path),
            }
        },
    }
    report_path = tmp_path / "replication_report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return replication_plan, replication_plan_path, report, report_path, source_runs


def _write_holdout_report_inputs(
    tmp_path: Path,
    *,
    report_module: ModuleType,
) -> tuple[dict[str, Any], Path]:
    holdout = _load_holdout_module()
    plan = {
        "schema_version": holdout.HOLDOUT_PLAN_SCHEMA_VERSION,
        "protocol": {"passes_per_configuration": holdout.PASS_COUNT},
        "analysis": _holdout_analysis_contract(),
        "cost_budget": {
            "estimated_total_model_cost_usd": 0.9,
            "max_total_model_cost_usd": holdout.MAX_TOTAL_MODEL_COST_USD,
            "pilot_cost_per_question_run_usd": 0.01,
            "max_pilot_cost_multiplier": holdout.MAX_PILOT_COST_MULTIPLIER,
        },
        "runs": [],
    }
    scores = {
        "baseline_fixed_reader": (False, False),
        "winner_fixed_reader": (True, False),
        "winner_token_bounded_1_2x": (True, True),
    }
    for pass_index, seed in enumerate(holdout.PASS_SEEDS, start=1):
        for configuration in report_module.HOLDOUT_CONFIGURATIONS:
            for domain in report_module.DOMAINS:
                question_ids = [f"{domain}-1", f"{domain}-2"]
                run_dir = tmp_path / "holdout" / f"pass_{pass_index:02d}" / configuration / domain
                memory_dir = tmp_path / "memory" / domain
                bounded = configuration == "winner_token_bounded_1_2x"
                is_baseline = configuration == "baseline_fixed_reader"
                config = {
                    "search_limit": 12,
                    "max_context_items": 8 if is_baseline else 10,
                    "max_chunks_per_trajectory": 8,
                    "neighbor_stitch_items": 0 if is_baseline else 2,
                    "neighbor_stitch_span": 0 if is_baseline else 1,
                    "state_part_completion_items": 0,
                    "state_part_refinement": not is_baseline,
                    "context_expansion_max_ratio": (
                        holdout.TOKEN_BOUNDED_MAX_RATIO if bounded else 0.0
                    ),
                }
                command = _holdout_reader_command(
                    run_dir=run_dir,
                    memory_dir=memory_dir,
                    domain=domain,
                    question_ids=question_ids,
                    config=config,
                    shuffle_seed=seed,
                )
                plan["runs"].append(
                    {
                        "pass_id": f"pass_{pass_index:02d}",
                        "pass_index": pass_index,
                        "configuration": configuration,
                        "domain": domain,
                        "output_dir": str(run_dir),
                        "command": command,
                    }
                )
                memory_tokens = 120 if bounded else 100
                _write_reader_report_run(
                    run_dir,
                    domain=domain,
                    question_ids=question_ids,
                    scores=scores[configuration],
                    config=config,
                    memory_limit=STANDARD_CONTEXT_TOKENS,
                    memory_dir=memory_dir,
                    model_cost=0.01,
                    shuffle_questions_seed=seed,
                    selected_question_ids_sha256=holdout.sha256_question_ids(question_ids),
                    memory_context_tokens=memory_tokens,
                    context_budget=(
                        _holdout_context_budget(binding=True)
                        if bounded
                        else _holdout_context_budget(binding=False)
                    ),
                )
    plan_path = tmp_path / "holdout_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan, plan_path


def _write_holdout_plan_run_artifacts(
    plan: dict[str, Any],
    *,
    report_module: ModuleType,
) -> None:
    reader_report = importlib.import_module("benchmarks.longmemeval_v2_reader_report")
    holdout = importlib.import_module("benchmarks.longmemeval_v2_reader_holdout")
    for run in plan["runs"]:
        configuration = run["configuration"]
        expected = report_module.expected_run_config(run["command"])
        question_ids = expected["question_ids"]
        if configuration == "baseline_fixed_reader":
            scores = [False] * len(question_ids)
        elif configuration == "winner_fixed_reader":
            scores = [index % 2 == 0 for index in range(len(question_ids))]
        else:
            scores = [True] * len(question_ids)
        bounded = configuration == "winner_token_bounded_1_2x"
        _write_reader_report_run(
            Path(run["output_dir"]),
            domain=run["domain"],
            question_ids=question_ids,
            scores=scores,
            config={key: expected[key] for key in reader_report.QUERY_OVERRIDE_KEYS},
            memory_limit=expected["memory_context_max_tokens"],
            memory_dir=Path(expected["load_memory_dir"]),
            model_cost=0.01,
            shuffle_questions_seed=run["shuffle_questions_seed"],
            selected_question_ids_sha256=holdout.sha256_question_ids(question_ids),
            memory_context_tokens=120 if bounded else 100,
            context_budget=_holdout_context_budget(binding=bounded),
        )
        receipt_path = Path(run["output_dir"]) / "longmemeval_v2_official_receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if expected["official_repo"] is not None:
            receipt["official_repo"]["path"] = expected["official_repo"]
        if expected["data_root"] is not None:
            receipt["dataset"]["data_root"] = expected["data_root"]
            receipt["dataset"]["tier"] = expected["tier"]
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


def _holdout_reader_command(
    *,
    run_dir: Path,
    memory_dir: Path,
    domain: str,
    question_ids: list[str],
    config: dict[str, Any],
    shuffle_seed: int | None,
) -> list[str]:
    command = [
        "moon",
        "run",
        "root:bench-longmemeval-v2-official-full",
        "--",
        "--domain",
        domain,
        "--output-dir",
        str(run_dir),
        "--load-memory-dir",
        str(memory_dir),
        "--reader-model",
        "Qwen/Qwen3.5-9B",
        "--reader-temperature",
        "0.6",
        "--reader-top-p",
        "0.95",
        "--reader-top-k",
        "20",
        "--reader-max-concurrent-requests",
        "16",
        "--memory-context-max-tokens",
        str(STANDARD_CONTEXT_TOKENS),
        "--question-ids",
        *question_ids,
    ]
    if shuffle_seed is not None:
        command.extend(("--shuffle-questions-seed", str(shuffle_seed)))
    for key, value in config.items():
        flag = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            command.append(flag if value else f"--no-{flag.removeprefix('--')}")
        else:
            command.extend((flag, str(value)))
    command.append("--allow-localhost")
    return command


def _holdout_context_budget(*, binding: bool) -> dict[str, Any]:
    if not binding:
        return {
            "enabled": False,
            "max_ratio": None,
            "binding": False,
            "dropped_item_count": 0,
        }
    return {
        "enabled": True,
        "max_ratio": EXPECTED_CONTEXT_EXPANSION_MAX_RATIO,
        "base_item_count": 2,
        "unbounded_item_count": 3,
        "final_item_count": 2,
        "base_token_count": 100,
        "max_token_count": 120,
        "unbounded_token_count": 140,
        "final_token_count": 120,
        "dropped_item_count": 1,
        "dropped_chunk_keys": [["trajectory", 1]],
        "binding": True,
    }


def _holdout_analysis_contract() -> dict[str, Any]:
    return {
        "decision_rule_version": "three-arm-question-bootstrap-v1",
        "go_min_mean_accuracy_delta": 0.03,
        "go_min_probability_positive": 0.85,
        "go_min_confidence_interval_lower": -0.02,
        "go_max_memory_token_inflation": 0.25,
        "candidate_selection_min_delta_over_bounded": 0.03,
        "minimum_bounded_binding_rate": 0.15,
        "no_go_max_probability_positive": 0.5,
        "no_go_rule": "each treatment has mean delta <= 0 or probability positive <= cutoff",
        "go_preference_order": [
            "winner_fixed_reader_if_eligible_and_3pp_above_bounded",
            "winner_token_bounded_1_2x_if_eligible",
        ],
        "otherwise": "RESEARCH-MORE",
    }


def _holdout_comparison(
    mean_delta: float,
    probability_positive: float,
    confidence_lower: float,
    token_inflation: float,
) -> dict[str, Any]:
    return {
        "cluster_bootstrap": {
            "mean_accuracy_delta": mean_delta,
            "probability_positive": probability_positive,
            "confidence_interval": {"lower": confidence_lower, "upper": 0.1},
        },
        "memory_context_tokens": {"inflation": token_inflation},
    }


def _write_reader_report_run(
    run_dir: Path,
    *,
    domain: str,
    question_ids: Sequence[str],
    scores: Sequence[bool],
    config: dict[str, Any],
    memory_limit: int,
    memory_dir: Path,
    model_cost: float,
    shuffle_questions_seed: int | None = None,
    selected_question_ids_sha256: str | None = None,
    memory_context_tokens: int | None = None,
    context_budget: dict[str, Any] | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "question_id": question_id,
            "question_type": "dynamic-environment",
            "score_bool": score,
            "answer_gold": "gold-sentinel",
        }
        for question_id, score in zip(question_ids, scores, strict=True)
    ]
    _write_jsonl(run_dir / "per_question.jsonl", rows)
    final_memory_tokens = (
        memory_context_tokens
        if memory_context_tokens is not None
        else (250 if memory_limit < STANDARD_CONTEXT_TOKENS else 400)
    )
    prompt_budget = context_budget or _holdout_context_budget(binding=False)
    _write_jsonl(
        run_dir / "prompt_rows.jsonl",
        [
            {
                "question_id": question_id,
                "memory_context_token_count": final_memory_tokens,
                "memory_post_query_metadata": {
                    "search_metadata": {
                        "adapter_assembly": {
                            "context_expansion_budget": prompt_budget,
                        }
                    }
                },
            }
            for question_id in question_ids
        ],
    )
    (run_dir / "exit_code").write_text("0\n", encoding="utf-8")
    (run_dir / "run_args.json").write_text(
        json.dumps(
            {
                "memory_context_max_tokens": memory_limit,
                "domain": domain,
                "output_dir": str(run_dir),
                "load_memory_dir": str(memory_dir),
                "model": "qwen/qwen3.5-9b",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "shuffle_questions_seed": shuffle_questions_seed,
                "reader_max_concurrent_requests": 16,
                "max_completion_tokens": 20_000,
                "timeout_seconds": 43_200.0,
                "evaluator_model": "openai/gpt-5.2",
                "evaluator_base_url": "https://openrouter.ai/api/v1",
                "evaluator_api_key_env": "OPENROUTER_API_KEY",
                "evaluator_reasoning_effort": "medium",
                "evaluator_max_completion_tokens": 4096,
                "evaluator_timeout_seconds": 43_200.0,
                "prompt_build_max_workers": 1,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "aggregated_metrics.json").write_text(
        json.dumps(
            {
                "overall": {"overall_full_set": sum(scores) / len(scores)},
                "tokens": {"prompt_tokens": 1000, "completion_tokens": 100},
                "memory_context": {
                    "avg_original_tokens": 400,
                    "avg_final_tokens": final_memory_tokens,
                    "num_truncated_sequences": (
                        EXPECTED_READER_REPORT_RECOVERIES
                        if memory_limit < STANDARD_CONTEXT_TOKENS
                        else 0
                    ),
                },
                "memory_query": {"avg_seconds": 2.0},
            }
        ),
        encoding="utf-8",
    )
    receipt = {
        "domain": domain,
        "official_repo": {"commit": "official-commit"},
        "dataset": {
            "questions_sha256": "sha256:questions",
            "trajectories_sha256": "sha256:trajectories",
            "haystack_sha256": f"sha256:{domain}-haystack",
        },
        "models": {
            "reader_model": "qwen/qwen3.5-9b",
            "evaluator_model": "openai/gpt-5.2",
        },
        "accounting": {
            "reader": {
                "cost_coverage_complete": True,
                "provider_reported_cost_usd": model_cost,
            },
            "judge": {
                "cost_coverage_complete": True,
                "provider_reported_cost_usd": 0.02,
            },
        },
        "source_runs": {
            "complete": True,
            "integrity_complete": True,
            "api_runtime_consistent": True,
            "domains": {
                domain: {
                    "effective_memory_config": {"memory_params": config},
                }
            },
        },
    }
    if selected_question_ids_sha256 is not None:
        receipt["dataset"]["selected_question_ids_sha256"] = selected_question_ids_sha256
    (run_dir / "longmemeval_v2_official_receipt.json").write_text(
        json.dumps(receipt),
        encoding="utf-8",
    )
