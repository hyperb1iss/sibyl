from __future__ import annotations

import json
import random
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from benchmarks.longmemeval_v2_official_source import (
    OFFICIAL_HARNESS_COMMIT,
    OFFICIAL_HARNESS_DIFF_URL,
    OFFICIAL_HARNESS_PATH,
    OFFICIAL_HARNESS_PREVIOUS_COMMIT,
    OFFICIAL_REPO_URL,
)
from tools.bench import longmemeval_v2_artifact_bridge as bridge
from tools.bench import longmemeval_v2_rig as rig


@pytest.fixture(autouse=True)
def _use_synthetic_official_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    question_ids = {domain: [f"{domain}-q{index}" for index in range(2)] for domain in rig.DOMAINS}
    monkeypatch.setattr(
        rig,
        "OFFICIAL_SMALL_QUESTION_COUNTS",
        {domain: len(ids) for domain, ids in question_ids.items()},
    )
    monkeypatch.setattr(
        rig,
        "OFFICIAL_SMALL_QUESTION_IDS_SHA256",
        {domain: rig.canonical_sha256(sorted(ids)) for domain, ids in question_ids.items()},
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "exists": True,
        "sha256": bridge._sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _checks() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "status": "PASS",
            "detail": "complete",
            "surfaces": [name],
        }
        for name in sorted(bridge.REQUIRED_PASSING_CHECKS)
    ]


