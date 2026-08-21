from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from benchmarks import longmemeval_v2_release_contract as contract
from benchmarks import longmemeval_v2_release_inputs as inputs
from benchmarks import longmemeval_v2_release_plan as plan

EXPECTED_INITIAL_AA_ARM_COUNT = 6


def _runtime() -> dict[str, Any]:
    return {
        "api_url": "http://127.0.0.1:3334/api",
        "allow_localhost": True,
        "reader_base_url": "https://openrouter.ai/api/v1",
        "reader_model": "qwen/qwen3.5-9b",
        "reader_api_key_env": "OPENROUTER_API_KEY",
        "reader_max_concurrent_requests": 16,
        "reader_retry_attempts": 4,
        "evaluator_model": "gpt-5.2",
        "evaluator_api_key_env": "OPENAI_API_KEY",
        "evidence_composition_mode": "shared_relevance",
        "retrieval_max_planned_queries": 3,
        "max_context_chars_per_item": 18_000,
        "typed_stream_limit": 8,
        "note_distillation_model": "gpt-5.4-nano",
        "api_retry_attempts": 3,
        "prompt_build_max_workers": 1,
    }


def _manifest(
    *,
    phase: str,
    pass_id: str,
    seed: int,
    role: str = "machine",
    preregistration: str = "",
) -> dict[str, Any]:
    treatment = role == "render_treatment"
    naive = role == "naive"
    return {
        "experiment_id": "sibyl-v1.3-release",
        "experiment_phase": phase,
        "pass_id": pass_id,
        "pass_seed": seed,
        "arm_role": role,
        "substrate": "naive" if naive else "machine",
        "preregistration_sha256": preregistration,
        "max_spend_usd": 3.6 if treatment else 3.0,
        "retrieval_mode": "naive" if naive else "fast",
        "max_context_total_chars": 72_000 if treatment else 60_000,
        "operational_note_dedupe_mode": "source_kind" if treatment else "source",
        "operational_note_lane_mode": "additive" if treatment else "reserved",
        "operational_note_distillation_profile": "render_v1" if treatment else "baseline",
        "render_group_lanes": treatment,
        "render_action_spines": treatment,
    }


def _aa_spec() -> dict[str, Any]:
    passes = []
    for index, seed in enumerate((1301, 1302, 1303), start=1):
        pass_id = f"aa-{index}"
        passes.append(
            {
                "kind": "paired",
                "pass_id": pass_id,
                "seed": seed,
                "arms": [
                    {
                        "arm_id": f"{pass_id}-left",
                        "memory_source": "build_baseline" if index == 1 else "baseline",
                        "manifest": _manifest(phase="aa", pass_id=pass_id, seed=seed),
                    },
                    {
                        "arm_id": f"{pass_id}-right",
                        "memory_source": "baseline",
                        "manifest": _manifest(phase="aa", pass_id=pass_id, seed=seed),
                    },
                ],
            }
        )
    return {
        "schema_version": contract.STAGE_SPEC_SCHEMA_VERSION,
        "experiment_id": "sibyl-v1.3-release",
        "stage": "aa",
        "mode": "initial",
        "runtime": _runtime(),
        "memory_roots": {"baseline": None, "render": None},
        "upstream": {"aa_receipt": None, "paired_passes": [], "preregistration": None},
        "passes": passes,
    }


def _write_dataset(root: Path) -> None:
    (root / "haystacks").mkdir(parents=True)
    rows = [
        {"id": "web-1", "domain": "web"},
        {"id": "enterprise-1", "domain": "enterprise"},
    ]
    (root / "questions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (root / "trajectories.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({"web-1": [], "enterprise-1": []}),
        encoding="utf-8",
    )


def _canonical_uuid_factory() -> Any:
    values = iter(str(UUID(int=index)) for index in range(1, 20))
    return lambda: next(values)


