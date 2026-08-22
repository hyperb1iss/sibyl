from __future__ import annotations

import copy
import json
import os
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_command as command_runner
from benchmarks import longmemeval_v2_release_official_package as package
from benchmarks import longmemeval_v2_release_official_publication as publication
from benchmarks import longmemeval_v2_release_official_receipt as official_receipt
from benchmarks import longmemeval_v2_release_package_archive as package_archive
from benchmarks import longmemeval_v2_release_package_object as package_object
from benchmarks import longmemeval_v2_release_package_process as process
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_handoff import ExecutedDomain, ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import StagePlanError, bind_artifact, load_json
from tools.bench import longmemeval_v2_rig as rig

COMMAND_COUNT = 4


def _thaw_tree(root: Path) -> None:
    for current, _directories, files in os.walk(root):
        current_path = Path(current)
        with suppress(OSError):
            package_root.set_path_flags(current_path, 0)
        with suppress(OSError):
            current_path.chmod(0o700)
        for name in files:
            path = current_path / name
            with suppress(OSError):
                package_root.set_path_flags(path, 0)
            with suppress(OSError):
                path.chmod(0o600)


@pytest.fixture(autouse=True)
def _clear_immutable_test_outputs(tmp_path: Path) -> Any:
    yield
    _thaw_tree(tmp_path)


def _artifact(path: Path, text: str = "sealed\n") -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return bind_artifact(path, name="test artifact")


def _official_artifact(path: Path) -> dict[str, Any]:
    return {**bind_artifact(path, name="official test artifact"), "exists": True}


@pytest.fixture
def executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExecutedStage:
    paid_root = tmp_path / "paid"
    paid_root.mkdir()
    official = tmp_path / "official"
    official.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    system_description = _artifact(tmp_path / "SYSTEM_DESCRIPTION.md")
    adapter = _artifact(tmp_path / "sibyl_memory.py")
    run = {
        "arm_id": "aa-1-left",
        "pass_id": "aa-1",
        "seed": 1301,
        "manifest": {
            "experiment_id": "sibyl-v1.3-release",
            "experiment_phase": "aa",
            "pass_id": "aa-1",
            "pass_seed": 1301,
            "arm_role": "machine",
            "substrate": "machine",
            "preregistration_sha256": "",
            "configuration": {"retrieval_mode": "fast"},
            "geometry": {
                "max_context_items": 8,
                "max_context_chars_per_item": 18_000,
                "max_context_total_chars": 60_000,
            },
        },
        "execution": {
            "schema_version": rig.EXECUTION_IDENTITY_SCHEMA_VERSION,
            "kind": "local",
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
            "run_id": "00000000-0000-0000-0000-000000000001",
            "run_attempt": 1,
        },
        "domains": {
            domain: {"output_dir": str(paid_root / "runs" / "aa-1-left" / domain)}
            for domain in ("web", "enterprise")
        },
        "spend_reservation": {"max_spend_usd_total": 6.0},
    }
    for domain in ("web", "enterprise"):
        _artifact(Path(run["domains"][domain]["output_dir"]) / "aggregated_metrics.json")
    stack = {
        "sibyl_commit": "a" * 40,
        "sibyl_git_status": "clean",
        "official_source": {"commit": "b" * 40},
        "dataset_sha256_by_domain": {"web": "c" * 64, "enterprise": "d" * 64},
        "reader": {"model": "qwen/qwen3.5-9b", "base_url": "https://openrouter.ai/api/v1"},
        "judge": {"model": "gpt-5.2"},
    }
    plan = {
        "stage_plan_sha256": "sha256:" + "e" * 64,
        "output_root": str(paid_root),
        "runs": [run],
        "source_identity": {
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
        },
        "sibyl_provenance": {
            "sibyl_commit": "a" * 40,
            "git_dirty": False,
            "git_status": "clean",
        },
        "official_source": {"path": str(official), "commit": "b" * 40},
        "dataset": {
            "root": str(data),
            "question_count_by_domain": {"web": 240, "enterprise": 211},
            "artifacts": {
                "questions": {"sha256": "sha256:" + "1" * 64},
                "trajectories": {"sha256": "sha256:" + "2" * 64},
                "small_haystack": {"sha256": "sha256:" + "3" * 64},
            },
        },
        "spec": {
            "runtime": {
                "reader_model": "qwen/qwen3.5-9b",
                "reader_base_url": "https://openrouter.ai/api/v1",
                "evaluator_model": "gpt-5.2",
                "reader_api_key_env": "OPENROUTER_API_KEY",
                "evaluator_api_key_env": "OPENAI_API_KEY",
            }
        },
        "stack_identity": stack,
        "package_inputs": {"system_description": system_description, "adapter": adapter},
    }
    control = _artifact(paid_root / "runner_claim.json")
    status_control = _artifact(paid_root / "runner_status.json")
    status_receipt = {"status": "EXECUTED"}
    domains = tuple(
        ExecutedDomain(
            arm_id=run["arm_id"],
            domain=domain,
            actual_cost_usd=0.5,
            exit_artifact=_artifact(paid_root / "exits" / run["arm_id"] / f"{domain}.json"),
            artifacts=(
                (
                    "aggregated_metrics",
                    bind_artifact(
                        Path(run["domains"][domain]["output_dir"]) / "aggregated_metrics.json",
                        name="aggregated metrics",
                    ),
                ),
            ),
        )
        for domain in ("web", "enterprise")
    )
    monkeypatch.setattr(state, "require_claimed_stage_plan", lambda _plan: [run])
    monkeypatch.setattr(state, "read_status_receipt", lambda _plan: status_receipt)
    return ExecutedStage(
        plan=plan,
        runs=(run,),
        domains=domains,
        status_receipt=status_receipt,
        control_artifacts=(
            ("runner_claim", control),
            ("runner_status", status_control),
        ),
    )


