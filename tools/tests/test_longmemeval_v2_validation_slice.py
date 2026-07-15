from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_validation_slice as validation

PRIMARY_QUESTION_COUNT = 50
REQUIRED_CORRECTNESS_GAIN = 3


def test_validation_slice_is_score_blind_stratified_and_source_bound(tmp_path: Path) -> None:
    questions_path = tmp_path / "questions.jsonl"
    rows = []
    for domain in validation.DOMAINS:
        rows.extend(
            {
                "id": f"{domain}-{index:03d}",
                "domain": domain,
                "question_type": (
                    "procedure" if index < PRIMARY_QUESTION_COUNT else "errors-gotchas"
                ),
                "question": f"question sentinel {index}",
                "answer": f"answer sentinel {index}",
            }
            for index in range(80)
        )
    questions_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (tmp_path / "trajectories.jsonl").write_text("{}\n", encoding="utf-8")
    haystack_dir = tmp_path / "haystacks"
    haystack_dir.mkdir()
    (haystack_dir / "lme_v2_small.json").write_text("{}", encoding="utf-8")

    causal_plan_path = tmp_path / "causal-plan.json"
    causal_plan_path.write_text(
        json.dumps(
            {
                "confirmation_selection": {
                    "excluded_question_ids_by_domain": {
                        domain: [f"{domain}-{index:03d}" for index in range(4)]
                        for domain in validation.DOMAINS
                    },
                    "question_ids_by_domain": {
                        domain: [f"{domain}-{index:03d}" for index in range(4, 8)]
                        for domain in validation.DOMAINS
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    prior_paths = {}
    for domain in validation.DOMAINS:
        path = tmp_path / f"{domain}-prior.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "id": f"{domain}-{index:03d}",
                        "domain": domain,
                        "question": "prior question sentinel",
                        "answer": "prior answer sentinel",
                    }
                    for index in range(8, 10)
                ]
            ),
            encoding="utf-8",
        )
        prior_paths[domain] = path
    baseline_run_dirs = _write_baseline_runs(tmp_path)

    manifest = validation.build_validation_slice(
        questions_path=questions_path,
        causal_plan_path=causal_plan_path,
        prior_run_questions=prior_paths,
        baseline_run_dirs=baseline_run_dirs,
        created_at="2026-07-15T00:00:00+00:00",
    )

    validation.require_validation_slice(manifest)
    validation.validate_frozen_manifest(
        manifest,
        questions_path=questions_path,
        require_pinned_baseline=False,
    )
    assert manifest["selection"]["seed"] == validation.SAMPLE_SEED
    assert manifest["candidate_configuration"] == validation.CANDIDATE_CONFIGURATION
    assert manifest["decision_rule"] == validation.DECISION_RULE
    for domain in validation.DOMAINS:
        selected = manifest["selection"]["question_ids_by_domain"][domain]
        excluded = manifest["selection"]["excluded_question_ids_by_domain"][domain]
        assert len(selected) == validation.QUESTIONS_PER_DOMAIN
        assert not set(selected) & set(excluded)
        assert set(manifest["selection"]["selected_question_type_counts_by_domain"][domain]) == {
            "errors-gotchas",
            "procedure",
        }
    serialized = json.dumps(manifest)
    assert "question sentinel" not in serialized
    assert "answer sentinel" not in serialized

    changed = json.loads(serialized)
    changed["selection"]["question_ids_by_domain"]["web"][0] = "tampered"
    with pytest.raises(ValueError, match="changed from its score-blind source selection"):
        validation.require_validation_slice(changed)


def test_validation_slice_requires_exactly_one_prior_source_per_domain(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Expected prior-run question paths"):
        validation.parse_prior_run_questions([f"web={tmp_path / 'web.json'}"])


def test_committed_composition_validation_manifest_is_frozen() -> None:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_composition_validation.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == validation.SCHEMA_VERSION
    assert manifest["integrity_contract"] == validation.INTEGRITY_CONTRACT
    assert manifest["candidate_configuration"] == validation.CANDIDATE_CONFIGURATION
    assert manifest["decision_rule"] == validation.DECISION_RULE
    assert set(manifest["exclusion_lineage"]) == {
        "prior_composition_candidate",
        "prior_development_and_diagnostics",
        "unused_causal_confirmation",
    }
    validation.validate_exclusion_lineage(manifest["exclusion_lineage"])
    assert {
        name: source["sha256"] for name, source in manifest["source_artifacts"]["dataset"].items()
    } == validation.VALIDATION_DATASET_SHA256
    assert {
        question_id: {
            "path": source["relative_path"],
            "sha256": source["sha256"],
        }
        for question_id, source in manifest["source_artifacts"]["selected_question_images"].items()
    } == validation.SELECTED_QUESTION_IMAGES
    for domain in validation.DOMAINS:
        selected = manifest["selection"]["question_ids_by_domain"][domain]
        excluded = manifest["selection"]["excluded_question_ids_by_domain"][domain]
        assert len(selected) == validation.QUESTIONS_PER_DOMAIN
        assert not set(selected) & set(excluded)
        assert manifest["selected_question_ids_sha256_by_domain"][domain] == (
            validation.sha256_question_ids(selected)
        )
        assert manifest["excluded_question_ids_sha256_by_domain"][domain] == (
            validation.sha256_question_ids(excluded)
        )
    validation.validate_frozen_baseline(
        manifest["frozen_baseline"],
        selection=manifest["selection"],
    )

    changed = json.loads(json.dumps(manifest))
    first_id = next(
        iter(changed["frozen_baseline"]["domains"]["web"]["selected_scores_by_question_id"])
    )
    changed["frozen_baseline"]["domains"]["web"]["selected_scores_by_question_id"][first_id] ^= True
    with pytest.raises(ValueError, match="score source changed"):
        validation.validate_frozen_baseline(
            changed["frozen_baseline"],
            selection=changed["selection"],
        )

    changed = json.loads(json.dumps(manifest))
    changed["exclusion_lineage"]["prior_composition_candidate"]["enterprise"].pop()
    with pytest.raises(ValueError, match=r"prior_composition_candidate\.enterprise changed"):
        validation.validate_exclusion_lineage(changed["exclusion_lineage"])


@pytest.mark.parametrize(
    "reader_base_url",
    ["https://example.invalid/v1", "https://openrouter.ai/api/v1/"],
)
def test_validation_slice_rejects_candidate_configuration_drift(
    reader_base_url: str,
) -> None:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_composition_validation.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="does not match frozen manifest"):
        validation.validate_candidate_configuration(
            manifest,
            tier="small",
            reader_base_url=reader_base_url,
            reader_model="qwen/qwen3.5-9b",
            reader_max_concurrent_requests=16,
            reader_retry_attempts=4,
            evaluator_model="gpt-5.2",
            evidence_composition_mode="shared_relevance",
            source_evidence_bundling=True,
            include_screenshot_refs=False,
        )


def test_validation_slice_rejects_cross_domain_exclusion(tmp_path: Path) -> None:
    questions_path = tmp_path / "questions.jsonl"
    questions_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": f"{domain}-{index:03d}",
                    "domain": domain,
                    "question_type": "procedure",
                }
            )
            for domain in validation.DOMAINS
            for index in range(60)
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing from their domain"):
        validation.load_question_metadata(
            questions_path,
            excluded={"web": set(), "enterprise": {"web-000"}},
        )


