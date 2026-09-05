"""Actual process and snapshot checks for the trusted development task adapter."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from benchmarks.agent_tasks.manifest import (
    ManifestError,
    digest,
    identity,
    runtime_identity,
)
from benchmarks.agent_tasks.runner import _group_quiescent, run_task

CONTROLLER_FAILURE = 7

FIXTURES = Path(__file__).parent / "fixtures" / "agent_tasks"


@pytest.fixture
def experiment(tmp_path):
    root = tmp_path / "frozen"
    root.mkdir()

    def artifact(name, content):
        data = content.encode() if isinstance(content, str) else content
        (root / name).write_bytes(data)
        return {"path": name, "sha256": digest(data)}

    manifest = {
        "schema_version": "sibyl-agent-task-manifest-v1",
        "purpose": "trusted_development",
        "controller_model": "deterministic-fixture",
        "controller_tools": ["write_file"],
        "controller_budget": {
            "input_tokens": 10,
            "output_tokens": 10,
            "tool_calls": 2,
            "cost_usd": 0.0,
        },
        "experiment_id": "tiny-fixture",
        "runtime_sha256": identity(runtime_identity()),
        "seed": 3,
        "dependency_lock": artifact("lock.json", "{}"),
        "controller": {
            "script": artifact("controller.py", (FIXTURES / "controller.py").read_bytes())
        },
        "controller_timeout_seconds": 3.0,
        "checker_timeout_seconds": 3.0,
        "experiences": [
            {
                "id": "source-one",
                "family_id": "source-family",
                "split": "learning",
                "revision": "fixture-v1",
                "artifact": artifact("experience.txt", "Observed evidence, not task answers."),
            }
        ],
        "tasks": [
            {
                "id": "task-one",
                "family_id": "task-family",
                "split": "development",
                "prompt": artifact("prompt.txt", "Use the supplied memory to update answer.txt."),
                "workspace": [
                    {"artifact": artifact("baseline.txt", "0"), "destination": "answer.txt"}
                ],
                "checker": {
                    "script": artifact("checker.py", (FIXTURES / "checker.py").read_bytes())
                },
            }
        ],
        "arms": [
            {
                "id": "memory",
                "memory_pack": artifact("memory.txt", "42"),
                "learning_source_ids": ["source-one"],
            }
        ],
    }
    path = root / "manifest.json"

    def freeze():
        path.write_text(json.dumps(manifest))
        return path

    freeze()
    return manifest, freeze, tmp_path / "attempt"


def execute(experiment):
    _, freeze, output = experiment
    return run_task(freeze(), task_id="task-one", arm_id="memory", output=output)


def test_controller_final_state_is_independently_checked(experiment, monkeypatch):
    monkeypatch.setenv("SIBYL_TEST_SECRET", "must-not-reach-child")
    receipt = execute(experiment)
    _, freeze, output = experiment
    assert receipt["success"]
    assert receipt["status"] == "passed"
    assert receipt["sealed_isolation"] is False
    assert receipt["learning_benefit_established"] is False
    assert (freeze().parent / "baseline.txt").read_text() == "0"
    assert (output / "checker-workspace/answer.txt").read_text() == "42"
    initial = json.loads((output / "initial-snapshot.json").read_text())
    final = json.loads((output / "final-snapshot.json").read_text())
    assert initial != final
    assert (
        receipt["checker_input_snapshot_sha256"]
        == receipt["controller_final_snapshot_sha256"]
        == identity(final)
    )
    assert receipt["usage"]["cost_usd"] == 0
    assert receipt["usage"]["synthetic"] is True
    assert receipt["usage"]["provenance"] == "controller_reported"
    request = json.loads((output / "controller-request.json").read_text())
    assert request["memory_pack"] == "42"
    assert request["pack_id"] == receipt["pack_id"]
    assert request["attempt_id"] == receipt["attempt_id"]
    assert "checker" not in request
    saved = json.loads((output / "receipt.json").read_text())
    checksum = saved.pop("receipt_sha256")
    assert checksum == identity(saved)


@pytest.mark.parametrize(
    ("role", "mode", "status"),
    [
        ("controller", "wrong", "task_failed"),
        ("controller", "fail", "controller_failed"),
        ("controller", "timeout", "controller_timeout"),
        ("controller", "malformed", "controller_protocol_invalid"),
        ("controller", "badusage", "controller_protocol_invalid"),
        ("controller", "symlink", "unsafe_snapshot"),
        ("checker", "fail", "checker_failed"),
        ("checker", "timeout", "checker_timeout"),
        ("checker", "malformed", "checker_protocol_invalid"),
    ],
)
def test_failed_processes_keep_partial_receipts(experiment, role, mode, status):
    manifest, _, output = experiment
    program = manifest["controller"] if role == "controller" else manifest["tasks"][0]["checker"]
    program["args"] = [mode]
    if mode == "timeout":
        manifest[f"{role}_timeout_seconds"] = 0.1
    receipt = execute(experiment)
    assert receipt["status"] == status
    assert not receipt["success"]
    assert (output / "receipt.json").exists()
    assert (output / f"{role}-stdout.txt").exists()
    assert (output / f"{role}-stderr.txt").exists()
    if role == "controller" and mode == "fail":
        assert receipt["controller"]["returncode"] == CONTROLLER_FAILURE
        assert "checker" not in receipt


def test_missing_usage_is_not_silently_zero(experiment):
    experiment[0]["controller"]["args"] = ["unknownusage"]
    receipt = execute(experiment)
    assert receipt["success"]
    assert receipt["usage"]["cost_usd"] is None
    assert receipt["usage"]["input_tokens"] is None
    assert receipt["usage"]["complete"] is False
    assert receipt["budget_status"] == "unknown"


def test_timeout_terminates_child_before_snapshot(experiment):
    manifest, _, output = experiment
    manifest["controller"]["args"] = ["child"]
    manifest["controller_timeout_seconds"] = 0.2
    receipt = execute(experiment)
    assert receipt["status"] == "controller_timeout"
    time.sleep(1.0)
    assert not (output / "controller-workspace/late-child.txt").exists()
    assert not (output / "checker-workspace/late-child.txt").exists()


@pytest.mark.parametrize(
    "filename",
    ["baseline.txt", "controller.py", "checker.py", "memory.txt", "prompt.txt", "lock.json"],
)
def test_changed_frozen_inputs_fail_before_output(experiment, filename):
    _, freeze, output = experiment
    path = freeze()
    (path.parent / filename).write_text("changed")
    with pytest.raises(ManifestError, match="changed input"):
        execute(experiment)
    assert not output.exists()


@pytest.mark.parametrize(
    ("violation", "message"),
    [
        ("unknown", "Extra inputs are not permitted"),
        ("family", "family or exact content overlaps"),
        ("source", "only declared learning sources"),
        ("sealed", "development-only"),
        ("collision", "duplicate workspace destination"),
        ("path", "canonical relative file path"),
        ("runtime", "runtime identity differs"),
    ],
)
def test_manifest_rejects_invalid_boundaries(experiment, violation, message):
    manifest, _, output = experiment
    if violation == "unknown":
        manifest["unexpected"] = True
    elif violation == "family":
        manifest["tasks"][0]["family_id"] = "source-family"
    elif violation == "source":
        manifest["arms"][0]["learning_source_ids"] = ["undeclared"]
    elif violation == "sealed":
        manifest["tasks"][0]["split"] = "sealed"
    elif violation == "collision":
        manifest["tasks"][0]["workspace"] *= 2
    elif violation == "path":
        manifest["tasks"][0]["workspace"][0]["destination"] = "../escaped"
    else:
        manifest["runtime_sha256"] = "0" * 64
    with pytest.raises(ValueError, match=message):
        execute(experiment)
    assert not output.exists()


def test_input_symlink_is_rejected(experiment):
    _, freeze, output = experiment
    path = freeze()
    target = path.parent / "memory.txt"
    target.unlink()
    target.symlink_to("baseline.txt")
    with pytest.raises(ManifestError, match="symlink"):
        execute(experiment)
    assert not output.exists()


def test_output_collision_does_not_overwrite_receipt(experiment):
    execute(experiment)
    output = experiment[2]
    before = (output / "receipt.json").read_bytes()
    with pytest.raises(FileExistsError):
        execute(experiment)
    assert (output / "receipt.json").read_bytes() == before


def test_cli_runs_the_actual_protocol(experiment):
    _, freeze, output = experiment
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "benchmarks.agent_tasks",
            "--manifest",
            str(freeze()),
            "--task",
            "task-one",
            "--arm",
            "memory",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "passed"


@pytest.mark.parametrize("limit", ["tool_calls", "input_tokens", "output_tokens", "cost_usd"])
def test_reported_budget_excess_is_a_failure(experiment, limit):
    manifest, _, output = experiment
    manifest["controller_budget"][limit] = 0.0 if limit == "cost_usd" else 0
    script = manifest["controller"]["script"]
    path = experiment[1]().parent / script["path"]
    content = path.read_text().replace('"' + limit + '": 0', '"' + limit + '": 1')
    path.write_text(content)
    script["sha256"] = digest(path.read_bytes())
    receipt = execute(experiment)
    assert receipt["status"] == "controller_budget_exceeded"
    assert receipt["budget_status"] == "exceeded"
    assert not receipt["success"]
    assert receipt["outcome"]["passed"] is True
    assert (output / "checker-stdout.txt").exists()


def test_live_group_prevents_snapshot_publication(experiment, monkeypatch):
    monkeypatch.setattr("benchmarks.agent_tasks.runner._group_quiescent", lambda group_id: False)
    receipt = execute(experiment)
    assert receipt["status"] == "controller_cleanup_failed"
    assert "controller_final_snapshot_sha256" not in receipt
    assert not (experiment[2] / "checker-workspace").exists()


def test_successful_parent_cannot_leave_mutating_children(experiment):
    experiment[0]["controller"]["args"] = ["orphan"]
    receipt = execute(experiment)
    assert receipt["success"]
    assert receipt["controller"]["process_group_quiescent"] is True
    time.sleep(1.0)
    assert not (experiment[2] / "controller-workspace/late-child.txt").exists()


def test_empty_directories_and_modes_reach_the_checker(experiment):
    experiment[0]["controller"]["args"] = ["directories"]
    receipt = execute(experiment)
    assert receipt["success"]
    checked = experiment[2] / "checker-workspace"
    assert (checked / "empty/subdir").is_dir()
    assert checked.stat().st_mode == (experiment[2] / "controller-workspace").stat().st_mode
    assert (checked / "empty").stat().st_mode == (
        experiment[2] / "controller-workspace/empty"
    ).stat().st_mode


def test_duplicate_json_keys_are_rejected(experiment):
    path = experiment[1]()
    path.write_text(path.read_text().replace('"seed": 3', '"seed": 3, "seed": 4'))
    with pytest.raises(ManifestError, match="duplicate JSON key"):
        run_task(path, task_id="task-one", arm_id="memory", output=experiment[2])


def test_exact_content_cannot_cross_splits(experiment):
    manifest, _, _ = experiment
    manifest["experiences"][0]["artifact"] = manifest["tasks"][0]["prompt"]
    with pytest.raises(ManifestError, match="exact content overlaps"):
        execute(experiment)


def test_output_parent_alias_cannot_write_into_frozen_inputs(experiment):
    path = experiment[1]()
    alias = experiment[2].parent / "input-alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(ManifestError, match="outside the frozen input directory"):
        run_task(path, task_id="task-one", arm_id="memory", output=alias / "attempt")
    assert not (path.parent / "attempt").exists()


@pytest.mark.parametrize("role", ["controller", "checker"])
def test_duplicate_protocol_fields_cannot_publish_success(experiment, role):
    manifest, _, output = experiment
    program = manifest["controller"] if role == "controller" else manifest["tasks"][0]["checker"]
    program["args"] = ["duplicate"]
    receipt = execute(experiment)
    assert receipt["status"] == f"{role}_protocol_invalid"
    assert "duplicate JSON key" in receipt["error"]
    assert receipt["success"] is False
    assert (output / f"{role}-stdout.txt").read_text()
    if role == "controller":
        assert receipt["usage"]["cost_usd"] is None
        assert "checker" not in receipt


def test_unreadable_subtree_cannot_publish_lossy_success(experiment):
    manifest, _, output = experiment
    manifest["controller"]["args"] = ["unreadable"]
    manifest["tasks"][0]["checker"]["args"] = ["deleted-private"]
    receipt = execute(experiment)
    private = output / "controller-workspace/private"
    try:
        assert receipt["success"] is False
        if os.geteuid() != 0:
            assert receipt["status"] == "unsafe_snapshot"
            assert "cannot read complete task snapshot" in receipt["error"]
            assert not (output / "checker-stdout.txt").exists()
        else:
            # Root can read mode000, so its faithful copy must fail the oracle.
            assert receipt["status"] == "task_failed"
    finally:
        private.chmod(0o700)
        checked = output / "checker-workspace/private"
        if checked.exists():
            checked.chmod(0o700)
    assert (private / "retained.txt").read_text() == "must not disappear during snapshot"


@pytest.mark.parametrize("artifact_kind", ["sealed_prompt", "checker", "workspace", "experience"])
def test_pack_cannot_copy_known_nonlearning_bytes(experiment, artifact_kind):
    manifest, freeze, output = experiment
    task = manifest["tasks"][0]
    if artifact_kind == "sealed_prompt":
        artifact = manifest["arms"][0]["memory_pack"]
        manifest["tasks"].append(
            {
                **task,
                "id": "held-out",
                "family_id": "held-out-family",
                "split": "sealed",
                "prompt": artifact,
            }
        )
    elif artifact_kind == "checker":
        artifact = task["checker"]["script"]
    elif artifact_kind == "workspace":
        artifact = task["workspace"][0]["artifact"]
    else:
        artifact = {"path": "held-out.txt", "sha256": digest(b"held-out evidence")}
        (freeze().parent / artifact["path"]).write_bytes(b"held-out evidence")
        manifest["experiences"].append(
            {
                "id": "held-out",
                "family_id": "held-out-family",
                "split": "sealed",
                "revision": "v1",
                "artifact": artifact,
            }
        )
    manifest["arms"][0]["memory_pack"] = artifact
    with pytest.raises(ManifestError, match="memory pack overlaps a declared nonlearning artifact"):
        execute(experiment)
    assert not output.exists()


def test_live_process_group_blocks_until_killed():
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        assert _group_quiescent(process.pid, timeout=0.1) is False
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    assert _group_quiescent(process.pid, timeout=1.0) is True


def test_staged_checker_tamper_is_rejected_before_execution(experiment):
    experiment[0]["controller"]["args"] = ["tamper-checker"]
    receipt = execute(experiment)
    assert receipt["status"] == "runner_error"
    assert "staged program differs from the frozen manifest" in receipt["error"]
    assert receipt["success"] is False
    assert not (experiment[2] / "checker-stdout.txt").exists()


def test_declared_nonlearning_source_reference_is_rejected(experiment):
    experiment[0]["experiences"][0]["split"] = "development"
    with pytest.raises(ManifestError, match="only declared learning sources"):
        execute(experiment)
    assert not experiment[2].exists()


def test_nested_workspace_file_collision_is_rejected(experiment):
    workspace = experiment[0]["tasks"][0]["workspace"]
    workspace[0]["destination"] = "parent"
    workspace.append({**workspace[0], "destination": "parent/child"})
    with pytest.raises(ManifestError, match="workspace file/directory collision"):
        execute(experiment)
    assert not experiment[2].exists()


@pytest.mark.parametrize("kind", ["prompt", "memory_pack"])
def test_non_utf8_text_is_rejected_before_output(experiment, kind):
    manifest, freeze, output = experiment
    artifact = manifest["tasks"][0][kind] if kind == "prompt" else manifest["arms"][0][kind]
    (freeze().parent / artifact["path"]).write_bytes(bytes([255]))
    artifact["sha256"] = digest(bytes([255]))
    with pytest.raises(ManifestError, match="prompt and memory pack inputs require UTF-8"):
        execute(experiment)
    assert not output.exists()


def test_output_parent_alias_outside_inputs_is_supported(experiment):
    _, freeze, output = experiment
    alias = output.parent / "output-alias"
    alias.symlink_to(output.parent, target_is_directory=True)
    receipt = run_task(freeze(), task_id="task-one", arm_id="memory", output=alias / output.name)
    assert receipt["success"] is True
    assert (output / "receipt.json").exists()


def test_empty_control_and_memory_arm_share_frozen_comparison(experiment):
    manifest, freeze, output = experiment
    root = freeze().parent
    (root / "empty.txt").write_bytes(b"")
    empty = {"path": "empty.txt", "sha256": digest(b"")}
    # Empty baseline files cannot disqualify an empty no-memory control.
    manifest["tasks"][0]["workspace"].append({"artifact": empty, "destination": "empty.txt"})
    manifest["arms"].append({"id": "control", "memory_pack": empty, "learning_source_ids": []})
    path = freeze()
    memory = run_task(path, task_id="task-one", arm_id="memory", output=output)
    control = run_task(
        path, task_id="task-one", arm_id="control", output=output.with_name("control")
    )
    assert memory["status"] == "passed"
    assert control["status"] == "task_failed"
    assert control["success"] is False
    for key in ("experiment_id", "manifest_sha256", "task_sha256", "controller_budget", "seed"):
        assert memory[key] == control[key]
    for key in ("arm_id", "pack_id", "attempt_id", "request_id"):
        assert memory[key] != control[key]
    assert control["pack_id"] == digest(b"")
    assert (output.with_name("control") / "controller-request.json").exists()