def _owned_command_path(value: str, *, log_path: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else log_path.parents[1] / path


def _successful_invoke(
    command: list[str],
    *,
    log_path: Path,
    secrets: tuple[str, ...],
    **_options: Any,
) -> int:
    del secrets
    state.append_log(
        log_path,
        {
            "event": "start",
            "recorded_at": state.now(),
            "command_sha256": rig.canonical_sha256(command),
        },
    )
    joined = " ".join(command)
    if "step_1_single_operating_point" in joined:
        root = _owned_command_path(
            command[command.index("--output-root") + 1],
            log_path=log_path,
        )
        operating = root / "sibyl_live_api/operating_points/official"
        for relative in package.OPERATING_POINT_FILES:
            _artifact(operating / relative)
    elif "step_2_build_package" in joined:
        root = _owned_command_path(
            command[command.index("--output-root") + 1],
            log_path=log_path,
        )
        _artifact(root / "sibyl_live_api/SYSTEM_DESCRIPTION.md")
        _artifact(root / "sibyl_live_api" / Path(command[4]).name)
        _artifact(root / "sibyl_live_api/submission_overview.json")
        _artifact(root / "sibyl_live_api.tar.gz")
    elif "combine_aggregated_metrics" in joined:
        _artifact(_owned_command_path(command[command.index("-o") + 1], log_path=log_path))
    else:
        _artifact(
            _owned_command_path(
                command[command.index("--receipt-output") + 1],
                log_path=log_path,
            ),
            "{}\n",
        )
    state.append_log(
        log_path,
        {"event": "exit", "recorded_at": state.now(), "returncode": 0},
    )
    return 0


def _stub_score_aware_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        official_receipt,
        "require_combined_receipt",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        official_receipt.bridge,
        "build_arm_run",
        lambda _path: {"sealed": True},
    )
    monkeypatch.setattr(
        official_receipt,
        "require_arm_run",
        lambda _executed, _run, arm, **_kwargs: arm,
    )


