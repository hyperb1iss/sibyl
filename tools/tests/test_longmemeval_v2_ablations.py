from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path
from types import ModuleType
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


def _load_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_ablations.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_ablations", path)
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
        ]
    )

    arm = module.retrieval_arm_from_args(args)

    assert arm["name"] == "trajectory_18k"
    assert arm["neighbor_stitch_items"] == EXPECTED_NEIGHBOR_STITCH_ITEMS
    assert arm["neighbor_stitch_span"] == EXPECTED_NEIGHBOR_STITCH_SPAN
    assert module.arm_by_name("trajectory_18k")["neighbor_stitch_items"] == 0


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

    with pytest.raises(ValueError, match="requires a GO"):
        module.build_reader_plan(
            experiment_plan=plan,
            gate={**gate, "decision": "NO-GO"},
            baseline_runs=baseline_runs,
        )


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