@pytest.fixture
def sealed_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    data_root = tmp_path / "data"
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    (official_repo / "evaluation").mkdir()
    (official_repo / "evaluation" / "harness.py").write_text("# test\n", encoding="utf-8")
    _write_dataset(data_root)
    monkeypatch.setattr(
        inputs.rig,
        "OFFICIAL_SMALL_QUESTION_COUNTS",
        {"web": 1, "enterprise": 1},
    )
    monkeypatch.setattr(
        inputs.rig,
        "OFFICIAL_SMALL_QUESTION_IDS_SHA256",
        {
            "web": inputs.rig.canonical_sha256(["web-1"]),
            "enterprise": inputs.rig.canonical_sha256(["enterprise-1"]),
        },
    )
    source = {
        "repository": "hyperb1iss/sibyl",
        "ref": "refs/heads/main",
        "sha": "a" * 40,
    }
    official_source = {
        "url": "https://github.com/xiaowu0162/LongMemEval-V2",
        "path": str(official_repo.resolve()),
        "commit": plan.OFFICIAL_HARNESS_COMMIT,
        "expected_commit": plan.OFFICIAL_HARNESS_COMMIT,
        "pin_matches": True,
        "git_status": "clean",
        "harness_path": "evaluation/harness.py",
        "harness_exists": True,
        "previous_reviewed_commit": "be15ea6e995462f3391c1a610892df3f67dfa7bd",
        "reviewed_diff_url": (
            "https://github.com/xiaowu0162/LongMemEval-V2/compare/"
            "be15ea6e995462f3391c1a610892df3f67dfa7bd..."
            f"{plan.OFFICIAL_HARNESS_COMMIT}"
        ),
    }
    monkeypatch.setattr(plan, "require_pinned_source", lambda _path: official_source)
    monkeypatch.setattr(plan, "discover_source_identity", lambda _root: source)
    monkeypatch.setattr(
        plan,
        "git_provenance",
        lambda _root: {
            "sibyl_commit": source["sha"],
            "git_dirty": False,
            "git_status": "clean",
        },
    )
    spec = _aa_spec()
    spec_path = tmp_path / "stage.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return {
        "spec": spec,
        "spec_path": spec_path,
        "official_repo": official_repo,
        "data_root": data_root,
        "output_root": tmp_path / "output",
        "source": source,
    }


def _build(inputs: dict[str, Any]) -> dict[str, Any]:
    return plan.build_stage_plan(
        spec=inputs["spec"],
        spec_path=inputs["spec_path"],
        official_repo=inputs["official_repo"],
        data_root=inputs["data_root"],
        output_root=inputs["output_root"],
        uuid_factory=_canonical_uuid_factory(),
    )


def _reseal(stage_plan: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in stage_plan.items() if key != "stage_plan_sha256"}
    stage_plan["stage_plan_sha256"] = inputs.rig.canonical_sha256(unsigned)


def test_stage_plan_expands_exact_domains_waves_and_local_execution(
    sealed_inputs: dict[str, Any],
) -> None:
    stage_plan = _build(sealed_inputs)

    assert len(stage_plan["runs"]) == EXPECTED_INITIAL_AA_ARM_COUNT
    assert stage_plan["waves"] == [
        ["aa-1-left"],
        ["aa-1-right"],
        ["aa-2-left", "aa-2-right"],
        ["aa-3-left", "aa-3-right"],
    ]
    run_ids = set()
    for run in stage_plan["runs"]:
        execution = run["execution"]
        assert execution["kind"] == "local"
        assert execution["repository"] == "hyperb1iss/sibyl"
        assert execution["ref"] == "refs/heads/main"
        assert execution["sha"] == "a" * 40
        assert execution["run_attempt"] == 1
        UUID(execution["run_id"])
        run_ids.add(execution["run_id"])
        assert set(run["domains"]) == {"web", "enterprise"}
        for domain in ("web", "enterprise"):
            domain_run = run["domains"][domain]
            assert domain_run["plan_command"][-1] == "--plan-only"
            assert "--plan-only" not in domain_run["run_command"]
            for command in (domain_run["plan_command"], domain_run["run_command"]):
                assert command[command.index("--local-run-id") + 1] == execution["run_id"]
                assert command[command.index("--local-ref") + 1] == "refs/heads/main"
                assert "or.key" not in " ".join(command)
                assert "OPENROUTER_API_KEY" in command
    assert len(run_ids) == len(stage_plan["runs"])
    first_saved_memory = str(
        sealed_inputs["output_root"] / "runs" / "aa-1-left" / "web" / "checkpoint"
    )
    right_web = stage_plan["runs"][1]["domains"]["web"]
    assert right_web["planning_memory_dir"] == first_saved_memory
    assert right_web["execution_memory_dir"] == first_saved_memory
    plan.require_stage_plan(stage_plan, check_checkout=False)


