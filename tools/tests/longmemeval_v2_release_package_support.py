"""Shared fixtures for release package and authority tests."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_official_package as package
from benchmarks import longmemeval_v2_release_official_receipt as official_receipt
from benchmarks import longmemeval_v2_release_package_archive as package_archive
from benchmarks import longmemeval_v2_release_package_object as package_object
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_handoff import ExecutedDomain, ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import bind_artifact, load_json
from tools.bench import longmemeval_v2_rig as rig

COMMAND_COUNT = 4


def thaw_tree(root: Path) -> None:
    for current, _directories, files in os.walk(root):
        current_path = Path(current)
        with suppress(OSError):
            os.chflags(current_path, 0)
        with suppress(OSError):
            current_path.chmod(0o700)
        for name in files:
            path = current_path / name
            with suppress(OSError):
                os.chflags(path, 0)
            with suppress(OSError):
                path.chmod(0o600)


def artifact(path: Path, text: str = "sealed\n") -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return bind_artifact(path, name="test artifact")


def official_artifact(path: Path) -> dict[str, Any]:
    return {**bind_artifact(path, name="official test artifact"), "exists": True}


def build_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExecutedStage:
    paid_root = tmp_path / "paid"
    paid_root.mkdir()
    official = tmp_path / "official"
    official.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    system_description = artifact(tmp_path / "SYSTEM_DESCRIPTION.md")
    adapter = artifact(tmp_path / "sibyl_memory.py")
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
        artifact(Path(run["domains"][domain]["output_dir"]) / "aggregated_metrics.json")
    stack = {
        "sibyl_commit": "a" * 40,
        "sibyl_git_status": "clean",
        "official_source": {"commit": "b" * 40},
        "dataset_sha256_by_domain": {"web": "c" * 64, "enterprise": "d" * 64},
        "reader": {
            "model": "qwen/qwen3.5-9b",
            "base_url": "https://openrouter.ai/api/v1",
        },
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
    control = artifact(paid_root / "runner_claim.json")
    status_control = artifact(paid_root / "runner_status.json")
    status_receipt = {"status": "EXECUTED"}
    domains = tuple(
        ExecutedDomain(
            arm_id=run["arm_id"],
            domain=domain,
            actual_cost_usd=0.5,
            exit_artifact=artifact(paid_root / "exits" / run["arm_id"] / f"{domain}.json"),
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


def owned_command_path(value: str, *, log_path: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else log_path.parents[1] / path


def successful_invoke(
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
        root = owned_command_path(
            command[command.index("--output-root") + 1],
            log_path=log_path,
        )
        operating = root / "sibyl_live_api/operating_points/official"
        for relative in package.OPERATING_POINT_FILES:
            artifact(operating / relative)
    elif "step_2_build_package" in joined:
        root = owned_command_path(
            command[command.index("--output-root") + 1],
            log_path=log_path,
        )
        artifact(root / "sibyl_live_api/SYSTEM_DESCRIPTION.md")
        artifact(root / "sibyl_live_api" / Path(command[4]).name)
        artifact(root / "sibyl_live_api/submission_overview.json")
        artifact(root / "sibyl_live_api.tar.gz")
    elif "combine_aggregated_metrics" in joined:
        artifact(owned_command_path(command[command.index("-o") + 1], log_path=log_path))
    else:
        artifact(
            owned_command_path(
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


def stub_score_aware_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
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


def published_members(root: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    publication = load_json(package_object.publication_path(root, "aa-1-left"))
    content = Path(publication["package_object"]["path"]).read_bytes()
    members, _manifest = package_archive.require_package_object(content)
    return publication, members
