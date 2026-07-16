from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_official as official
from benchmarks import longmemeval_v2_validation_slice as v1
from benchmarks import longmemeval_v2_validation_slice_v2 as validation

PROCEDURE_QUESTION_COUNT = 35
MAX_CONTEXT_TOTAL_CHARS = validation.CANDIDATE_CONFIGURATION["max_context_total_chars"]
EXPECTED_QUESTION_COUNT = validation.QUESTIONS_PER_DOMAIN * len(validation.DOMAINS)


def test_successor_slice_is_score_blind_and_excludes_predecessor(tmp_path: Path) -> None:
    questions_path, manifest = _write_synthetic_manifest(tmp_path)

    validation.require_validation_slice(manifest, require_pinned_predecessor=False)
    validation.validate_frozen_manifest(
        manifest,
        questions_path=questions_path,
        require_pinned_sources=False,
    )
    predecessor_ids = {
        domain: {f"{domain}-{index:03d}" for index in range(10)} for domain in validation.DOMAINS
    }
    for domain in validation.DOMAINS:
        selected = manifest["selection"]["question_ids_by_domain"][domain]
        assert len(selected) == validation.QUESTIONS_PER_DOMAIN
        assert not set(selected) & predecessor_ids[domain]
    serialized = json.dumps(manifest)
    assert "question sentinel" not in serialized
    assert "answer sentinel" not in serialized