def _published_members(root: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    publication = load_json(package_object.publication_path(root, "aa-1-left"))
    content = Path(publication["package_object"]["path"]).read_bytes()
    members, _manifest = package_archive.require_package_object(content)
    return publication, members


def test_official_arm_package_records_every_command_and_exact_output(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()

    result = publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=root.parent,
    )

    assert result["status"] == "PASS"
    assert result["actual_cost_usd"] == 1.0
    published, members = _published_members(root.parent)
    assert published == result
    assert len([name for name in members if name.startswith("command_receipts/")]) == (
        COMMAND_COUNT
    )
    assert sorted(name for name in members if name.startswith("logs/")) == [
        f"logs/{step}.jsonl" for step in sorted(package.PACKAGE_STEPS)
    ]
    assert not any(
        path.name.startswith(f".{root.parent.name}.staging-")
        for path in root.parent.parent.iterdir()
    )


def test_zero_exit_without_outputs_writes_fail_receipt(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_outputs(
        command: list[str],
        *,
        log_path: Path,
        secrets: tuple[str, ...],
        **_options: Any,
    ) -> int:
        del secrets
        state.append_log(
            log_path,
            {
                "event": "start",
                "recorded_at": state.now(),
                "command_sha256": rig.canonical_sha256(command),
            },
        )
        state.append_log(
            log_path,
            {"event": "exit", "recorded_at": state.now(), "returncode": 0},
        )
        return 0

    monkeypatch.setattr(process, "_invoke_command", no_outputs)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()

    with pytest.raises(StagePlanError, match="inventory"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=root.parent,
        )

    staging = next(
        path
        for path in root.parent.parent.iterdir()
        if path.name.startswith(f".{root.parent.name}.staging-aa-1-left-")
    )
    receipt = load_json(process.command_receipt_path(staging, "operating-point"))
    assert receipt["status"] == "FAIL"
    assert receipt["returncode"] == 0
    assert not package_object.publication_path(root.parent, "aa-1-left").exists()


def test_packaging_rejects_stale_root_without_deleting_it(
    executed: ExecutedStage, tmp_path: Path
) -> None:
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()
    root.mkdir()
    marker = root / "keep"
    marker.write_text("mine\n", encoding="utf-8")

    with pytest.raises(StagePlanError, match="foreign package entry"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=root.parent,
        )

    assert marker.read_text(encoding="utf-8") == "mine\n"


def test_completed_package_rejects_bound_output_mutation(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()
    publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=root.parent,
    )
    receipt = load_json(package_object.publication_path(root.parent, "aa-1-left"))
    object_path = Path(receipt["package_object"]["path"])
    package_root.set_path_flags(object_path, 0)
    object_path.chmod(0o600)
    object_path.write_bytes(b"tampered\n")

    with pytest.raises(StagePlanError, match="changed"):
        publication.require_official_arm_package(
            executed,
            arm_id="aa-1-left",
            packages_root=root.parent,
        )


def test_completed_package_rejects_mutable_object_mode(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    result = publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=packages_root,
    )
    object_path = Path(result["package_object"]["path"])
    package_root.set_path_flags(object_path, 0)
    object_path.chmod(0o600)

    with pytest.raises(StagePlanError, match="immutable state changed"):
        publication.require_official_arm_package(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )


@pytest.mark.parametrize("mutation", ["status", "object"])
def test_publication_returns_only_after_canonical_consumer_validation(
    mutation: str,
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    real_write = package.release_io.write_json_once_atomic_at

    def mutate_before_publication(
        directory_fd: int,
        name: str,
        payload: dict[str, Any],
    ) -> None:
        if name == package_object.AUTHORITY_NAME:
            if mutation == "status":
                status_path = Path(dict(executed.control_artifacts)["runner_status"]["path"])
                status_path.write_text("revoked\n", encoding="utf-8")
            else:
                object_name = Path(payload["package_object"]["path"]).name
                os.chmod(object_name, 0o600, dir_fd=directory_fd)
                object_fd = os.open(object_name, os.O_WRONLY | os.O_TRUNC, dir_fd=directory_fd)
                try:
                    os.write(object_fd, b"mutated\n")
                finally:
                    os.close(object_fd)
        real_write(directory_fd, name, payload)

    monkeypatch.setattr(
        package.release_io,
        "write_json_once_atomic_at",
        mutate_before_publication,
    )
    with pytest.raises(StagePlanError):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )
    published = package_object.publication_path(packages_root, "aa-1-left").is_file()
    assert published is (mutation == "status")


def test_completed_package_rejects_publication_receipt_mutation(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()
    publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=root.parent,
    )

    receipt = package_object.publication_path(root.parent, "aa-1-left")
    package_root.set_path_flags(receipt, 0)
    receipt.chmod(0o600)
    receipt.write_text("{}\n", encoding="utf-8")
    with pytest.raises(StagePlanError):
        publication.require_official_arm_package(
            executed,
            arm_id="aa-1-left",
            packages_root=root.parent,
        )


def test_completed_package_object_rebuild_is_deterministic(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()
    publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=root.parent,
    )

    published, members = _published_members(root.parent)
    rebuilt, manifest = package_archive.build_package_object(members)
    assert rebuilt == Path(published["package_object"]["path"]).read_bytes()
    assert manifest["package_manifest_sha256"] == published["package_manifest_sha256"]


def test_packaging_rejects_revoked_executed_status(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    monkeypatch.setattr(
        state,
        "read_status_receipt",
        lambda _plan: {"status": "FAIL"},
    )

    with pytest.raises(StagePlanError, match="status changed"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )


def test_packaging_rejects_reconstructed_failed_handoff(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = {"status": "FAIL"}
    monkeypatch.setattr(state, "read_status_receipt", lambda _plan: failed)
    reconstructed = replace(executed, status_receipt=failed)
    packages_root = tmp_path / "packages"
    packages_root.mkdir()

    with pytest.raises(StagePlanError, match="requires an EXECUTED stage"):
        publication.package_official_arm(
            reconstructed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )


def test_arm_root_creation_rejects_packages_parent_symlink_swap(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    displaced = tmp_path / "displaced"
    escape = tmp_path / "escape"
    escape.mkdir()
    real_open = os.open
    swapped = False

    def swap_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        nonlocal swapped
        if Path(path) == packages_root and not swapped:
            swapped = True
            packages_root.rename(displaced)
            packages_root.symlink_to(escape, target_is_directory=True)
        return real_open(path, flags, mode)

    monkeypatch.setattr(package_root.os, "open", swap_before_open)

    with pytest.raises(StagePlanError, match=r"opened safely|safe existing ancestor"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )
    assert not (escape / "aa-1-left").exists()


def test_arm_packages_are_canonical_non_nested_siblings(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=packages_root,
    )

    with pytest.raises(StagePlanError, match="missing or unreadable"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root / "aa-1-left",
        )


def test_completed_publication_rejects_live_status_mutation(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()
    publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=root.parent,
    )
    status_path = Path(dict(executed.control_artifacts)["runner_status"]["path"])
    status_path.write_text("revoked\n", encoding="utf-8")
    with pytest.raises(StagePlanError, match="status"):
        publication.require_official_arm_package(
            executed,
            arm_id="aa-1-left",
            packages_root=root.parent,
        )


def test_completed_publication_revalidates_paid_artifacts(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()
    publication.package_official_arm(
        executed,
        arm_id="aa-1-left",
        packages_root=root.parent,
    )
    paid_metrics = Path(executed.domains[0].artifacts[0][1]["path"])
    paid_metrics.write_text("mutated\n", encoding="utf-8")
    with pytest.raises(StagePlanError, match="executed domain"):
        publication.require_official_arm_package(
            executed,
            arm_id="aa-1-left",
            packages_root=root.parent,
        )


def test_publication_interruption_leaves_no_authority_and_blocks_reuse(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    _stub_score_aware_boundary(monkeypatch)
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    real_write = package.release_io.write_json_once_atomic_at

    def interrupt(directory_fd: int, name: str, payload: dict[str, Any]) -> None:
        if name == package_object.AUTHORITY_NAME:
            raise OSError("simulated publication interruption")
        real_write(directory_fd, name, payload)

    monkeypatch.setattr(package.release_io, "write_json_once_atomic_at", interrupt)
    with pytest.raises(OSError, match="publication interruption"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )
    assert not package_object.publication_path(packages_root, "aa-1-left").exists()
    assert any(
        path.name.startswith(f".{packages_root.name}.staging-aa-1-left-")
        for path in packages_root.parent.iterdir()
    )
    with pytest.raises(StagePlanError, match="stale unpublished staging"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=packages_root,
        )


@pytest.mark.parametrize(
    "target_step",
    ["operating-point", "submission", "combined-metrics", "official-receipt"],
)
def test_package_steps_reject_foreign_files_in_official_subtrees(
    target_step: str,
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invoke(
        command: list[str],
        *,
        log_path: Path,
        secrets: tuple[str, ...],
        **options: Any,
    ) -> int:
        result = _successful_invoke(
            command,
            log_path=log_path,
            secrets=secrets,
            **options,
        )
        step = log_path.stem
        if step == target_step:
            if step == "operating-point":
                output = (
                    _owned_command_path(
                        command[command.index("--output-root") + 1],
                        log_path=log_path,
                    )
                    / "sibyl_live_api/operating_points/official/foreign.bin"
                )
            elif step == "submission":
                output = (
                    _owned_command_path(
                        command[command.index("--output-root") + 1],
                        log_path=log_path,
                    )
                    / "sibyl_live_api/foreign.bin"
                )
            elif step == "combined-metrics":
                output = (
                    _owned_command_path(command[command.index("-o") + 1], log_path=log_path).parent
                    / "foreign.bin"
                )
            else:
                output = (
                    _owned_command_path(
                        command[command.index("--receipt-output") + 1],
                        log_path=log_path,
                    ).parent
                    / "foreign.bin"
                )
            _artifact(output)
        return result

    monkeypatch.setattr(process, "_invoke_command", invoke)
    _stub_score_aware_boundary(monkeypatch)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()

    with pytest.raises(StagePlanError, match="inventory"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=root.parent,
        )


def test_packaging_rejects_combined_receipt_mutation_during_bridge(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "_invoke_command", _successful_invoke)
    monkeypatch.setattr(
        official_receipt,
        "require_combined_receipt",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        official_receipt,
        "require_arm_run",
        lambda _executed, _run, arm, **_kwargs: arm,
    )

    def mutate_receipt(path: Path) -> dict[str, Any]:
        path.write_text("[]\n", encoding="utf-8")
        return {"sealed": True}

    monkeypatch.setattr(official_receipt.bridge, "build_arm_run", mutate_receipt)
    root = tmp_path / "packages" / "aa-1-left"
    root.parent.mkdir()

    with pytest.raises(StagePlanError, match="changed during artifact bridging"):
        publication.package_official_arm(
            executed,
            arm_id="aa-1-left",
            packages_root=root.parent,
        )


def test_combined_gate_failure_blocks_score_bridge(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "combined.json"
    receipt.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        official_receipt.eval_gate,
        "evaluate_report",
        lambda *_args, **_kwargs: ["bad"],
    )
    called = False

    def bridge_call(_path: Path) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(official_receipt.bridge, "build_arm_run", bridge_call)
    with pytest.raises(StagePlanError, match="release gate"):
        official_receipt.require_combined_receipt(
            executed,
            executed.runs[0],
            path=receipt,
            command=["moon", "run", "task", "--"],
            paths={},
        )
    assert called is False


def test_combined_receipt_binds_dataset_models_sources_and_outputs(
    executed: ExecutedStage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = executed.runs[0]
    root = tmp_path / "arm-package"
    commands, paths = package._step_commands(executed, run, root)
    questions_path = Path(executed.plan["dataset"]["root"]) / "questions.jsonl"
    _artifact(questions_path)
    executed.plan["dataset"]["artifacts"]["questions"]["path"] = str(questions_path)
    monkeypatch.setattr(
        official_receipt.eval_gate,
        "evaluate_report",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        official_receipt.official,
        "load_longmemeval_v2_questions",
        lambda _path: [
            SimpleNamespace(id="web-id", domain="web"),
            SimpleNamespace(id="enterprise-id", domain="enterprise"),
        ],
    )
    monkeypatch.setattr(
        official_receipt.official,
        "summarize_dataset_counts",
        lambda **_kwargs: (451, 91),
    )
    artifact_paths = {
        "metric_overview": paths["operating_root"] / "metric_overview.json",
        "combined_metrics": paths["combined_metrics"],
        "submission_overview": paths["submission_root"]
        / package.SUBMISSION_NAME
        / "submission_overview.json",
        "submission_archive": paths["submission_root"] / f"{package.SUBMISSION_NAME}.tar.gz",
    }
    for output in artifact_paths.values():
        _artifact(output)
    question_ids = ["web-id", "enterprise-id"]
    receipt: dict[str, Any] = {
        "command": [
            "benchmarks/longmemeval_v2_official.py",
            *commands["official-receipt"][2:],
        ],
        "domain": "combined",
        "tier": package.TIER,
        "method": package.SUBMISSION_NAME,
        "sibyl_commit": executed.plan["source_identity"]["sha"],
        "runner_provenance": executed.plan["sibyl_provenance"],
        "official_repo": executed.plan["official_source"],
        "dataset": {
            "name": "longmemeval-v2",
            "data_root": executed.plan["dataset"]["root"],
            "tier": package.TIER,
            "questions_sha256": "sha256:" + "1" * 64,
            "trajectories_sha256": "sha256:" + "2" * 64,
            "haystack_sha256": "sha256:" + "3" * 64,
            "question_count": 451,
            "selected_question_ids_sha256": official_receipt.official.sha256_question_ids(
                question_ids
            ),
            "official_question_count": 451,
            "official_question_ids_sha256": official_receipt.official.sha256_question_ids(
                question_ids
            ),
            "selection_complete": True,
            "required_trajectory_count": 91,
        },
        "models": {
            "reader_model": "qwen/qwen3.5-9b",
            "reader_base_url": "https://openrouter.ai/api/v1",
            "evaluator_model": "gpt-5.2",
        },
        "source_runs": {
            "domains": {
                domain: {"output_dir": run["domains"][domain]["output_dir"]}
                for domain in ("web", "enterprise")
            }
        },
        "artifacts": {name: _official_artifact(output) for name, output in artifact_paths.items()},
    }
    receipt_path = paths["combined_receipt"]
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")

    assert (
        official_receipt.require_combined_receipt(
            executed,
            run,
            path=receipt_path,
            command=commands["official-receipt"],
            paths=paths,
        )
        == receipt
    )

    drifts: list[dict[str, Any]] = []
    wrong_dataset = copy.deepcopy(receipt)
    wrong_dataset["dataset"]["selected_question_ids_sha256"] = "sha256:" + "0" * 64
    drifts.append(wrong_dataset)
    wrong_model = copy.deepcopy(receipt)
    wrong_model["models"]["reader_model"] = "attacker/stale-reader"
    drifts.append(wrong_model)
    wrong_source = copy.deepcopy(receipt)
    wrong_source["source_runs"]["domains"]["web"]["output_dir"] = str(tmp_path / "stale")
    drifts.append(wrong_source)
    wrong_artifact = copy.deepcopy(receipt)
    wrong_artifact["artifacts"]["combined_metrics"]["sha256"] = "sha256:" + "0" * 64
    drifts.append(wrong_artifact)
    for drifted in drifts:
        receipt_path.write_text(json.dumps(drifted, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(StagePlanError):
            official_receipt.require_combined_receipt(
                executed,
                run,
                path=receipt_path,
                command=commands["official-receipt"],
                paths=paths,
            )


def test_package_command_logs_redact_provider_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "".join(("provider", "-secret-value"))

    class Process:
        stdout = iter([f"password={secret}\n"])

        @staticmethod
        def wait() -> int:
            return 0

    monkeypatch.setattr(command_runner.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    log = tmp_path / "command.jsonl"

    assert command_runner.invoke_command(["fake"], log_path=log, secrets=(secret,)) == 0
    assert secret not in log.read_text(encoding="utf-8")


def test_package_spawn_failure_writes_redacted_return_code_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "".join(("spawn", "-secret-value"))

    def fail_spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"credentials={secret}")

    monkeypatch.setattr(command_runner.subprocess, "Popen", fail_spawn)
    root = tmp_path / "package"
    root.mkdir()

    with pytest.raises(StagePlanError, match="exited -1"):
        process.execute_step(
            root=root,
            step="spawn",
            command=["missing"],
            collect=lambda: {},
            receipts={},
            secrets=(secret,),
            validate_inputs=lambda: None,
        )

    receipt = load_json(process.command_receipt_path(root, "spawn"))
    assert receipt["status"] == "FAIL"
    assert receipt["returncode"] == -1
    assert secret not in (root / "logs/spawn.jsonl").read_text(encoding="utf-8")
