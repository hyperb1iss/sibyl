from __future__ import annotations

import gzip
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from benchmarks import local_execution_identity as local_identity
from benchmarks import longmemeval_v2_official as official
from benchmarks import longmemeval_v2_release_contract as contract
from benchmarks import longmemeval_v2_release_inputs as inputs
from benchmarks import longmemeval_v2_release_memory as memory
from benchmarks import longmemeval_v2_release_plan as plan

EXPECTED_INITIAL_AA_ARM_COUNT = 6
MATCHED_PASS_INDEX = 4


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
    retrieval_mode = "naive" if naive else "fast"
    dedupe_mode = "source_kind" if treatment else "source"
    lane_mode = "additive" if treatment else "reserved"
    distillation_profile = "render_v1" if treatment else "baseline"
    total_chars = 72_000 if treatment else 60_000
    return {
        "experiment_id": "sibyl-v1.3-release",
        "experiment_phase": phase,
        "pass_id": pass_id,
        "pass_seed": seed,
        "arm_role": role,
        "substrate": "naive" if naive else "machine",
        "preregistration_sha256": preregistration,
        "max_spend_usd": 3.6 if treatment else 3.0,
        "retrieval_mode": retrieval_mode,
        "max_context_total_chars": total_chars,
        "operational_note_dedupe_mode": dedupe_mode,
        "operational_note_lane_mode": lane_mode,
        "operational_note_distillation_profile": distillation_profile,
        "render_group_lanes": treatment,
        "render_action_spines": treatment,
        "configuration": {
            "retrieval_mode": retrieval_mode,
            "max_context_chars_per_item": 18_000,
            "operational_note_dedupe_mode": dedupe_mode,
            "operational_note_lane_mode": lane_mode,
            "operational_note_distillation_profile": distillation_profile,
            "render_group_lanes": treatment,
            "render_action_spines": treatment,
        },
        "geometry": {
            "max_context_items": 8,
            "max_context_chars_per_item": 18_000,
            "max_context_total_chars": total_chars,
        },
    }


def _aa_spec() -> dict[str, Any]:
    passes: list[dict[str, Any]] = []
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
        "upstream": {
            "aa_authorization": None,
            "preregistration_authorization": None,
        },
        "passes": passes,
    }


def _arm_contract(arm: dict[str, Any]) -> dict[str, Any]:
    manifest = arm["manifest"]
    return {
        "substrate": manifest["substrate"],
        "configuration": manifest["configuration"],
        "geometry": manifest["geometry"],
    }