def _write_synthetic_manifest(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    questions_path = tmp_path / "questions.jsonl"
    rows = [
        {
            "id": f"{domain}-{index:03d}",
            "domain": domain,
            "question_type": (
                "procedure" if index < PROCEDURE_QUESTION_COUNT else "static-environment"
            ),
            "question": f"question sentinel {index}",
            "answer": f"answer sentinel {index}",
        }
        for domain in validation.DOMAINS
        for index in range(70)
    ]
    questions_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (tmp_path / "trajectories.jsonl").write_text("{}\n", encoding="utf-8")
    haystack_dir = tmp_path / "haystacks"
    haystack_dir.mkdir()
    (haystack_dir / "lme_v2_small.json").write_text("{}", encoding="utf-8")
    predecessor_path = tmp_path / "composition-v1.json"
    predecessor_path.write_text(
        json.dumps(
            {
                "schema_version": v1.SCHEMA_VERSION,
                "selection": {
                    "excluded_question_ids_by_domain": {
                        domain: [f"{domain}-{index:03d}" for index in range(5)]
                        for domain in validation.DOMAINS
                    },
                    "question_ids_by_domain": {
                        domain: [f"{domain}-{index:03d}" for index in range(5, 10)]
                        for domain in validation.DOMAINS
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    baseline_run_dirs = _write_baseline_runs(tmp_path)

    manifest = validation.build_validation_slice(
        questions_path=questions_path,
        predecessor_manifest_path=predecessor_path,
        baseline_run_dirs=baseline_run_dirs,
        created_at="2026-07-16T00:00:00+00:00",
        require_pinned_predecessor=False,
    )
    return questions_path, manifest


def test_committed_successor_manifest_is_frozen() -> None:
    root = Path(__file__).parents[2]
    predecessor_path = root / "benchmarks" / "longmemeval_v2_composition_validation.json"
    manifest_path = root / "benchmarks" / "longmemeval_v2_composition_validation_v2.json"
    predecessor = json.loads(predecessor_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    validation.require_manifest_contract(manifest)
    validation.validate_exclusion_lineage(manifest["exclusion_lineage"])
    assert v1.sha256_file(predecessor_path) == validation.PREDECESSOR_MANIFEST_SHA256
    assert manifest["source_artifacts"]["predecessor_manifest"]["sha256"] == (
        validation.PREDECESSOR_MANIFEST_SHA256
    )
    assert manifest["candidate_configuration"]["max_context_total_chars"] == (
        MAX_CONTEXT_TOTAL_CHARS
    )
    assert manifest["protocol"]["question_count"] == EXPECTED_QUESTION_COUNT
    assert manifest["protocol"]["questions_per_domain"] == validation.QUESTIONS_PER_DOMAIN
    assert {
        name: source["sha256"] for name, source in manifest["source_artifacts"]["dataset"].items()
    } == v1.VALIDATION_DATASET_SHA256
    assert {
        question_id: {
            "path": source["relative_path"],
            "sha256": source["sha256"],
        }
        for question_id, source in manifest["source_artifacts"]["selected_question_images"].items()
    } == validation.SELECTED_QUESTION_IMAGES
    for domain in validation.DOMAINS:
        predecessor_seen = set(
            predecessor["selection"]["excluded_question_ids_by_domain"][domain]
        ) | set(predecessor["selection"]["question_ids_by_domain"][domain])
        selected = manifest["selection"]["question_ids_by_domain"][domain]
        excluded = manifest["selection"]["excluded_question_ids_by_domain"][domain]
        assert len(selected) == validation.QUESTIONS_PER_DOMAIN
        assert not set(selected) & predecessor_seen
        assert set(excluded) == predecessor_seen
        assert manifest["selected_question_ids_sha256_by_domain"][domain] == (
            v1.sha256_question_ids(selected)
        )
    assert (
        manifest["selection"]["available_question_type_counts_by_domain"]["enterprise"]
        == (manifest["selection"]["selected_question_type_counts_by_domain"]["enterprise"])
    )
    validation.validate_frozen_baseline(
        manifest["frozen_baseline"],
        selection=manifest["selection"],
    )


def test_successor_configuration_rejects_context_budget_drift() -> None:
    manifest = _load_committed_manifest()
    kwargs = {
        "tier": "small",
        "reader_base_url": "https://openrouter.ai/api/v1",
        "reader_model": "qwen/qwen3.5-9b",
        "reader_max_concurrent_requests": 16,
        "reader_retry_attempts": 4,
        "evaluator_model": "gpt-5.2",
        "evidence_composition_mode": "shared_relevance",
        "source_evidence_bundling": True,
        "include_screenshot_refs": False,
        "max_context_total_chars": 60_000,
    }

    validation.validate_candidate_configuration(manifest, **kwargs)
    kwargs["max_context_total_chars"] = 59_999
    with pytest.raises(ValueError, match="does not match frozen manifest"):
        validation.validate_candidate_configuration(manifest, **kwargs)


def test_successor_evaluation_is_manifest_and_artifact_bound(tmp_path: Path) -> None:
    questions_path, manifest = _write_synthetic_manifest(tmp_path)
    run_dirs = _write_candidate_runs(tmp_path, manifest=manifest)
    report = validation.evaluate_candidate(
        manifest,
        run_dirs=run_dirs,
        questions_path=questions_path,
        require_pinned_sources=False,
    )
    assert report["decision"]["outcome"] == "NO-GO"

    changed_manifest = json.loads(json.dumps(manifest))
    changed_manifest["integrity_contract"]["sampling_uses_answers_or_scores"] = True
    with pytest.raises(ValueError, match="integrity contract changed"):
        validation.evaluate_candidate(
            changed_manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )

    plan_path = run_dirs["web"] / "longmemeval_v2_official_plan.json"
    original_plan = plan_path.read_text(encoding="utf-8")
    changed_plan = json.loads(original_plan)
    changed_plan["max_context_total_chars"] = 59_999
    plan_path.write_text(json.dumps(changed_plan), encoding="utf-8")
    with pytest.raises(ValueError, match="web context budget changed"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )
    plan_path.write_text(original_plan, encoding="utf-8")

    scores_path = run_dirs["web"] / "per_question.jsonl"
    original_scores = scores_path.read_text(encoding="utf-8")
    scores_path.write_text(original_scores + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="per-question results artifact binding changed"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )
    scores_path.write_text(original_scores, encoding="utf-8")

    memory_path = run_dirs["web"] / "runtime_inputs" / "memory_config.json"
    original_memory = memory_path.read_text(encoding="utf-8")
    changed_memory = json.loads(original_memory)
    changed_memory["memory_params"]["max_context_total_chars"] = 59_999
    memory_path.write_text(json.dumps(changed_memory), encoding="utf-8")
    with pytest.raises(ValueError, match="memory config artifact binding changed"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )
    memory_path.write_text(original_memory, encoding="utf-8")

    accurate_run_dirs = _write_candidate_runs(
        tmp_path / "accurate",
        manifest=manifest,
        retrieval_mode="accurate",
    )
    with pytest.raises(ValueError, match="frozen retrieval mode changed"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=accurate_run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )


def test_developmental_replay_is_descriptive_and_non_promotable(tmp_path: Path) -> None:
    questions_path, manifest = _write_synthetic_manifest(tmp_path)
    run_dirs = _write_candidate_runs(
        tmp_path,
        manifest=manifest,
        retrieval_mode="accurate",
    )

    report = validation.evaluate_candidate(
        manifest,
        run_dirs=run_dirs,
        questions_path=questions_path,
        require_pinned_sources=False,
        developmental_replay=True,
    )

    assert report["status"] == "PASS"
    assert report["evaluation_context"] == {
        "claim_level": "adaptive developmental replay on a previously scored sample",
        "candidate_scores_previously_observed": True,
        "promotion_decision_allowed": False,
        "question_selection_changed": False,
        "treatment": validation.DEVELOPMENTAL_REPLAY_TREATMENT,
    }
    assert report["decision"] == {
        "outcome": "DEVELOPMENTAL",
        "full_run_allowed": False,
        "promotion_decision": False,
        "would_pass_frozen_rule": False,
        "informational_checks": {
            "combined_accuracy_delta": False,
            "minimum_domain_delta": True,
        },
    }

    memory_path = run_dirs["web"] / "runtime_inputs" / "memory_config.json"
    changed_memory = json.loads(memory_path.read_text(encoding="utf-8"))
    changed_memory["memory_params"]["retrieval_max_planned_queries"] = 2
    with pytest.raises(ValueError, match="developmental retrieval treatment changed"):
        validation.validate_developmental_retrieval_config(
            changed_memory,
            name="Candidate web runtime memory config",
        )

    receipt_path = run_dirs["web"] / "longmemeval_v2_official_receipt.json"
    changed_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    changed_receipt["accounting"]["planner"]["tracking_complete"] = False
    receipt_path.write_text(json.dumps(changed_receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="planner accounting is incomplete"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
            developmental_replay=True,
        )


def test_developmental_replay_cli_succeeds_without_promoting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "report.json"
    manifest_path.write_text("{}", encoding="utf-8")

    def evaluate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["developmental_replay"] is True
        return {
            "status": "PASS",
            "decision": {
                "outcome": "DEVELOPMENTAL",
                "full_run_allowed": False,
            },
        }

    monkeypatch.setattr(validation, "evaluate_candidate", evaluate)

    assert (
        validation.main(
            [
                "evaluate",
                "--manifest",
                str(manifest_path),
                "--questions",
                str(tmp_path / "questions.jsonl"),
                "--run",
                f"web={tmp_path / 'web'}",
                "--run",
                f"enterprise={tmp_path / 'enterprise'}",
                "--developmental-replay",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    assert json.loads(output_path.read_text(encoding="utf-8"))["decision"] == {
        "outcome": "DEVELOPMENTAL",
        "full_run_allowed": False,
    }


def test_successor_evaluation_rejects_semantic_and_completeness_drift(tmp_path: Path) -> None:
    questions_path, manifest = _write_synthetic_manifest(tmp_path)
    run_dirs = _write_candidate_runs(tmp_path, manifest=manifest)
    memory_path = run_dirs["web"] / "runtime_inputs" / "memory_config.json"
    receipt_path = run_dirs["web"] / "longmemeval_v2_official_receipt.json"
    original_memory = memory_path.read_text(encoding="utf-8")
    original_receipt = receipt_path.read_text(encoding="utf-8")

    changed_memory = json.loads(original_memory)
    changed_memory["memory_params"]["evidence_composition_mode"] = "reserved_support"
    memory_path.write_text(json.dumps(changed_memory), encoding="utf-8")
    receipt = json.loads(original_receipt)
    source_run = receipt["source_runs"]["domains"]["web"]
    source_run["runtime_inputs"]["memory_config"]["sha256"] = v1.sha256_file(memory_path)
    source_run["effective_memory_config"] = changed_memory
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime memory config changed"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )
    memory_path.write_text(original_memory, encoding="utf-8")
    receipt_path.write_text(original_receipt, encoding="utf-8")

    receipt = json.loads(original_receipt)
    receipt["source_runs"]["domains"]["web"]["effective_memory_config"]["memory_params"][
        "evidence_composition_mode"
    ] = "reserved_support"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="effective memory config changed"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )
    receipt_path.write_text(original_receipt, encoding="utf-8")

    receipt = json.loads(original_receipt)
    del receipt["source_runs"]["domains"]["web"]["run_args"]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="run args artifact binding changed"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )
    receipt_path.write_text(original_receipt, encoding="utf-8")

    provider_path = run_dirs["web"] / "provider_usage" / "reader.jsonl"
    original_provider_usage = provider_path.read_text(encoding="utf-8")
    provider_path.write_text(original_provider_usage + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reader provider usage artifact binding changed"):
        validation.evaluate_candidate(
            manifest,
            run_dirs=run_dirs,
            questions_path=questions_path,
            require_pinned_sources=False,
        )


def _load_committed_manifest() -> dict[str, Any]:
    path = (
        Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_composition_validation_v2.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _write_baseline_runs(tmp_path: Path) -> dict[str, Path]:
    result = {}
    for domain in validation.DOMAINS:
        run_dir = tmp_path / f"baseline-{domain}"
        run_dir.mkdir()
        scores = [
            {"question_id": f"{domain}-{index:03d}", "score_bool": index % 2 == 0}
            for index in range(70)
        ]
        (run_dir / "per_question.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in scores),
            encoding="utf-8",
        )
        (run_dir / "longmemeval_v2_official_plan.json").write_text(
            json.dumps(
                {
                    "domain": domain,
                    "reader_model": v1.CANDIDATE_CONFIGURATION["reader_model"],
                    "evaluator_model": v1.CANDIDATE_CONFIGURATION["evaluator_model"],
                    "runner_provenance": {"sibyl_commit": v1.BASELINE_COMMIT},
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "aggregated_metrics.json").write_text(
            json.dumps(
                {
                    "overall": {
                        "count_all_questions": len(scores),
                        "overall_full_set": 0.5,
                    }
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "longmemeval_v2_official_receipt.json").write_text("{}", encoding="utf-8")
        result[domain] = run_dir
    return result


def _write_candidate_runs(
    tmp_path: Path,
    *,
    manifest: dict[str, Any],
    retrieval_mode: str | None = None,
) -> dict[str, Path]:
    result = {}
    for domain in validation.DOMAINS:
        run_dir = tmp_path / f"candidate-{domain}"
        runtime_dir = run_dir / "runtime_inputs"
        provider_usage_dir = run_dir / "provider_usage"
        runtime_dir.mkdir(parents=True)
        provider_usage_dir.mkdir()
        selected = manifest["selection"]["question_ids_by_domain"][domain]
        scores = manifest["frozen_baseline"]["domains"][domain]["selected_scores_by_question_id"]
        sibyl_commit = "a" * v1.GIT_COMMIT_HEX_LENGTH
        provider_usage_run_id = f"candidate-{domain}-provider-usage"
        scores_path = run_dir / "per_question.jsonl"
        scores_path.write_text(
            "".join(
                json.dumps(
                    {
                        "question_id": question_id,
                        "score_bool": scores[question_id],
                        "memory_post_query_metadata": {
                            "api_runtime": {"sibyl_commit": sibyl_commit},
                            **(
                                {
                                    "retrieval_mode": "accurate",
                                    "search_metadata": {
                                        "planner_status": "success",
                                        "planner_usage": {
                                            "requests": 1,
                                            "input_tokens": 1,
                                            "output_tokens": 1,
                                        },
                                    },
                                }
                                if retrieval_mode == "accurate"
                                else {}
                            ),
                        },
                    }
                )
                + "\n"
                for question_id in selected
            ),
            encoding="utf-8",
        )
        plan_path = run_dir / "longmemeval_v2_official_plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "domain": domain,
                    **validation.CANDIDATE_CONFIGURATION,
                    "selected_question_ids_sha256": f"sha256:{v1.sha256_question_ids(selected)}",
                    "provider_usage_run_id": provider_usage_run_id,
                    "runner_provenance": {"sibyl_commit": sibyl_commit},
                }
            ),
            encoding="utf-8",
        )
        memory_config = {
            "memory_type": "sibyl_live_api",
            "memory_params": {
                "max_context_total_chars": MAX_CONTEXT_TOTAL_CHARS,
                "evidence_composition_mode": validation.CANDIDATE_CONFIGURATION[
                    "evidence_composition_mode"
                ],
                "source_evidence_bundling": validation.CANDIDATE_CONFIGURATION[
                    "source_evidence_bundling"
                ],
                "include_screenshot_refs": validation.CANDIDATE_CONFIGURATION[
                    "include_screenshot_refs"
                ],
            },
        }
        if retrieval_mode is not None:
            memory_config["memory_params"].update(
                {
                    "retrieval_mode": retrieval_mode,
                    "retrieval_max_planned_queries": 3,
                }
            )
        memory_path = runtime_dir / "memory_config.json"
        memory_path.write_text(json.dumps(memory_config), encoding="utf-8")
        (run_dir / "run_args.json").write_text(
            json.dumps(
                {
                    "domain": domain,
                    "model": validation.CANDIDATE_CONFIGURATION["reader_model"],
                    "base_url": validation.CANDIDATE_CONFIGURATION["reader_base_url"],
                    "evaluator_model": validation.CANDIDATE_CONFIGURATION["evaluator_model"],
                    "reader_max_concurrent_requests": validation.CANDIDATE_CONFIGURATION[
                        "reader_max_concurrent_requests"
                    ],
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "aggregated_metrics.json").write_text("{}", encoding="utf-8")
        (runtime_dir / "questions.json").write_text(
            json.dumps([{"id": question_id} for question_id in selected]),
            encoding="utf-8",
        )
        (runtime_dir / "haystack.json").write_text(
            json.dumps({question_id: [] for question_id in selected}),
            encoding="utf-8",
        )
        for role in ("reader", "judge"):
            (provider_usage_dir / f"{role}.jsonl").write_text(
                json.dumps(
                    {
                        "role": role,
                        "run_id": provider_usage_run_id,
                        "usage": {"total_tokens": 1},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        source_args = argparse.Namespace(
            domain=domain,
            web_output_dir=None,
            enterprise_output_dir=None,
        )
        source_run_records = official.load_receipt_source_runs(
            args=source_args,
            output_dir=run_dir,
        )
        source_runs = official.build_source_runs_receipt(
            args=source_args,
            source_runs=source_run_records,
        )
        assert source_runs["complete"] is True
        assert source_runs["integrity_complete"] is True
        assert source_runs["api_runtime_consistent"] is True
        receipt = _candidate_receipt(
            domain,
            selected=selected,
            sibyl_commit=sibyl_commit,
            source_runs=source_runs,
            accounting=official.build_receipt_accounting(
                metrics={},
                aggregated_metrics={},
                per_question_rows=source_run_records[0]["per_question_rows"],
                source_runs=source_run_records,
            ),
        )
        (run_dir / "longmemeval_v2_official_receipt.json").write_text(
            json.dumps(receipt),
            encoding="utf-8",
        )
        result[domain] = run_dir
    return result


def _candidate_receipt(
    domain: str,
    *,
    selected: list[str],
    sibyl_commit: str,
    source_runs: dict[str, Any],
    accounting: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "sibyl-longmemeval-v2-official-receipt-v1",
        "suite": "LongMemEval-V2 official",
        "suite_version": "official-harness-v1",
        "method": "sibyl_live_api",
        "domain": domain,
        "tier": "small",
        "official_repo": {
            "url": "https://github.com/xiaowu0162/LongMemEval-V2",
            "commit": v1.OFFICIAL_HARNESS_COMMIT,
            "harness_path": "evaluation/harness.py",
            "harness_exists": True,
        },
        "models": {
            "reader_model": validation.CANDIDATE_CONFIGURATION["reader_model"],
            "reader_base_url": validation.CANDIDATE_CONFIGURATION["reader_base_url"],
            "evaluator_model": validation.CANDIDATE_CONFIGURATION["evaluator_model"],
        },
        "runner_provenance": {
            "sibyl_commit": sibyl_commit,
            "git_dirty": False,
            "git_status": "clean",
        },
        "sibyl_commit": sibyl_commit,
        "accounting": accounting,
        "checks": [
            {"name": name, "status": status} for name, status in v1.RECEIPT_CHECK_STATUSES.items()
        ],
        "dataset": {
            **v1.VALIDATION_DATASET_SHA256,
            "name": "longmemeval-v2",
            "tier": "small",
            "question_count": len(selected),
            "selected_question_ids_sha256": f"sha256:{v1.sha256_question_ids(selected)}",
        },
        "source_runs": source_runs,
    }