def test_validation_report_applies_frozen_baseline_rule(tmp_path: Path) -> None:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_composition_validation.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    run_dirs = _write_candidate_runs(tmp_path, manifest=manifest)

    report = validation.evaluate_candidate(manifest, run_dirs=run_dirs)

    assert report["decision"]["outcome"] == "NO-GO"
    assert report["candidate"]["sibyl_commit"] == "a" * 40
    assert report["candidate"]["combined"]["accuracy_delta"] == 0.0

    enterprise_plan_path = run_dirs["enterprise"] / "longmemeval_v2_official_plan.json"
    enterprise_receipt_path = run_dirs["enterprise"] / "longmemeval_v2_official_receipt.json"
    enterprise_plan = json.loads(enterprise_plan_path.read_text(encoding="utf-8"))
    enterprise_receipt = json.loads(enterprise_receipt_path.read_text(encoding="utf-8"))
    enterprise_plan["runner_provenance"]["sibyl_commit"] = None
    enterprise_receipt["runner_provenance"]["sibyl_commit"] = None
    enterprise_receipt["sibyl_commit"] = None
    enterprise_plan_path.write_text(json.dumps(enterprise_plan), encoding="utf-8")
    enterprise_receipt_path.write_text(json.dumps(enterprise_receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="Sibyl commit is invalid"):
        validation.evaluate_candidate(manifest, run_dirs=run_dirs)

    enterprise_plan["runner_provenance"]["sibyl_commit"] = "b" * 40
    enterprise_receipt["runner_provenance"]["sibyl_commit"] = "b" * 40
    enterprise_receipt["sibyl_commit"] = "b" * 40
    enterprise_plan_path.write_text(json.dumps(enterprise_plan), encoding="utf-8")
    enterprise_receipt_path.write_text(json.dumps(enterprise_receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="different Sibyl commits"):
        validation.evaluate_candidate(manifest, run_dirs=run_dirs)
    enterprise_plan["runner_provenance"]["sibyl_commit"] = "a" * 40
    enterprise_receipt["runner_provenance"]["sibyl_commit"] = "a" * 40
    enterprise_receipt["sibyl_commit"] = "a" * 40
    enterprise_plan_path.write_text(json.dumps(enterprise_plan), encoding="utf-8")
    enterprise_receipt_path.write_text(json.dumps(enterprise_receipt), encoding="utf-8")

    enterprise_receipt["official_repo"]["commit"] = "b" * 40
    enterprise_receipt_path.write_text(json.dumps(enterprise_receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="official harness changed"):
        validation.evaluate_candidate(manifest, run_dirs=run_dirs)
    enterprise_receipt["official_repo"]["commit"] = validation.OFFICIAL_HARNESS_COMMIT
    enterprise_receipt_path.write_text(json.dumps(enterprise_receipt), encoding="utf-8")

    web_scores_path = run_dirs["web"] / "per_question.jsonl"
    web_scores = validation.load_jsonl(web_scores_path)
    flipped = 0
    for row in web_scores:
        if row["score_bool"] is False and flipped < REQUIRED_CORRECTNESS_GAIN:
            row["score_bool"] = True
            flipped += 1
    web_scores_path.write_text(
        "".join(json.dumps(row) + "\n" for row in web_scores),
        encoding="utf-8",
    )

    report = validation.evaluate_candidate(manifest, run_dirs=run_dirs)

    assert report["decision"]["outcome"] == "GO"
    assert report["decision"]["full_run_allowed"] is True


def test_validation_evaluate_cli_fails_closed_on_no_go(tmp_path: Path) -> None:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_composition_validation.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    run_dirs = _write_candidate_runs(tmp_path, manifest=manifest)
    output_path = tmp_path / "report.json"

    exit_code = validation.main(
        [
            "evaluate",
            "--manifest",
            str(path),
            "--run",
            f"web={run_dirs['web']}",
            "--run",
            f"enterprise={run_dirs['enterprise']}",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 1
    assert json.loads(output_path.read_text(encoding="utf-8"))["decision"]["outcome"] == "NO-GO"


def _write_baseline_runs(tmp_path: Path) -> dict[str, Path]:
    result = {}
    for domain in validation.DOMAINS:
        run_dir = tmp_path / f"baseline-{domain}"
        run_dir.mkdir()
        scores = [
            {"question_id": f"{domain}-{index:03d}", "score_bool": index % 2 == 0}
            for index in range(80)
        ]
        (run_dir / "per_question.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in scores),
            encoding="utf-8",
        )
        (run_dir / "longmemeval_v2_official_plan.json").write_text(
            json.dumps(
                {
                    "domain": domain,
                    "reader_model": validation.CANDIDATE_CONFIGURATION["reader_model"],
                    "evaluator_model": validation.CANDIDATE_CONFIGURATION["evaluator_model"],
                    "runner_provenance": {"sibyl_commit": validation.BASELINE_COMMIT},
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


def _write_candidate_runs(tmp_path: Path, *, manifest: dict[str, Any]) -> dict[str, Path]:
    result = {}
    selection = manifest["selection"]
    baseline = manifest["frozen_baseline"]
    assert isinstance(selection, dict)
    assert isinstance(baseline, dict)
    for domain in validation.DOMAINS:
        run_dir = tmp_path / f"candidate-{domain}"
        run_dir.mkdir()
        selected = selection["question_ids_by_domain"][domain]
        scores = baseline["domains"][domain]["selected_scores_by_question_id"]
        sibyl_commit = "a" * 40
        (run_dir / "per_question.jsonl").write_text(
            "".join(
                json.dumps({"question_id": question_id, "score_bool": scores[question_id]}) + "\n"
                for question_id in selected
            ),
            encoding="utf-8",
        )
        (run_dir / "longmemeval_v2_official_plan.json").write_text(
            json.dumps(
                {
                    "domain": domain,
                    "tier": validation.CANDIDATE_CONFIGURATION["tier"],
                    "reader_base_url": validation.CANDIDATE_CONFIGURATION["reader_base_url"],
                    "reader_model": validation.CANDIDATE_CONFIGURATION["reader_model"],
                    "reader_max_concurrent_requests": 16,
                    "reader_retry_attempts": 4,
                    "evaluator_model": validation.CANDIDATE_CONFIGURATION["evaluator_model"],
                    "evidence_composition_mode": validation.CANDIDATE_CONFIGURATION[
                        "evidence_composition_mode"
                    ],
                    "source_evidence_bundling": True,
                    "include_screenshot_refs": False,
                    "selected_question_ids_sha256": (
                        f"sha256:{validation.sha256_question_ids(selected)}"
                    ),
                    "runner_provenance": {"sibyl_commit": sibyl_commit},
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "longmemeval_v2_official_receipt.json").write_text(
            json.dumps(_candidate_receipt(domain, selected=selected, sibyl_commit=sibyl_commit)),
            encoding="utf-8",
        )
        result[domain] = run_dir
    return result


def _candidate_receipt(
    domain: str,
    *,
    selected: list[str],
    sibyl_commit: str,
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
            "commit": validation.OFFICIAL_HARNESS_COMMIT,
            "harness_path": "evaluation/harness.py",
            "harness_exists": True,
        },
        "models": {
            "reader_model": "qwen/qwen3.5-9b",
            "reader_base_url": "https://openrouter.ai/api/v1",
            "evaluator_model": "gpt-5.2",
        },
        "runner_provenance": {
            "sibyl_commit": sibyl_commit,
            "git_dirty": False,
            "git_status": "clean",
        },
        "sibyl_commit": sibyl_commit,
        "checks": [
            {"name": name, "status": status}
            for name, status in validation.RECEIPT_CHECK_STATUSES.items()
        ],
        "dataset": {
            **validation.VALIDATION_DATASET_SHA256,
            "name": "longmemeval-v2",
            "tier": "small",
            "question_count": len(selected),
            "selected_question_ids_sha256": (f"sha256:{validation.sha256_question_ids(selected)}"),
        },
    }
