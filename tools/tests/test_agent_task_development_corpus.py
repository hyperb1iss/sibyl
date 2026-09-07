"""Generated development tasks discriminate buggy, correct and partial fixes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from benchmarks.agent_tasks.manifest import (
    FIXED_ENVIRONMENT,
    Manifest,
    Task,
    digest,
    identity,
    read_artifact,
    runtime_identity,
)
from benchmarks.agent_tasks.runner import CheckerResult, _execute, run_task, snapshot

CORPUS = Path(__file__).resolve().parents[2] / "benchmarks" / "agent_tasks" / "development"
TASKS = [Task.model_validate(value) for value in json.loads((CORPUS / "tasks.json").read_bytes())]

# Reference edits are applied only to disposable validation snapshots. They are
# never task workspace artifacts, controller inputs or learning experiences.
REPAIRS = {
    "config-presence": [
        ("overrides.get(key) or value", "value if overrides.get(key) is None else overrides[key]")
    ],
    "patch-clear": [
        (
            "if value is not None:",
            "if value is None:\n            result.pop(key, None)\n        else:",
        )
    ],
    "cursor-zero": [("if not cursor:", "if cursor is None:")],
    "offset-filtered": [
        (
            'offset += len(page["items"])\n        if len(page["items"]) < page_size:',
            'offset += page_size\n        if offset >= page["total"]:',
        )
    ],
    "window-overlap": [
        ("left[0] <= right[1] and right[0] <= left[1]", "left[0] < right[1] and right[0] < left[1]")
    ],
    "date-entitlement": [("start <= day < end", "start <= day <= end")],
    "versioned-events": [
        (
            'latest[event["key"]] = event',
            'key = event["key"]\n        if key not in latest or event["version"] >= latest[key]["version"]:\n            latest[key] = event',
        )
    ],
    "ordered-deltas": [
        (
            '    latest = {event["key"]: event for event in events}\n    for event in latest.values():',
            "    for event in events:",
        )
    ],
}
PARTIAL_FIXES = {
    "config-presence": [
        (
            "value if overrides.get(key) is None else overrides[key]",
            "overrides[key] if isinstance(overrides.get(key), int) else overrides.get(key) or value",
        )
    ],
    "patch-clear": [
        (
            "else:\n            result[key] = value",
            "elif key in current:\n            result[key] = value",
        )
    ],
    "cursor-zero": [("if cursor is None:", 'if cursor is None or cursor == "":')],
    "offset-filtered": [("return items", "return list(dict.fromkeys(items))")],
    "window-overlap": [
        (
            "return left[0] < right[1] and right[0] < left[1]",
            "return left[0].replace(tzinfo=None) < right[1].replace(tzinfo=None) and right[0].replace(tzinfo=None) < left[1].replace(tzinfo=None)",
        )
    ],
    "date-entitlement": [
        ('    if start > end:\n        raise ValueError("reversed entitlement")\n', "")
    ],
    "versioned-events": [
        (
            'if key not in latest or event["version"] >= latest[key]["version"]:',
            "if key not in latest:",
        )
    ],
    "ordered-deltas": [
        ('result.get(key, 0) + event["value"]', 'result.get(key, 0) + max(0, event["value"])')
    ],
}


def artifact(root: Path, name: str, content: bytes) -> dict:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"path": name, "sha256": digest(content)}


def frozen_manifest(root: Path) -> Path:
    root.mkdir()
    for task in TASKS:
        for item in [
            task.prompt,
            task.checker.script,
            *(entry.artifact for entry in task.workspace),
        ]:
            artifact(root, item.path, read_artifact(CORPUS, item))
    controller = artifact(
        root,
        "noop.py",
        b'import json,sys\nr=json.load(sys.stdin)\njson.dump({"synthetic":True,"model":r["controller_model"],"tool_calls":0,"input_tokens":0,"output_tokens":0,"cost_usd":0.0},sys.stdout)\n',
    )
    lock = artifact(root, "uv.lock", (CORPUS.parents[2] / "uv.lock").read_bytes())
    memory = artifact(root, "empty-memory.txt", b"")
    manifest = Manifest.model_validate(
        {
            "schema_version": "sibyl-agent-task-manifest-v1",
            "experiment_id": "development-corpus-validation",
            "purpose": "trusted_development",
            "runtime_sha256": identity(runtime_identity()),
            "dependency_lock": lock,
            "seed": 0,
            "controller": {"script": controller},
            "controller_model": "synthetic-noop-validation",
            "controller_tools": [],
            "controller_budget": {
                "input_tokens": 0,
                "output_tokens": 0,
                "tool_calls": 0,
                "cost_usd": 0.0,
            },
            "controller_timeout_seconds": 5.0,
            "checker_timeout_seconds": 5.0,
            "experiences": [],
            "tasks": [task.model_dump() for task in TASKS],
            "arms": [{"id": "no-memory", "memory_pack": memory, "learning_source_ids": []}],
        }
    )
    path = root / "manifest.json"
    path.write_text(manifest.model_dump_json(indent=2))
    return path


def edit(workspace: Path, replacements: list[tuple[str, str]]) -> None:
    path = workspace / "app.py"
    source = path.read_text()
    for old, new in replacements:
        assert old in source, "reference edit no longer matches the fixture"
        source = source.replace(old, new)
    path.write_text(source)


def public_result(workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=workspace,
        env=FIXED_ENVIRONMENT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def independent_result(root: Path, workspace: Path, task: Task) -> CheckerResult:
    root.mkdir()
    process = _execute(
        program=CORPUS / task.checker.script.path,
        program_sha256=task.checker.script.sha256,
        args=task.checker.args,
        request={"attempt_id": root.name, "snapshot_sha256": identity(snapshot(workspace)[0])},
        workspace=workspace,
        output=root,
        role="checker",
        timeout=5,
    )
    assert process["returncode"] == 0
    assert not process["timed_out"]
    assert process["process_group_quiescent"]
    return CheckerResult.model_validate_json((root / "checker-stdout.txt").read_bytes())


def test_development_corpus_declares_eight_exposed_tasks_in_four_families():
    expected_tasks = 8
    assert len(TASKS) == expected_tasks
    assert set(Counter(task.family_id for task in TASKS).values()) == {2}
    assert {task.split for task in TASKS} == {"development"}
    for task in TASKS:
        assert task.id.startswith("dev-")
        assert task.family_id.startswith("dev-")
        assert task.checker.script.path not in {entry.artifact.path for entry in task.workspace}
        assert {entry.destination for entry in task.workspace} == {
            "app.py",
            "README.md",
            "tests/test_public.py",
        }
        for item in [
            task.prompt,
            task.checker.script,
            *(entry.artifact for entry in task.workspace),
        ]:
            assert read_artifact(CORPUS, item)


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_development_corpus_checks_initial_correct_and_partial_patches(tmp_path, task):
    manifest = frozen_manifest(tmp_path / "inputs")
    output = tmp_path / "initial-attempt"
    receipt = run_task(manifest, task_id=task.id, arm_id="no-memory", output=output)
    assert receipt["status"] == "task_failed", receipt
    assert receipt["checker"]["returncode"] == 0
    assert receipt["outcome"]["detail"].startswith("AssertionError:"), receipt["outcome"]
    assert receipt["sealed_isolation"] is False
    assert receipt["learning_benefit_established"] is False
    workspace = output / "controller-workspace"
    assert {item["path"] for item in snapshot(workspace)[0] if item["kind"] == "file"} == {
        "app.py",
        "README.md",
        "tests/test_public.py",
    }
    initial_public = public_result(workspace)
    assert initial_public.returncode == 1
    assert "FAIL:" in initial_public.stderr
    assert "ERROR:" not in initial_public.stderr

    correct = tmp_path / "reference-validation-workspace"
    shutil.copytree(workspace, correct)
    name = task.id.removeprefix("dev-")
    edit(correct, REPAIRS[name])
    result = independent_result(tmp_path / "correct-check", correct, task)
    assert result.passed, result.detail
    public = public_result(correct)
    assert public.returncode == 0, public.stderr

    partial = tmp_path / "partial-validation-workspace"
    shutil.copytree(correct, partial)
    edit(partial, PARTIAL_FIXES[name])
    public = public_result(partial)
    assert public.returncode == 0, public.stderr
    result = independent_result(tmp_path / "partial-check", partial, task)
    assert not result.passed
    assert result.detail.startswith("AssertionError:"), result.detail