def _receipt(
    *,
    domain: str,
    official_source: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    payload = dict.fromkeys(bridge.OFFICIAL_RECEIPT_KEYS)
    payload.update(
        {
            "schema_version": bridge.OFFICIAL_RECEIPT_SCHEMA_VERSION,
            "suite": "LongMemEval-V2 official",
            "suite_version": "official-harness-v1",
            "generated_at": "2026-08-20T00:00:00Z",
            "sibyl_commit": "a" * 40,
            "runner_provenance": {
                "sibyl_commit": "a" * 40,
                "git_dirty": False,
                "git_status": "clean",
            },
            "command": ["official"],
            "domain": domain,
            "tier": "small",
            "method": "sibyl_live_api",
            "claim_boundary": "official",
            "official_repo": official_source,
            "dataset": {
                "name": "longmemeval-v2",
                "data_root": plan["data_root"],
                "tier": "small",
                "questions_sha256": f"sha256:{'1' * 64}",
                "trajectories_sha256": f"sha256:{'2' * 64}",
                "haystack_sha256": f"sha256:{'3' * 64}",
                "question_count": plan["question_count"],
                "selected_question_ids_sha256": plan["selected_question_ids_sha256"],
                "official_question_count": plan["official_question_count"],
                "official_question_ids_sha256": plan["official_question_ids_sha256"],
                "selection_complete": plan["selection_complete"],
                "required_trajectory_count": plan["required_trajectory_count"],
            },
            "source_runs": {},
            "models": {
                "reader_model": plan["reader_model"],
                "reader_base_url": plan["reader_base_url"],
                "reader_expected_fragment": "reader-model",
                "evaluator_model": plan["evaluator_model"],
                "evaluator_expected_fragment": "judge-model",
                "evaluator_reasoning_effort": "none",
            },
            "artifacts": {},
            "metrics": {},
            "accounting": {
                "reader": {
                    "tracking_complete": True,
                    "estimated_input_tokens": 100,
                    "estimated_output_tokens": 20,
                },
                "judge": {
                    "tracking_complete": True,
                    "estimated_input_tokens": 30,
                    "estimated_output_tokens": 10,
                },
                "cost": {
                    "coverage_complete": True,
                    "provider_reported_total_usd": 0.5,
                },
            },
            "approval_boundary": {},
            "checks": _checks(),
        }
    )
    return payload


def _plan(
    *,
    domain: str,
    output_dir: Path,
    role: str,
    workflow_run_id: str,
    provider_run_id: str,
    phase: str,
    mode: str,
) -> dict[str, Any]:
    plan = dict.fromkeys(bridge.OFFICIAL_PLAN_KEYS)
    preregistration = None if phase in {"aa", "anchor"} else "9" * 64
    plan.update(
        {
            "schema_version": bridge.OFFICIAL_PLAN_SCHEMA_VERSION,
            "run_id": f"official-{domain}-{role}",
            "provider_usage_run_id": provider_run_id,
            "experiment_identity_schema_version": bridge.EXPERIMENT_IDENTITY_SCHEMA_VERSION,
            "runner_provenance": {
                "sibyl_commit": "a" * 40,
                "git_dirty": False,
                "git_status": "clean",
            },
            "execution": {
                "schema_version": rig.EXECUTION_IDENTITY_SCHEMA_VERSION,
                "kind": "github",
                "repository": "hyperb1iss/sibyl",
                "ref": "refs/heads/main",
                "workflow_ref": "hyperb1iss/sibyl/.github/workflows/eval.yml@refs/heads/main",
                "sha": "a" * 40,
                "run_id": workflow_run_id,
                "run_attempt": 1,
            },
            "experiment_id": "v1.3-final",
            "experiment_phase": phase,
            "pass_id": "pass-1",
            "pass_seed": 17,
            "arm_role": role,
            "substrate": "naive" if mode == "naive" else "machine",
            "preregistration_sha256": preregistration,
            "max_spend_usd": 2.0,
            "spend_reservation": {
                "schema_version": bridge.SPEND_RESERVATION_SCHEMA_VERSION,
                "status": "PASS",
                "currency": "USD",
                "price_snapshot": {},
                "price_snapshot_sources": {},
                "sections": {},
                "metered_estimate_usd": 1.0,
                "contingency_multiplier": 1.0,
                "unmetered_provider_reserve_usd": 0.0,
                "reserved_total_usd": 1.0,
                "max_spend_usd": 2.0,
                "within_cap": True,
                "enforcement": "fixed cap",
            },
            "domain": domain,
            "tier": "small",
            "method": "sibyl_live_api",
            "data_root": str((output_dir.parent / "dataset").resolve()),
            "output_dir": str(output_dir.resolve()),
            "runtime_dir": str((output_dir / "runtime_inputs").resolve()),
            "memory_config_path": str(
                (output_dir / "runtime_inputs" / "memory_config.json").resolve()
            ),
            "official_source": {
                "url": OFFICIAL_REPO_URL,
                "path": "/official/longmemeval-v2",
                "commit": OFFICIAL_HARNESS_COMMIT,
                "expected_commit": OFFICIAL_HARNESS_COMMIT,
                "pin_matches": True,
                "git_status": "clean",
                "harness_path": OFFICIAL_HARNESS_PATH,
                "harness_exists": True,
                "previous_reviewed_commit": OFFICIAL_HARNESS_PREVIOUS_COMMIT,
                "reviewed_diff_url": OFFICIAL_HARNESS_DIFF_URL,
            },
            "official_repo": "/official/longmemeval-v2",
            "plan_only": False,
            "skip_evaluation": False,
            "trajectory_path_exists": True,
            "question_count": 2,
            "required_trajectory_count": 2,
            "reader_model": "reader-model",
            "reader_base_url": "https://reader.invalid/v1",
            "evaluator_model": "judge-model",
            "max_context_total_chars": 60_000,
            "operational_note_dedupe_mode": "canonical",
            "operational_note_lane_mode": "grouped",
            "operational_note_distillation_profile": "v1.3",
            "render_char_total_treatment": 400_000,
            "render_group_lanes": False,
            "render_action_spines": False,
            "retrieval_mode": mode,
            "requirements": {"all_ready": True},
            "provider_usage": {
                role_name: str((output_dir / "provider_usage" / f"{role_name}.jsonl").resolve())
                for role_name in sorted(bridge.PROVIDER_ROLE_KEYS)
            },
            "summary": {},
            "honesty_contract": {},
        }
    )
    return plan


def _activity(*, mode: str, levers: dict[str, int] | None = None) -> dict[str, Any]:
    naive = mode == "naive"
    lever_activity = levers or {}
    return {
        "retrieval_mode": mode,
        "context_pack_requests": 1,
        "hybrid_vector_attempts": 0 if naive else 1,
        "hybrid_vector_successes": 0 if naive else 1,
        "naive_vector_attempts": 1 if naive else 0,
        "naive_vector_successes": 1 if naive else 0,
        "planner_query_count": 0,
        "typed_evidence_applicable": not naive,
        "typed_search_statuses": [] if naive else ["complete"],
        "activity_events": 2 + sum(lever_activity.values()),
        "mode": mode,
        "lever_activity": lever_activity,
    }


def _domain_run(
    root: Path,
    *,
    domain: str,
    role: str,
    workflow_run_id: str,
    provider_run_id: str,
    phase: str,
    mode: str,
) -> tuple[dict[str, Any], dict[str, Path]]:
    output_dir = root / domain
    runtime_dir = output_dir / "runtime_inputs"
    usage_dir = output_dir / "provider_usage"
    plan = _plan(
        domain=domain,
        output_dir=output_dir,
        role=role,
        workflow_run_id=workflow_run_id,
        provider_run_id=provider_run_id,
        phase=phase,
        mode=mode,
    )
    runtime_questions = [
        {
            "id": f"{domain}-q{index}",
            "question": f"question {index}",
            "answer": "answer",
            "question_type": "static",
            "eval_function": "exact",
            "domain": domain,
            "environment": domain,
        }
        for index in range(2)
    ]
    question_ids = [row["id"] for row in runtime_questions]
    plan["selected_question_ids_sha256"] = bridge._question_ids_sha256(question_ids)
    plan["official_question_count"] = len(question_ids)
    plan["official_question_ids_sha256"] = plan["selected_question_ids_sha256"]
    plan["selection_complete"] = True
    shuffled = list(question_ids)
    random.Random(plan["pass_seed"]).shuffle(shuffled)  # noqa: S311
    per_question: list[dict[str, Any]] = []
    rig_rows: list[dict[str, Any]] = []
    for stream_index, question_id in enumerate(shuffled):
        original_index = question_ids.index(question_id)
        score_row = dict.fromkeys(bridge.PER_QUESTION_KEYS)
        score_row.update(
            {
                "index": original_index,
                "stream_index": stream_index,
                "question_id": question_id,
                "memory_context": [{"type": "text", "value": "evidence"}],
                "memory_query_duration_seconds": 1.0,
                "memory_post_query_duration_seconds": 0.1,
                "score": 1.0,
                "score_bool": True,
                "usage": {"total_tokens": 100},
            }
        )
        per_question.append(score_row)
        levers = {"render_lane": 1} if role == "render_treatment" else {}
        rig_rows.append(
            {
                "question_id": question_id,
                "status": "valid",
                "context_status": "complete",
                "evidence_exposure_eligible": True,
                "evidence_exposed": stream_index == 0,
                "activity": _activity(mode=mode, levers=levers),
            }
        )
    paths = {
        "plan": output_dir / "longmemeval_v2_official_plan.json",
        "official_receipt": output_dir / "longmemeval_v2_official_receipt.json",
        "run_args": output_dir / "run_args.json",
        "aggregated_metrics": output_dir / "aggregated_metrics.json",
        "per_question": output_dir / "per_question.jsonl",
        "rig_rows": output_dir / "rig_rows.jsonl",
        "runtime_questions": runtime_dir / "questions.json",
        "runtime_haystack": runtime_dir / "haystack.json",
        "runtime_memory_config": runtime_dir / "memory_config.json",
        "reader_provider_usage": usage_dir / "reader.jsonl",
        "judge_provider_usage": usage_dir / "judge.jsonl",
    }
    _write_json(paths["plan"], plan)
    _write_json(
        paths["run_args"],
        {
            "domain": domain,
            "questions_path": str(paths["runtime_questions"].resolve()),
            "haystack_path": str(paths["runtime_haystack"].resolve()),
            "memory_config_path": str(paths["runtime_memory_config"].resolve()),
            "output_dir": str(output_dir.resolve()),
            "model": plan["reader_model"],
            "base_url": plan["reader_base_url"],
            "evaluator_model": plan["evaluator_model"],
            "shuffle_questions_seed": plan["pass_seed"],
        },
    )
    _write_json(paths["aggregated_metrics"], {"complete": True})
    _write_jsonl(paths["per_question"], per_question)
    _write_jsonl(paths["rig_rows"], rig_rows)
    _write_json(paths["runtime_questions"], runtime_questions)
    _write_json(paths["runtime_haystack"], {qid: ["trajectory"] for qid in question_ids})
    _write_json(
        paths["runtime_memory_config"],
        {
            "memory_type": "sibyl_live_api",
            "memory_params": {
                "api_url": "http://127.0.0.1:3334/api",
                "longmemeval_v2_domain": domain,
                "project_id": f"project-{domain}",
                "run_id": plan["run_id"],
                "runner_provenance": plan["runner_provenance"],
                "retrieval_mode": mode,
                "content_max_chars": 18_000,
                "max_context_items": 8,
                "max_context_chars_per_item": 12_000,
                "max_context_total_chars": 60_000,
                "operational_note_dedupe_mode": "canonical",
                "operational_note_lane_mode": "grouped",
                "operational_note_distillation_profile": "v1.3",
                "render_char_total_treatment": 400_000,
                "render_group_lanes": False,
                "render_action_spines": False,
            },
        },
    )
    _write_jsonl(paths["reader_provider_usage"], [{"run_id": provider_run_id}])
    _write_jsonl(paths["judge_provider_usage"], [{"run_id": provider_run_id}])
    official_source = plan["official_source"]
    _write_json(
        paths["official_receipt"],
        _receipt(domain=domain, official_source=official_source, plan=plan),
    )
    provider_records = {}
    for provider_role in sorted(bridge.PROVIDER_ROLE_KEYS):
        record = _artifact(paths[f"{provider_role}_provider_usage"])
        record.update(
            {
                "event_count": 2,
                "invalid_line_count": 0,
                "run_ids": [provider_run_id],
                "expected_run_id": provider_run_id,
                "foreign_event_count": 0,
                "attempt_count": 1,
            }
        )
        provider_records[provider_role] = record
    source = {
        "output_dir": str(output_dir.resolve()),
        "plan": _artifact(paths["plan"]),
        "official_receipt": _artifact(paths["official_receipt"]),
        "run_args": _artifact(paths["run_args"]),
        "aggregated_metrics": _artifact(paths["aggregated_metrics"]),
        "per_question": _artifact(paths["per_question"]),
        "rig_rows": _artifact(paths["rig_rows"]),
        "runtime_inputs": {
            "questions": _artifact(paths["runtime_questions"]),
            "haystack": _artifact(paths["runtime_haystack"]),
            "memory_config": _artifact(paths["runtime_memory_config"]),
        },
        "provider_usage": provider_records,
        "effective_memory_config": json.loads(
            paths["runtime_memory_config"].read_text(encoding="utf-8")
        ),
        "api_runtime": {
            "status": "healthy",
            "version": "1.3.0",
            "runtime": {
                "commit": "a" * 40,
                "git_dirty": False,
                "git_status": "clean",
            },
        },
        "api_runtime_consistent": True,
        "reader_model": plan["reader_model"],
        "reader_base_url": plan["reader_base_url"],
        "evaluator_model": plan["evaluator_model"],
        "method": "sibyl_live_api",
        "tier": "small",
    }
    return source, paths


def _combined_receipt(
    root: Path,
    *,
    role: str = "display_control",
    workflow_run_id: str = "1001",
    phase: str = "aa",
    mode: str = "fast",
) -> tuple[Path, dict[str, Any], dict[str, dict[str, Path]]]:
    sources: dict[str, Any] = {}
    paths_by_domain: dict[str, dict[str, Path]] = {}
    for domain in sorted(rig.DOMAINS):
        source, paths = _domain_run(
            root,
            domain=domain,
            role=role,
            workflow_run_id=workflow_run_id,
            provider_run_id=f"provider-{domain}-{role}",
            phase=phase,
            mode=mode,
        )
        sources[domain] = source
        paths_by_domain[domain] = paths
    web_plan = json.loads(paths_by_domain["web"]["plan"].read_text(encoding="utf-8"))
    combined = _receipt(
        domain="combined",
        official_source=web_plan["official_source"],
        plan=web_plan,
    )
    combined["source_runs"] = {
        "expected_domains": ["web", "enterprise"],
        "domains": sources,
        "complete": True,
        "integrity_complete": True,
        "api_runtime_consistent": True,
        "model_consistent": True,
        "method_consistent": True,
    }
    combined_path = root / "combined" / "longmemeval_v2_official_receipt.json"
    _write_json(combined_path, combined)
    return combined_path, combined, paths_by_domain


def _refresh_artifact(
    combined_path: Path,
    combined: dict[str, Any],
    *,
    domain: str,
    artifact: str,
    path: Path,
) -> None:
    combined["source_runs"]["domains"][domain][artifact] = _artifact(path)
    _write_json(combined_path, combined)


def test_bridge_builds_signed_arm_from_official_artifacts(tmp_path: Path) -> None:
    combined_path, _combined, _paths = _combined_receipt(tmp_path)

    arm = bridge.build_arm_run(combined_path)

    assert arm["schema_version"] == rig.ARM_RUN_SCHEMA_VERSION
    assert arm["experiment_phase"] == "aa"
    assert arm["preregistration_sha256"] == ""
    assert arm["provider_usage"]["requests"] == (
        len(rig.DOMAINS) * len(bridge.PROVIDER_ROLE_KEYS) * 2
    )
    assert arm["provider_usage"]["actual_cost_usd"] == 1.0
    assert arm["provider_usage"]["max_spend_usd_total"] == len(rig.DOMAINS) * 2.0
    assert len(arm["rows"]) == len(rig.DOMAINS) * 2
    assert set(arm["source_artifacts"]) == rig.DOMAINS
    assert arm["official_question_count_by_domain"] == {
        "enterprise": 2,
        "web": 2,
    }
    assert "longmemeval_v2_domain" not in arm["configuration"]


def test_bridge_builds_signed_arm_from_local_execution(tmp_path: Path) -> None:
    combined_path, combined, paths = _combined_receipt(tmp_path)
    local_execution = {
        "schema_version": rig.EXECUTION_IDENTITY_SCHEMA_VERSION,
        "kind": "local",
        "repository": "hyperb1iss/sibyl",
        "ref": "refs/heads/main",
        "sha": "a" * 40,
        "run_id": "d6cf4d36-606f-44d6-b386-c723e6b756e8",
        "run_attempt": 1,
    }
    for domain in sorted(rig.DOMAINS):
        plan = json.loads(paths[domain]["plan"].read_text(encoding="utf-8"))
        plan["execution"] = local_execution
        _write_json(paths[domain]["plan"], plan)
        _refresh_artifact(
            combined_path,
            combined,
            domain=domain,
            artifact="plan",
            path=paths[domain]["plan"],
        )

    arm = bridge.build_arm_run(combined_path)

    assert arm["execution"] == local_execution
    assert (
        rig.validate_arm(
            arm,
            stack_digest=rig.stack_fingerprint(arm["stack"]),
            side="local",
        )
        == arm
    )


def test_bridge_rejects_local_execution_sha_mismatch(tmp_path: Path) -> None:
    combined_path, combined, paths = _combined_receipt(tmp_path)
    plan = json.loads(paths["web"]["plan"].read_text(encoding="utf-8"))
    plan["execution"] = {
        "schema_version": rig.EXECUTION_IDENTITY_SCHEMA_VERSION,
        "kind": "local",
        "repository": "hyperb1iss/sibyl",
        "ref": "refs/heads/main",
        "sha": "b" * 40,
        "run_id": "d6cf4d36-606f-44d6-b386-c723e6b756e8",
        "run_attempt": 1,
    }
    _write_json(paths["web"]["plan"], plan)
    _refresh_artifact(
        combined_path,
        combined,
        domain="web",
        artifact="plan",
        path=paths["web"]["plan"],
    )

    with pytest.raises(bridge.BridgeInputError, match="clean execution SHA"):
        bridge.build_arm_run(combined_path)


def test_bridge_arm_rejects_resealed_row_truncation(tmp_path: Path) -> None:
    combined_path, _combined, _paths = _combined_receipt(tmp_path)
    arm = bridge.build_arm_run(combined_path)
    arm["rows"] = [
        next(row for row in arm["rows"] if row["domain"] == domain)
        for domain in sorted(rig.DOMAINS)
    ]
    arm["question_order_sha256"] = rig.canonical_sha256(
        [[row["domain"], row["question_id"]] for row in arm["rows"]]
    )
    arm["arm_run_sha256"] = rig.canonical_sha256(
        {key: value for key, value in arm.items() if key != "arm_run_sha256"}
    )

    with pytest.raises(rig.RigInputError, match="official question count differs"):
        rig.validate_arm(
            arm,
            stack_digest=rig.stack_fingerprint(arm["stack"]),
            side="truncated",
        )


def test_bridge_rejects_incomplete_official_small_selection(tmp_path: Path) -> None:
    combined_path, combined, paths = _combined_receipt(tmp_path)
    plan = json.loads(paths["web"]["plan"].read_text(encoding="utf-8"))
    plan["selection_complete"] = False
    _write_json(paths["web"]["plan"], plan)
    _refresh_artifact(
        combined_path,
        combined,
        domain="web",
        artifact="plan",
        path=paths["web"]["plan"],
    )

    with pytest.raises(bridge.BridgeInputError, match="complete Small corpus"):
        bridge.build_arm_run(combined_path)


def test_bridge_builds_paired_pass_from_distinct_workflow_runs(tmp_path: Path) -> None:
    left_path, _left_combined, _left_paths = _combined_receipt(
        tmp_path / "left",
        role="display_control",
        workflow_run_id="1001",
    )
    right_path, _right_combined, _right_paths = _combined_receipt(
        tmp_path / "right",
        role="display_treatment",
        workflow_run_id="1002",
    )

    paired = bridge.build_paired_pass(
        bridge.build_arm_run(left_path),
        bridge.build_arm_run(right_path),
    )

    assert paired["experiment_phase"] == "aa"
    assert paired["arms"]["left"]["execution"]["run_id"] == "1001"
    assert paired["arms"]["right"]["execution"]["run_id"] == "1002"
    assert rig.validate_pass(paired) == paired


def test_bridge_rejects_source_digest_drift(tmp_path: Path) -> None:
    combined_path, _combined, paths = _combined_receipt(tmp_path)
    original = paths["web"]["rig_rows"].read_text(encoding="utf-8")
    paths["web"]["rig_rows"].write_text(
        original.replace('"valid"', '"faile"', 1),
        encoding="utf-8",
    )

    with pytest.raises(bridge.BridgeInputError, match="digest does not match"):
        bridge.build_arm_run(combined_path)


def test_bridge_rejects_cross_domain_seed_drift(tmp_path: Path) -> None:
    combined_path, combined, paths = _combined_receipt(tmp_path)
    enterprise_plan = json.loads(paths["enterprise"]["plan"].read_text(encoding="utf-8"))
    enterprise_plan["pass_seed"] = 18
    _write_json(paths["enterprise"]["plan"], enterprise_plan)
    enterprise_args = json.loads(paths["enterprise"]["run_args"].read_text(encoding="utf-8"))
    enterprise_args["shuffle_questions_seed"] = 18
    _write_json(paths["enterprise"]["run_args"], enterprise_args)
    _refresh_artifact(
        combined_path,
        combined,
        domain="enterprise",
        artifact="plan",
        path=paths["enterprise"]["plan"],
    )
    _refresh_artifact(
        combined_path,
        combined,
        domain="enterprise",
        artifact="run_args",
        path=paths["enterprise"]["run_args"],
    )

    with pytest.raises(
        bridge.BridgeInputError, match="cross-domain identity differs for pass_seed"
    ):
        bridge.build_arm_run(combined_path)


def test_bridge_rejects_failed_or_reordered_rows(tmp_path: Path) -> None:
    combined_path, combined, paths = _combined_receipt(tmp_path)
    rig_rows = bridge._load_jsonl(paths["web"]["rig_rows"])
    rig_rows[0]["status"] = "failed"
    _write_jsonl(paths["web"]["rig_rows"], rig_rows)
    _refresh_artifact(
        combined_path,
        combined,
        domain="web",
        artifact="rig_rows",
        path=paths["web"]["rig_rows"],
    )
    with pytest.raises(bridge.BridgeInputError, match=r"rig row .* failed"):
        bridge.build_arm_run(combined_path)

    combined_path, combined, paths = _combined_receipt(tmp_path / "reordered")
    score_rows = bridge._load_jsonl(paths["web"]["per_question"])
    score_rows.reverse()
    _write_jsonl(paths["web"]["per_question"], score_rows)
    _refresh_artifact(
        combined_path,
        combined,
        domain="web",
        artifact="per_question",
        path=paths["web"]["per_question"],
    )
    with pytest.raises(bridge.BridgeInputError, match="rig row order differs"):
        bridge.build_arm_run(combined_path)


def test_bridge_rejects_provider_lineage_and_unpriced_cost(tmp_path: Path) -> None:
    combined_path, combined, _paths = _combined_receipt(tmp_path)
    reader = combined["source_runs"]["domains"]["web"]["provider_usage"]["reader"]
    reader["run_ids"] = ["foreign"]
    _write_json(combined_path, combined)
    with pytest.raises(bridge.BridgeInputError, match="lineage is incomplete or foreign"):
        bridge.build_arm_run(combined_path)

    combined_path, combined, paths = _combined_receipt(tmp_path / "unpriced")
    receipt = json.loads(paths["web"]["official_receipt"].read_text(encoding="utf-8"))
    receipt["accounting"]["cost"]["coverage_complete"] = False
    _write_json(paths["web"]["official_receipt"], receipt)
    _refresh_artifact(
        combined_path,
        combined,
        domain="web",
        artifact="official_receipt",
        path=paths["web"]["official_receipt"],
    )
    with pytest.raises(bridge.BridgeInputError, match="cost coverage is incomplete"):
        bridge.build_arm_run(combined_path)


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("models", "reader_model", "stale-reader", "receipt models differ"),
        (
            "dataset",
            "selected_question_ids_sha256",
            f"sha256:{'f' * 64}",
            "dataset identity differs",
        ),
    ],
)
def test_bridge_rejects_stale_receipt_identity(
    tmp_path: Path,
    section: str,
    field: str,
    value: object,
    message: str,
) -> None:
    combined_path, combined, paths = _combined_receipt(tmp_path)
    receipt = json.loads(paths["web"]["official_receipt"].read_text(encoding="utf-8"))
    receipt[section][field] = value
    _write_json(paths["web"]["official_receipt"], receipt)
    _refresh_artifact(
        combined_path,
        combined,
        domain="web",
        artifact="official_receipt",
        path=paths["web"]["official_receipt"],
    )

    with pytest.raises(bridge.BridgeInputError, match=message):
        bridge.build_arm_run(combined_path)


