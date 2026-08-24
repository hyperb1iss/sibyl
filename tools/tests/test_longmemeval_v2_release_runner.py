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
from tools.tests.longmemeval_v2_release_support import (
    _write_gzip_jsonl,
    aa_spec,
    anchor_spec,
    write_dataset,
    write_saved_memory,
)

EXPECTED_INITIAL_AA_ARM_COUNT = 6


def _read_gzip_jsonl(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


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
    write_dataset(data_root)
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
    spec = aa_spec()
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
        release_host={
            "platform": "darwin",
            "macos_major": 26,
            "filesystem_device": 1,
            "immutable_descendant_rename": True,
        },
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
                assert command[command.index("--require-embedding-provider") + 1] == "local"
                assert (
                    command[command.index("--require-embedding-model") + 1]
                    == "sentence-transformers/all-MiniLM-L6-v2"
                )
    assert len(run_ids) == len(stage_plan["runs"])
    assert stage_plan["package_inputs"] == {
        "system_description": inputs.bind_artifact(
            sealed_inputs["system_description"],
            name="test system description",
        ),
        "adapter": inputs.bind_artifact(sealed_inputs["adapter"], name="test adapter"),
    }
    assert stage_plan["release_host"] == {
        "platform": "darwin",
        "macos_major": 26,
        "filesystem_device": 1,
        "immutable_descendant_rename": True,
    }
    first_saved_memory = str(
        sealed_inputs["output_root"] / "runs" / "aa-1-left" / "web" / "memory_state"
    )
    right_web = stage_plan["runs"][1]["domains"]["web"]
    assert right_web["planning_memory_dir"] == first_saved_memory
    assert right_web["execution_memory_dir"] == first_saved_memory
    assert (
        right_web["plan_command"][right_web["plan_command"].index("--checkpoint-dir") + 1]
        == first_saved_memory
    )
    assert (
        right_web["run_command"][right_web["run_command"].index("--load-memory-dir") + 1]
        == first_saved_memory
    )
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
    for name in (
        "GITHUB_ACTIONS",
        "GITHUB_REF",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "GITHUB_SHA",
        "GITHUB_WORKFLOW_REF",
    ):
        monkeypatch.delenv(name, raising=False)
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
            "max_spend_usd_per_domain": 4.25,
            "max_spend_usd_total": 8.5,
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
    write_saved_memory(web_root, domain="web", source_sha=source["sha"])
    write_saved_memory(
        enterprise_root,
        domain="enterprise",
        source_sha=source["sha"],
    )
    spec = anchor_spec()
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
    write_saved_memory(web_root, domain="web", source_sha=source["sha"])
    write_saved_memory(
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
    spec = anchor_spec()
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
    write_saved_memory(web_root, domain="web", source_sha=source["sha"])
    write_saved_memory(
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
    spec = anchor_spec()
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
    write_saved_memory(
        web_root,
        domain="web",
        source_sha=source["sha"],
        project_id="shared-project",
    )
    write_saved_memory(
        enterprise_root,
        domain="enterprise",
        source_sha=source["sha"],
        project_id="shared-project",
    )
    spec = anchor_spec()
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
    write_saved_memory(
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

    tampered_host = deepcopy(stage_plan)
    tampered_host["release_host"]["macos_major"] = 25
    _reseal(tampered_host)
    with pytest.raises(contract.StagePlanError, match="requires macOS 26"):
        plan.require_stage_plan(tampered_host, check_checkout=False)


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


def test_local_execution_git_inspection_has_a_finite_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def timeout(*_args: object, **kwargs: Any) -> None:
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs["timeout"])

    monkeypatch.setattr(local_identity.subprocess, "run", timeout)

    with pytest.raises(ValueError, match="could not inspect"):
        local_identity._required_git_output(tmp_path, "status")
    assert captured["timeout"] == local_identity.GIT_INSPECTION_TIMEOUT_SECONDS