def _aa_extension_spec() -> dict[str, Any]:
    spec = _aa_spec()
    spec["mode"] = "extension"
    spec["passes"] = []
    for index, seed in enumerate((1304, 1305), start=4):
        pass_id = f"aa-{index}"
        spec["passes"].append(
            {
                "kind": "paired",
                "pass_id": pass_id,
                "seed": seed,
                "arms": [
                    {
                        "arm_id": f"{pass_id}-left",
                        "memory_source": "baseline",
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
    return spec


def _anchor_spec() -> dict[str, Any]:
    anchor_id = "anchor-1"
    seed = 1401
    spec = _aa_spec()
    spec["stage"] = "anchor"
    spec["mode"] = "standard"
    spec["passes"] = [
        {
            "kind": "anchor",
            "pass_id": anchor_id,
            "seed": seed,
            "arms": [
                {
                    "arm_id": "anchor-machine",
                    "memory_source": "baseline",
                    "manifest": _manifest(
                        phase="anchor",
                        pass_id=anchor_id,
                        seed=seed,
                    ),
                }
            ],
        }
    ]
    return spec


def _race_spec() -> tuple[dict[str, Any], dict[str, Any]]:
    preregistration = "b" * 64
    passes: list[dict[str, Any]] = []
    for index, seed in enumerate((1501, 1502, 1503, 1504), start=1):
        pass_id = f"race-{index}"
        passes.append(
            {
                "kind": "matched" if index == MATCHED_PASS_INDEX else "paired",
                "pass_id": pass_id,
                "seed": seed,
                "arms": [
                    {
                        "arm_id": f"{pass_id}-machine",
                        "memory_source": "baseline",
                        "manifest": _manifest(
                            phase="race",
                            pass_id=pass_id,
                            seed=seed,
                            preregistration=preregistration,
                        ),
                    },
                    {
                        "arm_id": f"{pass_id}-naive",
                        "memory_source": "baseline",
                        "manifest": _manifest(
                            phase="race",
                            pass_id=pass_id,
                            seed=seed,
                            role="naive",
                            preregistration=preregistration,
                        ),
                    },
                ],
            }
        )
    machine, naive = passes[0]["arms"]
    contracts = {
        "machine_configuration": deepcopy(machine["manifest"]["configuration"]),
        "naive_configuration": deepcopy(naive["manifest"]["configuration"]),
        "shipping_geometry": {
            "machine": deepcopy(machine["manifest"]["geometry"]),
            "naive": deepcopy(naive["manifest"]["geometry"]),
        },
        "matched_geometry": deepcopy(passes[-1]["arms"][0]["manifest"]["geometry"]),
    }
    spec = _aa_spec()
    spec.update({"stage": "race", "mode": "standard", "passes": passes})
    return spec, contracts


def _render_spec() -> tuple[dict[str, Any], dict[str, Any]]:
    preregistration = "c" * 64
    passes: list[dict[str, Any]] = []
    for index, seed in enumerate((1601, 1602, 1603), start=1):
        pass_id = f"render-{index}"
        passes.append(
            {
                "kind": "paired",
                "pass_id": pass_id,
                "seed": seed,
                "arms": [
                    {
                        "arm_id": f"{pass_id}-control",
                        "memory_source": "baseline",
                        "manifest": _manifest(
                            phase="render",
                            pass_id=pass_id,
                            seed=seed,
                            role="render_control",
                            preregistration=preregistration,
                        ),
                    },
                    {
                        "arm_id": f"{pass_id}-treatment",
                        "memory_source": "build_render" if index == 1 else "render",
                        "manifest": _manifest(
                            phase="render",
                            pass_id=pass_id,
                            seed=seed,
                            role="render_treatment",
                            preregistration=preregistration,
                        ),
                    },
                ],
            }
        )
    control, treatment = passes[0]["arms"]
    contracts = {
        "control_configuration": deepcopy(control["manifest"]["configuration"]),
        "treatment_configuration": deepcopy(treatment["manifest"]["configuration"]),
        "control_geometry": deepcopy(control["manifest"]["geometry"]),
        "treatment_geometry": deepcopy(treatment["manifest"]["geometry"]),
    }
    spec = _aa_spec()
    spec.update({"stage": "render", "mode": "standard", "passes": passes})
    return spec, contracts


def _trajectory(domain: str) -> dict[str, Any]:
    trajectory_id = f"{domain}-t1"
    return {
        "id": trajectory_id,
        "domain": domain,
        "environment": "browsergym",
        "goal": f"Finish the {domain} task",
        "outcome": "success",
        "start_url": "https://example.test/start",
        "states": [
            {
                "state_index": 0,
                "step": 0,
                "url": "https://example.test/start",
                "action": "click('submit')",
                "thought": "Submit the completed form",
                "accessibility_tree": "[submit] button 'Submit'",
                "screenshot": None,
            }
        ],
    }


def _write_dataset(root: Path) -> None:
    (root / "haystacks").mkdir(parents=True)
    rows = [
        {
            "id": "web-1",
            "domain": "web",
            "environment": "browsergym",
            "question_type": "single-session-user",
            "question": "What happened in the web task?",
            "answer": "success",
            "eval_function": "exact_match",
        },
        {
            "id": "enterprise-1",
            "domain": "enterprise",
            "environment": "browsergym",
            "question_type": "single-session-user",
            "question": "What happened in the enterprise task?",
            "answer": "success",
            "eval_function": "exact_match",
        },
    ]
    (root / "questions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (root / "trajectories.jsonl").write_text(
        "".join(json.dumps(_trajectory(domain)) + "\n" for domain in inputs.DOMAINS),
        encoding="utf-8",
    )
    (root / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({"web-1": ["web-t1"], "enterprise-1": ["enterprise-t1"]}),
        encoding="utf-8",
    )


def _write_gzip_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _read_gzip_jsonl(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_saved_memory(
    root: Path,
    *,
    domain: str,
    source_sha: str,
    project_id: str | None = None,
    run_id: str | None = None,
    api_url: str = "http://127.0.0.1:3334/api",
    include_screenshot_refs: bool = False,
) -> None:
    root.mkdir(parents=True)
    trajectory_id = f"{domain}-t1"
    project_id = project_id or f"project-{domain}"
    run_id = run_id or f"memory-{domain}"
    config = {
        "memory_type": "sibyl_live_api",
        "memory_params": {
            "api_url": api_url,
            "longmemeval_v2_domain": domain,
            "project_id": project_id,
            "run_id": run_id,
            "chunking_mode": "state",
            "content_max_chars": 18_000,
            "include_screenshot_refs": include_screenshot_refs,
            "runner_provenance": {
                "sibyl_commit": source_sha,
                "git_dirty": False,
                "git_status": "clean",
            },
        },
    }
    (root / "memory_config.json").write_text(
        json.dumps(config, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    catalog, spines = memory._expected_catalog_and_spines(
        {trajectory_id: _trajectory(domain)},
        params=config["memory_params"],
    )
    _write_gzip_jsonl(root / "chunk_catalog.jsonl.gz", catalog)
    _write_gzip_jsonl(root / "action_spines.jsonl.gz", spines)
    _write_gzip_jsonl(root / "distillation_receipts.jsonl.gz", [])
    manifest = {
        "schema_version": memory.MEMORY_MANIFEST_SCHEMA_VERSION,
        "api_url": config["memory_params"]["api_url"],
        "longmemeval_v2_domain": domain,
        "project_id": project_id,
        "run_id": run_id,
        "chunking_mode": "state",
        "content_max_chars": 18_000,
        "inserted_trajectories": 1,
        "created_entities": len(catalog),
        "ingest_api_runtime": {
            "status": "healthy",
            "version": "1.3.0",
            "runtime": {
                "commit": source_sha,
                "git_dirty": False,
                "git_status": "clean",
            },
        },
        "ingest_embedding_usage": {},
        "completed_trajectory_ids": [trajectory_id],
        "operational_trajectory_ids": [trajectory_id],
        "pending_embedding_job_ids": [],
        "pending_projection_job_ids": [],
        "pending_note_distillation_job_ids": [],
        "ingest_note_distillation_usage": {},
        "ingest_note_distillation_receipt_count": 0,
        "ingest_note_distillation_receipt_set_sha256": inputs.rig.canonical_sha256({}),
        "ingest_finalized": True,
        "memory_config_sha256": inputs.sha256_file(root / "memory_config.json"),
        "chunk_catalog_sha256": inputs.sha256_file(root / "chunk_catalog.jsonl.gz"),
        "action_spine_count": len(spines),
        "action_spines_sha256": inputs.sha256_file(root / "action_spines.jsonl.gz"),
        "distillation_receipt_count": 0,
        "distillation_receipts_sha256": inputs.sha256_file(root / "distillation_receipts.jsonl.gz"),
    }
    (root / "memory_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _canonical_uuid_factory() -> Any:
    values = iter(str(UUID(int=index)) for index in range(1, 20))
    return lambda: next(values)


def _local_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "sibyl"
    checkout.mkdir()
    git = shutil.which("git")
    assert git is not None
    commands = [
        [git, "init", "-b", "main"],
        [git, "config", "user.email", "eval@example.test"],
        [git, "config", "user.name", "Eval Test"],
        [git, "remote", "add", "origin", "git@github.com:hyperb1iss/sibyl.git"],
    ]
    for command in commands:
        subprocess.run(  # noqa: S603
            command,
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
    (checkout / "README.md").write_text("sealed\n", encoding="utf-8")
    subprocess.run(  # noqa: S603
        [git, "add", "README.md"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603
        [git, "commit", "-m", "test: seal checkout"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(  # noqa: S603
        [git, "update-ref", "refs/remotes/origin/main", sha],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    return checkout, "refs/heads/main", sha


@pytest.fixture
def sealed_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    data_root = tmp_path / "data"
    official_repo = tmp_path / "official"
    official_repo.mkdir()
    (official_repo / "evaluation").mkdir()
    (official_repo / "evaluation" / "harness.py").write_text("# test\n", encoding="utf-8")
    _write_dataset(data_root)
    monkeypatch.setattr(
        inputs,
        "OFFICIAL_DATASET_SHA256",
        {
            name: inputs.sha256_file(data_root / relative)
            for name, relative in inputs.DATASET_ARTIFACT_NAMES.items()
        },
    )
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
    monkeypatch.setattr(
        plan,
        "require_local_checkout",
        lambda _root: {
            "source_identity": source,
            "provenance": {
                "sibyl_commit": source["sha"],
                "git_dirty": False,
                "git_status": "clean",
            },
        },
    )
    spec = _aa_spec()
    spec_path = tmp_path / "stage.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    system_description = tmp_path / "SYSTEM_DESCRIPTION.md"
    system_description.write_text("# Sibyl release evaluation\n", encoding="utf-8")
    adapter = tmp_path / "sibyl_memory.py"
    adapter.write_text("# sealed adapter\n", encoding="utf-8")
    return {
        "spec": spec,
        "spec_path": spec_path,
        "official_repo": official_repo,
        "data_root": data_root,
        "output_root": tmp_path / "output",
        "system_description": system_description,
        "adapter": adapter,
        "source": source,
    }


def _build(inputs: dict[str, Any]) -> dict[str, Any]:
    return plan.build_stage_plan(
        spec=inputs["spec"],
        spec_path=inputs["spec_path"],
        official_repo=inputs["official_repo"],
        data_root=inputs["data_root"],
        output_root=inputs["output_root"],
        system_description_path=inputs["system_description"],
        adapter_path=inputs["adapter"],
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
    assert stage_plan["package_inputs"] == {
        "system_description": inputs.bind_artifact(
            sealed_inputs["system_description"],
            name="test system description",
        ),
        "adapter": inputs.bind_artifact(sealed_inputs["adapter"], name="test adapter"),
    }
    first_saved_memory = str(
        sealed_inputs["output_root"] / "runs" / "aa-1-left" / "web" / "checkpoint"
    )
    right_web = stage_plan["runs"][1]["domains"]["web"]
    assert right_web["planning_memory_dir"] == first_saved_memory
    assert right_web["execution_memory_dir"] == first_saved_memory
    plan.require_stage_plan(stage_plan, check_checkout=False)


@pytest.mark.parametrize("input_name", ["system_description", "adapter"])
def test_stage_plan_rejects_package_input_drift(
    sealed_inputs: dict[str, Any],
    input_name: str,
) -> None:
    stage_plan = _build(sealed_inputs)
    package_input = sealed_inputs[input_name]
    original = package_input.read_bytes()
    package_input.write_bytes(b"x" + original[1:])
    with pytest.raises(contract.StagePlanError, match="digest changed"):
        plan.require_stage_plan(stage_plan, check_checkout=False)


def test_all_plan_only_reservations_accept_future_internal_memory(
    sealed_inputs: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    stage_plan = _build(sealed_inputs)
    monkeypatch.setattr(
        official,
        "require_local_checkout",
        lambda _root: {
            "source_identity": sealed_inputs["source"],
            "provenance": {
                "sibyl_commit": sealed_inputs["source"]["sha"],
                "git_dirty": False,
                "git_status": "clean",
            },
        },
    )
    monkeypatch.setattr(
        official,
        "official_source_record",
        lambda _path: {
            "commit": plan.OFFICIAL_HARNESS_COMMIT,
            "git_status": "clean",
        },
    )
    reservations = []
    for run in stage_plan["runs"]:
        for domain_run in run["domains"].values():
            command = domain_run["plan_command"]
            assert "--plan-only" in command
            assert "--load-memory-dir" not in command
            args = official.parse_args(command[len(plan.OFFICIAL_COMMAND_PREFIX) :])
            reservations.append(official.build_memory_config(args))

    assert len(reservations) == EXPECTED_INITIAL_AA_ARM_COUNT * 2
    assert all(item["memory_type"] == "sibyl_live_api" for item in reservations)


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


def test_dataset_rejects_root_and_payload_symlink_escape(
    sealed_inputs: dict[str, Any],
    tmp_path: Path,
) -> None:
    data_root = sealed_inputs["data_root"]
    linked_root = tmp_path / "linked-data"
    linked_root.symlink_to(data_root, target_is_directory=True)
    with pytest.raises(contract.StagePlanError, match="non-symlinked"):
        inputs.dataset_record(linked_root)

    external_questions = tmp_path / "outside-questions.jsonl"
    external_questions.write_bytes(data_root.joinpath("questions.jsonl").read_bytes())
    data_root.joinpath("questions.jsonl").unlink()
    data_root.joinpath("questions.jsonl").symlink_to(external_questions)
    with pytest.raises(contract.StagePlanError, match="symlink"):
        inputs.dataset_record(data_root)


def test_external_memory_requires_completed_distinct_domain_lineage(
    sealed_inputs: dict[str, Any],
) -> None:
    source = sealed_inputs["source"]
    web_root = sealed_inputs["output_root"].parent / "memory-web"
    enterprise_root = sealed_inputs["output_root"].parent / "memory-enterprise"
    _write_saved_memory(web_root, domain="web", source_sha=source["sha"])
    _write_saved_memory(
        enterprise_root,
        domain="enterprise",
        source_sha=source["sha"],
    )
    spec = _anchor_spec()
    spec["memory_roots"]["baseline"] = {
        "web": str(web_root),
        "enterprise": str(enterprise_root),
    }
    dataset = inputs.dataset_record(sealed_inputs["data_root"])

    bindings = memory.build_memory_bindings(spec, dataset=dataset, source=source)

    assert bindings["baseline"]["web"]["manifest"]["ingest_finalized"] is True
    same_root = deepcopy(spec)
    same_root["memory_roots"]["baseline"]["enterprise"] = str(web_root)
    with pytest.raises(contract.StagePlanError, match="distinct canonical roots"):
        memory.build_memory_bindings(same_root, dataset=dataset, source=source)

    manifest_path = enterprise_root / "memory_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["completed_trajectory_ids"] = ["web-t1"]
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    with pytest.raises(contract.StagePlanError, match="wrong lineage"):
        memory.build_memory_bindings(spec, dataset=dataset, source=source)


def test_external_memory_rejects_garbage_catalog(
    sealed_inputs: dict[str, Any],
) -> None:
    source = sealed_inputs["source"]
    web_root = sealed_inputs["output_root"].parent / "garbage-web"
    enterprise_root = sealed_inputs["output_root"].parent / "garbage-enterprise"
    _write_saved_memory(web_root, domain="web", source_sha=source["sha"])
    _write_saved_memory(
        enterprise_root,
        domain="enterprise",
        source_sha=source["sha"],
    )
    catalog_path = web_root / "chunk_catalog.jsonl.gz"
    catalog_path.write_bytes(b"not a gzip catalog")
    manifest_path = web_root / "memory_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["chunk_catalog_sha256"] = inputs.sha256_file(catalog_path)
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    spec = _anchor_spec()
    spec["memory_roots"]["baseline"] = {
        "web": str(web_root),
        "enterprise": str(enterprise_root),
    }

    with pytest.raises(contract.StagePlanError, match="gzip JSONL"):
        memory.build_memory_bindings(
            spec,
            dataset=inputs.dataset_record(sealed_inputs["data_root"]),
            source=source,
        )


@pytest.mark.parametrize(
    ("filename", "manifest_key", "message"),
    [
        (
            "chunk_catalog.jsonl.gz",
            "chunk_catalog_sha256",
            "deterministic source chunks",
        ),
        (
            "action_spines.jsonl.gz",
            "action_spines_sha256",
            "deterministic trajectories",
        ),
    ],
)
def test_external_memory_rejects_self_resealed_deterministic_artifact_drift(
    sealed_inputs: dict[str, Any],
    filename: str,
    manifest_key: str,
    message: str,
) -> None:
    source = sealed_inputs["source"]
    web_root = sealed_inputs["output_root"].parent / f"tampered-{filename}-web"
    enterprise_root = sealed_inputs["output_root"].parent / f"tampered-{filename}-enterprise"
    _write_saved_memory(web_root, domain="web", source_sha=source["sha"])
    _write_saved_memory(
        enterprise_root,
        domain="enterprise",
        source_sha=source["sha"],
    )
    artifact_path = web_root / filename
    rows = _read_gzip_jsonl(artifact_path)
    rows[0]["content"] = "forged but self-consistent"
    _write_gzip_jsonl(artifact_path, rows)
    manifest_path = web_root / "memory_manifest.json"
    saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    saved_manifest[manifest_key] = inputs.sha256_file(artifact_path)
    manifest_path.write_text(json.dumps(saved_manifest) + "\n", encoding="utf-8")
    spec = _anchor_spec()
    spec["memory_roots"]["baseline"] = {
        "web": str(web_root),
        "enterprise": str(enterprise_root),
    }

    with pytest.raises(contract.StagePlanError, match=message):
        memory.build_memory_bindings(
            spec,
            dataset=inputs.dataset_record(sealed_inputs["data_root"]),
            source=source,
        )


def test_external_memory_rejects_cross_domain_project_and_run_lineage(
    sealed_inputs: dict[str, Any],
) -> None:
    source = sealed_inputs["source"]
    web_root = sealed_inputs["output_root"].parent / "shared-project-web"
    enterprise_root = sealed_inputs["output_root"].parent / "shared-project-enterprise"
    _write_saved_memory(
        web_root,
        domain="web",
        source_sha=source["sha"],
        project_id="shared-project",
    )
    _write_saved_memory(
        enterprise_root,
        domain="enterprise",
        source_sha=source["sha"],
        project_id="shared-project",
    )
    spec = _anchor_spec()
    spec["memory_roots"]["baseline"] = {
        "web": str(web_root),
        "enterprise": str(enterprise_root),
    }

    with pytest.raises(contract.StagePlanError, match="distinct project IDs"):
        memory.build_memory_bindings(
            spec,
            dataset=inputs.dataset_record(sealed_inputs["data_root"]),
            source=source,
        )

    wrong_run_root = sealed_inputs["output_root"].parent / "wrong-run-web"
    _write_saved_memory(
        wrong_run_root,
        domain="web",
        source_sha=source["sha"],
        run_id="memory-enterprise",
    )
    spec["memory_roots"]["baseline"] = {
        "web": str(wrong_run_root),
        "enterprise": str(enterprise_root),
    }
    with pytest.raises(contract.StagePlanError, match="run ID is not bound to web"):
        memory.build_memory_bindings(
            spec,
            dataset=inputs.dataset_record(sealed_inputs["data_root"]),
            source=source,
        )


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


def test_stage_plan_rejects_existing_output_root(sealed_inputs: dict[str, Any]) -> None:
    sealed_inputs["output_root"].mkdir()
    with pytest.raises(contract.StagePlanError, match="fresh output root"):
        _build(sealed_inputs)


def test_stage_plan_revalidation_requires_fresh_output_root(
    sealed_inputs: dict[str, Any],
) -> None:
    stage_plan = _build(sealed_inputs)
    sealed_inputs["output_root"].mkdir()

    with pytest.raises(contract.StagePlanError, match="no longer fresh"):
        plan.require_stage_plan(stage_plan, check_checkout=False)


def test_stage_planner_rejects_hidden_untracked_checkout(
    sealed_inputs: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, _ref, _sha = _local_checkout(tmp_path)
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603
        [git, "config", "status.showUntrackedFiles", "no"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    (checkout / "hidden-by-config.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setattr(plan, "ROOT", checkout)
    monkeypatch.setattr(plan, "require_local_checkout", local_identity.require_local_checkout)

    with pytest.raises(contract.StagePlanError, match="clean Sibyl checkout"):
        _build(sealed_inputs)


@pytest.mark.parametrize(
    ("published", "message"),
    [
        (ValueError("deleted"), "could not verify its exact ref on origin"),
        ("force-moved", "differs from the exact ref on origin"),
    ],
)
def test_stage_planner_rejects_changed_live_origin(
    sealed_inputs: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: str | ValueError,
    message: str,
) -> None:
    checkout, ref, sha = _local_checkout(tmp_path)
    original = local_identity._required_git_output

    def git_output(root: Path, *args: str) -> str:
        if args == ("ls-remote", "--exit-code", "--refs", "origin", ref):
            if isinstance(published, ValueError):
                raise published
            return f"{'b' * len(sha)}\t{ref}"
        return original(root, *args)

    monkeypatch.setattr(plan, "ROOT", checkout)
    monkeypatch.setattr(plan, "require_local_checkout", local_identity.require_local_checkout)
    monkeypatch.setattr(local_identity, "_required_git_output", git_output)

    with pytest.raises(contract.StagePlanError, match=message):
        _build(sealed_inputs)