def test_stage_plan_seals_immutable_dataset_and_fixed_domain_caps(
    sealed_inputs: dict[str, Any],
) -> None:
    stage_plan = _build(sealed_inputs)

    assert stage_plan["dataset"]["revision"] == inputs.OFFICIAL_DATASET_REVISION
    for run in stage_plan["runs"]:
        assert run["spend_reservation"] == {
            "currency": "USD",
            "max_spend_usd_per_domain": 3.0,
            "max_spend_usd_total": 6.0,
            "enforcement": "official plan-only reservation before provider calls",
        }

    sealed_inputs["data_root"].joinpath("questions.jsonl").write_text(
        json.dumps({"id": "web-changed", "domain": "web"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(contract.StagePlanError, match="dataset"):
        plan.require_stage_plan(stage_plan, check_checkout=False)


def test_stage_plan_rejects_resealed_command_and_execution_tampering(
    sealed_inputs: dict[str, Any],
) -> None:
    stage_plan = _build(sealed_inputs)
    tampered_command = deepcopy(stage_plan)
    command = tampered_command["runs"][0]["domains"]["web"]["run_command"]
    command[command.index("--reader-model") + 1] = "stale/reader-model"
    _reseal(tampered_command)
    with pytest.raises(contract.StagePlanError, match="commands"):
        plan.require_stage_plan(tampered_command, check_checkout=False)

    tampered_execution = deepcopy(stage_plan)
    tampered_execution["runs"][1]["execution"]["run_id"] = stage_plan["runs"][0]["execution"][
        "run_id"
    ]
    _reseal(tampered_execution)
    with pytest.raises(contract.StagePlanError, match="distinct"):
        plan.require_stage_plan(tampered_execution, check_checkout=False)


def test_stage_spec_rejects_memory_lineage_and_treatment_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _aa_spec()
    spec["passes"][0]["arms"][0]["memory_source"] = "baseline"
    with pytest.raises(contract.StagePlanError, match="memory lineage"):
        contract.require_stage_spec(spec)

    cap_drift = _aa_spec()
    cap_drift["passes"][0]["arms"][1]["manifest"]["max_spend_usd"] = 3.5
    with pytest.raises(contract.StagePlanError, match="changed configuration"):
        contract.require_stage_spec(cap_drift)

    render_identity = "render-1"
    treatment = _manifest(
        phase="render",
        pass_id=render_identity,
        seed=1601,
        role="render_treatment",
        preregistration="b" * 64,
    )
    treatment["render_group_lanes"] = False
    preregistration_path = tmp_path / "render-preregistration.json"
    preregistration_path.write_text("{}", encoding="utf-8")
    render_spec = {
        "schema_version": contract.STAGE_SPEC_SCHEMA_VERSION,
        "experiment_id": "sibyl-v1.3-release",
        "stage": "render",
        "mode": "standard",
        "runtime": _runtime(),
        "memory_roots": {"baseline": None, "render": None},
        "upstream": {
            "aa_receipt": None,
            "paired_passes": [],
            "preregistration": str(preregistration_path),
        },
        "passes": [
            {
                "kind": "paired",
                "pass_id": f"render-{index}",
                "seed": 1600 + index,
                "arms": [
                    {
                        "arm_id": f"render-{index}-control",
                        "memory_source": "baseline",
                        "manifest": _manifest(
                            phase="render",
                            pass_id=f"render-{index}",
                            seed=1600 + index,
                            role="render_control",
                            preregistration="b" * 64,
                        ),
                    },
                    {
                        "arm_id": f"render-{index}-treatment",
                        "memory_source": "build_render" if index == 1 else "render",
                        "manifest": treatment
                        if index == 1
                        else _manifest(
                            phase="render",
                            pass_id=f"render-{index}",
                            seed=1600 + index,
                            role="render_treatment",
                            preregistration="b" * 64,
                        ),
                    },
                ],
            }
            for index in range(1, 4)
        ],
    }
    preregistration = {
        "seeds": [1601, 1602, 1603],
        "preregistration_sha256": f"sha256:{'b' * 64}",
    }
    monkeypatch.setattr(
        contract.rig,
        "validate_preregistration",
        lambda _raw, *, kind: preregistration,
    )
    with pytest.raises(contract.StagePlanError, match="treatment bundle"):
        contract.require_stage_spec(render_spec)


def test_stage_plan_rejects_existing_output_root(sealed_inputs: dict[str, Any]) -> None:
    sealed_inputs["output_root"].mkdir()
    with pytest.raises(contract.StagePlanError, match="fresh output root"):
        _build(sealed_inputs)