def test_bridge_rejects_unknown_plan_fields_and_control_lever_activity(
    tmp_path: Path,
) -> None:
    combined_path, combined, paths = _combined_receipt(tmp_path)
    plan = json.loads(paths["web"]["plan"].read_text(encoding="utf-8"))
    plan["surprise"] = True
    _write_json(paths["web"]["plan"], plan)
    _refresh_artifact(
        combined_path,
        combined,
        domain="web",
        artifact="plan",
        path=paths["web"]["plan"],
    )
    with pytest.raises(bridge.BridgeInputError, match=r"unknown=\['surprise'\]"):
        bridge.build_arm_run(combined_path)

    combined_path, combined, paths = _combined_receipt(tmp_path / "lever")
    rig_rows = bridge._load_jsonl(paths["web"]["rig_rows"])
    rig_rows[0]["activity"]["lever_activity"] = {"unapproved": 1}
    rig_rows[0]["activity"]["activity_events"] += 1
    _write_jsonl(paths["web"]["rig_rows"], rig_rows)
    _refresh_artifact(
        combined_path,
        combined,
        domain="web",
        artifact="rig_rows",
        path=paths["web"]["rig_rows"],
    )
    with pytest.raises(bridge.BridgeInputError, match="lever activity keys differ"):
        bridge.build_arm_run(combined_path)


def test_pair_bridge_rejects_recomputed_arm_seed_drift(tmp_path: Path) -> None:
    left_path, _left_combined, _left_paths = _combined_receipt(
        tmp_path / "left",
        role="display_control",
        workflow_run_id="1001",
    )
    right_path, _right_combined, _right_paths = _combined_receipt(
        tmp_path / "right",
        role="display_treatment",
        workflow_run_id="1002",
    )
    left = bridge.build_arm_run(left_path)
    right = deepcopy(bridge.build_arm_run(right_path))
    right["seed"] += 1
    right["arm_run_sha256"] = rig.canonical_sha256(
        {key: value for key, value in right.items() if key != "arm_run_sha256"}
    )

    with pytest.raises(rig.RigInputError, match="right arm seed differs"):
        bridge.build_paired_pass(left, right)
