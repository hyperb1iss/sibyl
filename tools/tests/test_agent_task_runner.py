"""Actual process and snapshot checks for the trusted development task adapter."""

from __future__ import annotations

import base64
import json
import os
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import asdict
from pathlib import Path

import pytest
from benchmarks.agent_tasks import coding_controller, json_oracle, runner
from benchmarks.agent_tasks import coding_controller as runtime
from benchmarks.agent_tasks.coding_controller import inventory
from benchmarks.agent_tasks.json_oracle import evaluate_json_oracle
from benchmarks.agent_tasks.manifest import (
    Arm,
    JsonOracleChecker,
    Manifest,
    ManifestError,
    digest,
    identity,
    load_manifest,
    runtime_identity,
    validate_native_render_binding,
)
from benchmarks.agent_tasks.runner import _group_quiescent, run_task

from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextPack,
    ContextSection,
)
from sibyl_core.tools.context_rendering import render_context_pack

CONTROLLER_FAILURE = 7
REPORTED_INPUT_TOKENS = 2

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
    assert receipt["input_retention"] == (
        "selected_task_and_arm_only_not_a_complete_experiment_archive"
    )
    assert {path.name for path in (output / "inputs").iterdir()} == {
        "lock.json",
        "controller.py",
        "prompt.txt",
        "baseline.txt",
        "checker.py",
        "memory.txt",
    }
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
    assert receipt["status"] == "controller_timeout", json.dumps(receipt, indent=2, sort_keys=True)
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
        ("sealed", "supports only learning and development tasks"),
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
    controller = root / "controller.py"
    controller.write_text(
        controller.read_text().replace(
            "request = json.load(sys.stdin)",
            'request = json.load(sys.stdin)\nif not request["memory_pack"]:\n'
            '    assert not Path("../inputs/memory.txt").exists()',
        )
    )
    manifest["controller"]["script"]["sha256"] = digest(controller.read_bytes())
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
    assert not (output.with_name("control") / "inputs/memory.txt").exists()


def native_payload(source_revision=7, *, schema_version="sibyl-context-render-v2", procedure=False):
    """Small Unicode rendering with actual core receipt generation."""
    item = ContextItem(
        id="source-record",
        type="procedure" if procedure else "note",
        name="Café 💜",
        content=(
            "## Preconditions\n```python\nif ready:\n    apply()\n```\n"
            + "Step with check 💜\n" * 90
            + "## Abstain when\nUnsupported version.\n"
        )
        if procedure
        else "A verified observation. " * 30,
        score=1.0,
        facet=ContextFacet.DECISIONS,
        reason="source",
        source_revision=source_revision,
    )
    pack = ContextPack(
        goal="Use frozen evidence",
        intent=ContextIntent.GENERAL,
        query="observation",
        domain=None,
        project=None,
        sections=[ContextSection(ContextFacet.DECISIONS, "Decisions", [item])],
        total_items=1,
    )
    rendered = render_context_pack(
        pack,
        max_content_chars=80,
        schema_version=schema_version,
        token_budget=4000 if procedure else None,
    )
    return {
        **asdict(pack),
        "markdown": rendered.markdown,
        "render_receipt": asdict(rendered.receipt),
    }


def bind_native_payload(experiment, payload, *, memory=None):
    """Freeze a sidecar and a hash-checking fixture oracle outside the workspace."""
    manifest, freeze, _ = experiment
    root = freeze().parent
    packed = payload["markdown"].encode() if memory is None else memory
    data = json.dumps(payload, default=str).encode()
    (root / "native.json").write_bytes(data)
    (root / "memory.txt").write_bytes(packed)
    arm = manifest["arms"][0]
    arm["memory_pack"]["sha256"] = digest(packed)
    arm["native_render_payload"] = {"path": "native.json", "sha256": digest(data)}
    checker = (
        "import hashlib,json,sys\nfrom pathlib import Path\njson.load(sys.stdin)\n"
        f"passed=hashlib.sha256(Path('answer.txt').read_bytes()).hexdigest()=={digest(packed)!r}\n"
        "print(json.dumps({'passed':passed,'detail':'exact supplied bytes'}))\n"
    ).encode()
    (root / "checker.py").write_bytes(checker)
    manifest["tasks"][0]["checker"]["script"]["sha256"] = digest(checker)
    return arm


@pytest.mark.parametrize("split", ["learning", "development"])
@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("procedure", [False, True])
def test_native_binding_automatically_retains_input_outside_controller_workspace(
    experiment, split, version, procedure
):
    experiment[0]["tasks"][0]["split"] = split
    payload = native_payload(schema_version=f"sibyl-context-render-{version}", procedure=procedure)
    arm = bind_native_payload(experiment, payload)
    receipt = execute(experiment)
    output = experiment[2]
    assert receipt["success"] is True
    request = json.loads((output / "controller-request.json").read_text())
    assert request["memory_pack"].encode() == payload["markdown"].encode()
    assert (
        receipt["pack_id"] == request["memory_pack_sha256"] == digest(payload["markdown"].encode())
    )
    assert receipt["memory_provenance"] == {
        "status": f"native_render_{version}",
        "native_payload_sha256": arm["native_render_payload"]["sha256"],
        "render_schema_version": f"sibyl-context-render-{version}",
    }
    assert receipt["memory_provenance"]["native_payload_sha256"] != request["memory_pack_sha256"]
    assert "native_render_payload" not in request
    assert not (output / "controller-workspace/native.json").exists()
    retained = {
        name: (output / "inputs" / name).read_bytes() for name in ("native.json", "memory.txt")
    }
    validate_native_render_binding(Arm.model_validate(arm), retained)
    retained["native.json"] += b"\n"
    with pytest.raises(ManifestError, match="changed artifact bytes"):
        validate_native_render_binding(Arm.model_validate(arm), retained)


@pytest.mark.parametrize("missing", ["native.json", "memory.txt"])
def test_native_binding_replay_rejects_missing_retained_artifact(experiment, missing):
    arm = bind_native_payload(experiment, native_payload())
    root = experiment[1]().parent
    retained = {
        name: (root / name).read_bytes()
        for name in ("native.json", "memory.txt")
        if name != missing
    }
    with pytest.raises(ManifestError, match="missing a retained artifact"):
        validate_native_render_binding(Arm.model_validate(arm), retained)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "null",
        "version",
        "receipt_only",
        "list",
        "string",
        "number",
        "no_markdown",
        "item",
        "revision",
        "span",
        "input_hash",
        "disposition",
        "options",
    ],
)
def test_native_provenance_tamper_fails_before_process_or_output(experiment, monkeypatch, mutation):
    payload = native_payload()
    arm = bind_native_payload(experiment, payload)
    if mutation == "missing":
        del payload["render_receipt"]
    elif mutation == "null":
        payload["render_receipt"] = None
    elif mutation == "version":
        payload["render_receipt"]["schema_version"] = "sibyl-context-render-v9"
    elif mutation == "receipt_only":
        payload = {"render_receipt": payload["render_receipt"]}
    elif mutation in {"list", "string", "number"}:
        payload = {"list": [], "string": "x", "number": 5}[mutation]
    elif mutation == "no_markdown":
        del payload["markdown"]
    elif mutation == "item":
        payload["sections"][0]["items"][0]["content"] = "Different source"
    elif mutation == "revision":
        payload["sections"][0]["items"][0]["source_revision"] = 9
    elif mutation == "span":
        payload["render_receipt"]["spans"][0]["start_byte"] += 1
    elif mutation == "input_hash":
        payload["render_receipt"]["spans"][0]["input_sha256"] = "0" * 64
    elif mutation == "disposition":
        payload["render_receipt"]["dispositions"] = []
    elif mutation == "options":
        payload["render_receipt"]["options"]["max_content_chars"] = 1000
    data = json.dumps(payload, default=str).encode()
    (experiment[1]().parent / "native.json").write_bytes(data)
    arm["native_render_payload"]["sha256"] = digest(data)

    def forbidden_process(*args, **kwargs):
        pytest.fail("preflight launched a controller")

    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    with pytest.raises(ManifestError, match="native render"):
        execute(experiment)
    assert not experiment[2].exists()


@pytest.mark.parametrize("change", ["newline", "crlf", "space", "normalization", "empty"])
def test_native_memory_join_is_exact_bytes(experiment, change):
    payload = native_payload()
    text = payload["markdown"]
    changed = {
        "newline": text + "\n",
        "crlf": text.replace("\n", "\r\n"),
        "space": text + " ",
        "normalization": unicodedata.normalize("NFD", text),
        "empty": "",
    }[change]
    assert changed != text
    bind_native_payload(experiment, payload, memory=changed.encode())
    with pytest.raises(ManifestError, match="differs from the exact memory pack"):
        execute(experiment)
    assert not experiment[2].exists()


@pytest.mark.parametrize("explicit_null", [False, True])
def test_legacy_manifest_and_arm_identity_remain_unchanged(experiment, explicit_null):
    manifest, _, output = experiment
    if explicit_null:
        manifest["arms"][0]["native_render_payload"] = None
    fixed = {**manifest, "runtime_sha256": "0" * 64}
    parsed = Manifest.model_validate(fixed)
    assert (
        identity(parsed.model_dump(mode="json"))
        == "0594a4ab474830ceeb267a2252127dd409b1bbd6248ac8d4a79062068b7cecea"
    )
    assert (
        identity(parsed.arms[0].model_dump(mode="json"))
        == "3b766c6d9ecb19c85a506e3defa112909c774d19e3b429300040674c9803754f"
    )
    receipt = execute(experiment)
    assert receipt["memory_provenance"]["status"] == "unattributed"
    assert (
        "native_render_payload" not in json.loads((output / "manifest.json").read_text())["arms"][0]
    )


def test_empty_control_does_not_claim_native_provenance(experiment):
    manifest, freeze, _ = experiment
    (freeze().parent / "memory.txt").write_bytes(b"")
    manifest["arms"][0]["memory_pack"]["sha256"] = digest(b"")
    manifest["arms"][0]["learning_source_ids"] = []
    receipt = execute(experiment)
    assert receipt["memory_provenance"] == {
        "status": "none",
        "native_payload_sha256": None,
        "render_schema_version": None,
    }


@pytest.mark.parametrize("claimed", [False, True])
def test_manifest_preflight_imports_core_only_for_claims(experiment, claimed):
    if claimed:
        bind_native_payload(experiment, native_payload())
    script = (
        "import importlib.abc,sys\n"
        "class RejectCore(importlib.abc.MetaPathFinder):\n"
        " def find_spec(self,fullname,path=None,target=None):\n"
        "  if fullname=='sibyl_core' or fullname.startswith('sibyl_core.'):\n"
        "   raise RuntimeError('unexpected core import')\n"
        "sys.meta_path.insert(0,RejectCore())\n"
        "from pathlib import Path\n"
        "from benchmarks.agent_tasks.manifest import load_manifest\n"
        "load_manifest(Path(sys.argv[1]))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(experiment[1]())],
        capture_output=True,
        text=True,
        check=False,
    )
    if claimed:
        assert result.returncode != 0
        assert "unexpected core import" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


def test_native_claim_changes_arm_and_manifest_identity(experiment):
    before = Manifest.model_validate(experiment[0])
    bind_native_payload(experiment, native_payload())
    after = Manifest.model_validate(experiment[0])
    assert identity(before.model_dump(mode="json")) != identity(after.model_dump(mode="json"))
    assert identity(before.arms[0].model_dump(mode="json")) != identity(
        after.arms[0].model_dump(mode="json")
    )
    assert after.arms[0].model_dump(mode="json")["native_render_payload"] is not None


def test_native_binding_keeps_unavailable_source_revisions(experiment):
    payload = native_payload(source_revision=None)
    bind_native_payload(experiment, payload)
    receipt = execute(experiment)
    assert receipt["success"] is True
    retained = json.loads((experiment[2] / "inputs/native.json").read_text())
    assert all(span["source_revision"] is None for span in retained["render_receipt"]["spans"])
    assert all(
        span["revision_status"] == "unavailable" for span in retained["render_receipt"]["spans"]
    )


@pytest.mark.parametrize("mutation", ["duplicate", "nan", "file_hash"])
def test_native_sidecar_bytes_fail_preflight(experiment, monkeypatch, mutation):
    payload = native_payload()
    arm = bind_native_payload(experiment, payload)
    data = json.dumps(payload, default=str).encode()
    if mutation == "duplicate":
        data = data[:-1] + b',"markdown":"duplicate"}'
    elif mutation == "nan":
        data = data[:-1] + b',"extra":NaN}'
    (experiment[1]().parent / "native.json").write_bytes(data)
    arm["native_render_payload"]["sha256"] = "0" * 64 if mutation == "file_hash" else digest(data)

    def forbidden_process(*args, **kwargs):
        pytest.fail("invalid sidecar launched a controller")

    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    with pytest.raises(
        ManifestError, match=r"duplicate JSON key|non-JSON numeric constant|changed input"
    ):
        execute(experiment)
    assert not experiment[2].exists()


@pytest.mark.parametrize("split", ["learning", "development"])
@pytest.mark.parametrize("mode", [None, "wrong", "fail", "malformed", "badusage"])
def test_task_collection_preserves_checked_outcomes_and_split(experiment, split, mode):
    manifest, _, output = experiment
    manifest["tasks"][0]["split"] = split
    if mode:
        manifest["controller"]["args"] = [mode]
    receipt = execute(experiment)
    expected = {
        None: "passed",
        "wrong": "task_failed",
        "fail": "controller_failed",
        "malformed": "controller_protocol_invalid",
        "badusage": "controller_protocol_invalid",
    }
    assert receipt["status"] == expected[mode]
    assert receipt["success"] is (mode is None)
    assert receipt["task_split"] == split
    assert receipt["task_family_id"] == "task-family"
    retained = Manifest.model_validate_json((output / "manifest.json").read_bytes())
    task = retained.tasks[0]
    assert receipt["task_sha256"] == identity(task.model_dump(mode="json"))
    assert receipt["manifest_sha256"] == identity(retained.model_dump(mode="json"))
    assert task.split == receipt["task_split"]
    assert task.family_id == receipt["task_family_id"]
    assert receipt["learning_benefit_established"] is False
    assert receipt["sealed_isolation"] is False
    saved = json.loads((output / "receipt.json").read_bytes())
    checksum = saved.pop("receipt_sha256")
    assert checksum == identity(saved)
    if mode in (None, "wrong"):
        assert receipt["outcome"]["passed"] is (mode is None)
        assert (
            receipt["controller_final_snapshot_sha256"] == receipt["checker_input_snapshot_sha256"]
        )
    else:
        assert "outcome" not in receipt


@pytest.mark.parametrize("heldout_split", ["development", "sealed"])
@pytest.mark.parametrize("overlap", ["family", "prompt"])
def test_learning_tasks_cannot_overlap_later_splits(experiment, heldout_split, overlap):
    manifest, freeze, output = experiment
    learning = manifest["tasks"][0]
    learning["split"] = "learning"
    prompt = {"path": "later-task.txt", "sha256": digest(b"A different task")}
    (freeze().parent / prompt["path"]).write_bytes(b"A different task")
    heldout = {
        **learning,
        "id": "later-task",
        "family_id": "different-family",
        "split": heldout_split,
        "prompt": prompt,
    }
    heldout["family_id" if overlap == "family" else "prompt"] = learning[
        "family_id" if overlap == "family" else "prompt"
    ]
    manifest["tasks"].append(heldout)
    with pytest.raises(ManifestError, match="family or exact content overlaps"):
        execute(experiment)
    assert not output.exists()


def test_learning_task_can_coexist_with_separate_heldout_task(experiment):
    manifest, freeze, output = experiment
    learning = manifest["tasks"][0]
    learning["split"] = "learning"
    prompt = {"path": "sealed-task.txt", "sha256": digest(b"An independent sealed task")}
    (freeze().parent / prompt["path"]).write_bytes(b"An independent sealed task")
    manifest["tasks"].append(
        {
            **learning,
            "id": "sealed-task",
            "family_id": "sealed-family",
            "split": "sealed",
            "prompt": prompt,
        }
    )
    controller = freeze().parent / "controller.py"
    controller.write_bytes(
        b"from pathlib import Path\nassert not Path('../inputs/sealed-task.txt').exists()\n"
        + controller.read_bytes()
    )
    manifest["controller"]["script"]["sha256"] = digest(controller.read_bytes())
    receipt = execute(experiment)
    assert receipt["success"] is True
    assert receipt["task_split"] == "learning"
    assert not (output / "inputs/sealed-task.txt").exists()
    refused = output.parent / "sealed-attempt"
    with pytest.raises(ManifestError, match="sealed execution requires an isolated agent runtime"):
        run_task(freeze(), task_id="sealed-task", arm_id="memory", output=refused)
    assert not refused.exists()


def replace_program(experiment, role, source):
    manifest, freeze, _ = experiment
    program = manifest["controller"] if role == "controller" else manifest["tasks"][0]["checker"]
    path = freeze().parent / program["script"]["path"]
    path.write_text(source)
    program["script"]["sha256"] = digest(source.encode())


def traced_controller(*, exit_code=0, declared_hash=True, bad_hash=False, api_key=False):
    return (
        "import hashlib,json,os,sys\nfrom pathlib import Path\n"
        "request=json.load(sys.stdin)\n"
        + (
            f"assert hashlib.sha256(os.environ['OPENROUTER_API_KEY'].encode()).hexdigest()=={digest(b'fixture-provider-secret')!r}\n"
            if api_key
            else ""
        )
        + "trace=(json.dumps({'kind':'terminal','reason':'fixture','attempt_id':request['attempt_id']})+'\\n').encode()\n"
        "(Path.home()/'trace.jsonl').write_bytes(trace)\n"
        "Path('answer.txt').write_text(request['memory_pack'])\n"
        "result={'synthetic':False,'model':request['controller_model'],'input_tokens':2,'output_tokens':1,'tool_calls':1,'cost_usd':0.0}\n"
        + (
            "result['trace_sha256']="
            + ("'f'*64" if bad_hash else "hashlib.sha256(trace).hexdigest()")
            + "\n"
            if declared_hash
            else ""
        )
        + f"print(json.dumps(result))\nsys.exit({exit_code})\n"
    )


def test_declared_provider_key_reaches_only_controller(experiment, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-provider-secret")
    experiment[0]["controller_api_key_env"] = "OPENROUTER_API_KEY"
    replace_program(experiment, "controller", traced_controller(api_key=True))
    checker = (FIXTURES / "checker.py").read_text()
    replace_program(
        experiment,
        "checker",
        "import os\nassert 'OPENROUTER_API_KEY' not in os.environ\n" + checker,
    )
    receipt = execute(experiment)
    assert receipt["success"]
    assert receipt["controller_trace"]["declared_complete"]
    assert receipt["usage"]["provenance"] == "controller_reported"
    for path in experiment[2].rglob("*"):
        if path.is_file():
            assert b"fixture-provider-secret" not in path.read_bytes()
    inventory = json.loads((experiment[2] / "final-snapshot.json").read_text())
    assert all("trace" not in entry["path"] for entry in inventory)


def test_undeclared_provider_key_is_withheld(experiment, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-provider-secret")
    replace_program(
        experiment,
        "controller",
        "import os\nassert 'OPENROUTER_API_KEY' not in os.environ\n"
        + (FIXTURES / "controller.py").read_text(),
    )
    assert execute(experiment)["success"]


def test_missing_provider_key_fails_before_output(experiment, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    experiment[0]["controller_api_key_env"] = "OPENROUTER_API_KEY"
    with pytest.raises(ManifestError, match="credential is missing"):
        execute(experiment)
    assert not experiment[2].exists()


@pytest.mark.parametrize("name", ["PATH", "HOME", "LD_PRELOAD", "ARBITRARY_KEY"])
def test_controller_credential_cannot_override_process_environment(experiment, name):
    experiment[0]["controller_api_key_env"] = name
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        execute(experiment)
    assert not experiment[2].exists()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_usage_and_trace_survive_controller_outcome(experiment, exit_code):
    replace_program(experiment, "controller", traced_controller(exit_code=exit_code))
    receipt = execute(experiment)
    assert receipt["status"] == ("passed" if exit_code == 0 else "controller_failed")
    assert receipt["usage"]["input_tokens"] == REPORTED_INPUT_TOKENS
    assert receipt["usage"]["complete"] is True
    trace = (experiment[2] / "controller-trace.jsonl").read_bytes()
    assert receipt["controller_trace"]["sha256"] == digest(trace)
    assert receipt["controller_trace"]["provenance"] == "controller_reported_not_authenticated"
    if exit_code:
        assert "checker" not in receipt
        assert receipt["success"] is False


@pytest.mark.parametrize("bad_hash", [False, True])
def test_real_controller_requires_matching_trace_hash(experiment, bad_hash):
    replace_program(
        experiment, "controller", traced_controller(declared_hash=bad_hash, bad_hash=bad_hash)
    )
    receipt = execute(experiment)
    assert receipt["status"] == "controller_protocol_invalid"
    assert receipt["success"] is False
    assert "checker" not in receipt
    assert receipt["usage"]["input_tokens"] == REPORTED_INPUT_TOKENS
    assert (experiment[2] / "controller-trace.jsonl").exists()


def test_partial_trace_survives_missing_controller_result(experiment):
    source = traced_controller().split("result=")[0] + "sys.exit(7)\n"
    replace_program(experiment, "controller", source)
    receipt = execute(experiment)
    assert receipt["status"] == "controller_failed"
    assert receipt["controller_trace"]["declared_complete"] is False
    assert receipt["usage"]["cost_usd"] is None
    assert "controller_evidence_error" in receipt
    assert (experiment[2] / "controller-trace.jsonl").exists()


@pytest.mark.parametrize("kind", ["symlink", "directory", "empty"])
def test_invalid_trace_never_qualifies_as_retained_evidence(experiment, kind):
    source = traced_controller()
    create = "(Path.home()/'trace.jsonl').write_bytes(trace)"
    replacement = {
        "symlink": "(Path.home()/'trace.jsonl').symlink_to(Path.cwd()/'answer.txt')",
        "directory": "(Path.home()/'trace.jsonl').mkdir()",
        "empty": "(Path.home()/'trace.jsonl').write_bytes(b'')",
    }[kind]
    replace_program(experiment, "controller", source.replace(create, replacement))
    receipt = execute(experiment)
    assert receipt["status"] == "controller_protocol_invalid"
    assert "checker" not in receipt
    assert not receipt.get("controller_trace", {}).get("declared_complete", False)


def test_absent_controller_credential_preserves_legacy_manifest_hash(experiment):
    original = Manifest.model_validate(experiment[0]).model_dump(mode="json")
    experiment[0]["controller_api_key_env"] = None
    assert Manifest.model_validate(experiment[0]).model_dump(mode="json") == original


def test_timeout_allows_cleanup_without_becoming_a_task_outcome(experiment):
    manifest, _, output = experiment
    manifest["controller_timeout_seconds"] = 0.5
    replace_program(
        experiment,
        "controller",
        (
            "import json,time\nfrom pathlib import Path\n"
            "try:\n time.sleep(30)\n"
            "except KeyboardInterrupt:\n Path('cleanup.txt').write_text('finished')\n"
            "print(json.dumps({'synthetic':True,'input_tokens':0,'output_tokens':0,'tool_calls':0,'cost_usd':0.0}))\n"
        ),
    )
    receipt = execute(experiment)
    assert receipt["status"] == "controller_timeout"
    assert receipt["controller"]["exited_during_termination_grace"] is True
    assert (output / "controller-workspace/cleanup.txt").read_text() == "finished"
    assert "checker" not in receipt
    assert receipt["usage"]["complete"] is True


def test_timeout_still_kills_a_controller_that_ignores_interrupt(experiment, monkeypatch):
    monkeypatch.setattr(runner, "TERMINATION_GRACE_SECONDS", 0.1)
    experiment[0]["controller_timeout_seconds"] = 0.5
    replace_program(
        experiment,
        "controller",
        ("import signal,time\nsignal.signal(signal.SIGINT,signal.SIG_IGN)\ntime.sleep(30)\n"),
    )
    receipt = execute(experiment)
    assert receipt["status"] == "controller_timeout"
    assert receipt["controller"]["exited_during_termination_grace"] is False
    assert receipt["controller"]["returncode"] == -signal.SIGKILL
    assert receipt["controller"]["process_group_quiescent"] is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux directory setgid inheritance")
def test_fresh_workspace_does_not_inherit_shared_parent_setgid(experiment):
    manifest, _freeze, output = experiment
    output.parent.chmod(stat.S_IMODE(output.parent.stat().st_mode) | stat.S_ISGID)
    manifest["tasks"][0]["workspace"].append(
        {
            "artifact": manifest["tasks"][0]["workspace"][0]["artifact"],
            "destination": "tests/nested.txt",
        }
    )
    receipt = execute(experiment)
    assert receipt["success"]
    assert output.stat().st_mode & stat.S_ISGID
    workspace = output / "controller-workspace"
    assert not workspace.stat().st_mode & stat.S_ISGID
    assert not (workspace / "tests").stat().st_mode & stat.S_ISGID
    assert (workspace / "tests/nested.txt").read_text() == "0"
    assert inventory(workspace)[0]


@pytest.fixture
def json_oracle_experiment(experiment):

    manifest, freeze, output = experiment
    root = freeze().parent

    def artifact(name, data):
        (root / name).write_bytes(data)
        return {"path": name, "sha256": digest(data)}

    manifest["tasks"][0]["checker"] = {
        "schema_version": "sibyl-json-cli-oracle-v1",
        "oracle": artifact(
            "oracle.json",
            json.dumps(
                {
                    "schema_version": "sibyl-json-cli-cases-v1",
                    "cases": [{"id": "one", "input": {"value": 21}, "expected": 42}],
                }
            ).encode(),
        ),
        "runtime": artifact("runtime.py", Path(coding_controller.__file__).read_bytes()),
        "evaluator": artifact("oracle.py", Path(json_oracle.__file__).read_bytes()),
        "argv": ["python", "app.py"],
        "image": "sha256:" + "a" * 64,
        "docker": "/usr/bin/docker",
        "docker_host": "unix:///run/devbox-docker/docker.sock",
        "timeout_seconds": 1.0,
        "memory_mb": 256,
    }
    return manifest, freeze, output


@pytest.mark.parametrize(
    ("stdout", "exit_code", "execution_status", "expected_status"),
    [
        (b"42\n", 0, "ok", "passed"),
        (b"41\n", 0, "ok", "task_failed"),
        (b"true\n", 0, "ok", "task_failed"),
        (b"42.0\n", 0, "ok", "task_failed"),
        (b"42 trailing", 0, "ok", "candidate_protocol_invalid"),
        (b'{"x": 1, "x": 2}', 0, "ok", "candidate_protocol_invalid"),
        (b"NaN", 0, "ok", "candidate_protocol_invalid"),
        (b"1e999", 0, "ok", "candidate_protocol_invalid"),
        (b"deep-json", 0, "ok", "candidate_protocol_invalid"),
        (b"42\n", 3, "ok", "candidate_failed"),
        (b"", None, "timeout", "candidate_timeout"),
        (b"", None, "operational", "oracle_runtime_error"),
    ],
)
def test_json_oracle_judges_output_outside_candidate(
    json_oracle_experiment, monkeypatch, stdout, exit_code, execution_status, expected_status
):

    if stdout == b"deep-json":
        depth = 100_000
        stdout = b"[" * depth + b"0" + b"]" * depth
    calls = []
    monkeypatch.setattr(runtime, "_container_user", lambda *_: "0:0")

    def execute_container(options, **kwargs):
        calls.append(kwargs)
        assert kwargs["stdin"] == b'{"value":21}\n'
        argv = kwargs["argv"]
        assert argv[argv.index("--entrypoint") + 1 :] == ["python", "sha256:" + "a" * 64, "app.py"]
        assert "--interactive" in argv
        mount = argv[argv.index("--mount") + 1]
        assert mount.endswith("/checker-workspace,dst=/workspace,readonly")
        assert "oracle.json" not in " ".join(argv)
        assert "OPENROUTER_API_KEY" not in kwargs["environment"]
        return {
            "status": execution_status,
            "returncode": exit_code,
            "stdout_base64": base64.b64encode(stdout).decode(),
            "cleanup": {"terminated": True},
        }

    monkeypatch.setattr(runtime, "execute_container", execute_container)
    receipt = execute(json_oracle_experiment)
    assert receipt["status"] == expected_status
    assert receipt["success"] is (expected_status == "passed")
    assert receipt["sealed_isolation"] is False
    assert receipt["outcome"]["authentication"] == "none"
    assert receipt["outcome"]["snapshot_sha256"] == receipt["controller_final_snapshot_sha256"]
    assert receipt["outcome"]["attempt_id"] == receipt["attempt_id"]
    checker = json_oracle_experiment[0]["tasks"][0]["checker"]
    for field in ("oracle", "runtime", "evaluator"):
        assert receipt["outcome"][f"{field}_sha256"] == checker[field]["sha256"]
    assert len(calls) == 1
    assert len(receipt["outcome"]["cases"]) == 1
    retained = receipt["outcome"]["cases"][0]["execution"]
    assert base64.b64decode(retained["stdout_base64"]) == stdout
    saved = json.loads((json_oracle_experiment[2] / "receipt.json").read_bytes())
    assert saved["status"] == expected_status
    outcome = json.loads((json_oracle_experiment[2] / "oracle-outcome.json").read_bytes())
    assert outcome["cases"][0]["execution"] == retained


@pytest.mark.parametrize("field", ["runtime", "evaluator"])
def test_json_oracle_requires_installed_frozen_runtime(json_oracle_experiment, field):
    manifest, freeze, output = json_oracle_experiment
    artifact = manifest["tasks"][0]["checker"][field]
    changed = b"# another implementation\n"
    (freeze().parent / artifact["path"]).write_bytes(changed)
    artifact["sha256"] = digest(changed)
    with pytest.raises(ManifestError, match="runtime differs"):
        execute(json_oracle_experiment)
    assert not output.exists()


def test_json_oracle_still_rejects_sealed_tasks(json_oracle_experiment):
    manifest, _, output = json_oracle_experiment
    manifest["tasks"][0]["split"] = "sealed"
    with pytest.raises(ManifestError, match="sealed execution"):
        execute(json_oracle_experiment)
    assert not output.exists()


def test_json_oracle_does_not_execute_changed_snapshot(json_oracle_experiment, monkeypatch):

    _, freeze, output = json_oracle_experiment
    manifest, inputs = load_manifest(freeze())
    assert isinstance(manifest.tasks[0].checker, JsonOracleChecker)
    output.mkdir()
    (output / "app.py").write_text("unexecuted candidate")
    calls = []
    monkeypatch.setattr(runtime, "execute_container", lambda *args, **kwargs: calls.append(kwargs))
    outcome = evaluate_json_oracle(
        manifest.tasks[0].checker,
        inputs=inputs,
        workspace=output,
        snapshot_sha256="0" * 64,
        attempt_id="fixture",
        timeout_seconds=1.0,
    )
    assert outcome["status"] == "oracle_runtime_error"
    assert calls == []


def test_json_oracle_cannot_be_declared_as_candidate_workspace(json_oracle_experiment):
    manifest, _, output = json_oracle_experiment
    task = manifest["tasks"][0]
    task["workspace"].append({"artifact": task["checker"]["oracle"], "destination": "oracle.json"})
    with pytest.raises(ManifestError, match="private oracle overlaps"):
        execute(json_oracle_experiment)
    assert not output.exists()


@pytest.mark.parametrize("field", ["input", "expected"])
@pytest.mark.parametrize("value", [b"1e999", b"deep-json"])
def test_json_oracle_rejects_unrepresentable_cases_before_attempt(
    json_oracle_experiment, field, value
):
    if value == b"deep-json":
        depth = 100_000
        value = b"[" * depth + b"0" + b"]" * depth
    manifest, freeze, output = json_oracle_experiment
    artifact = manifest["tasks"][0]["checker"]["oracle"]
    path = freeze().parent / artifact["path"]
    case = {"id": "one", "input": None, "expected": None}
    case[field] = "PLACEHOLDER"
    encoded = json.dumps({"schema_version": "sibyl-json-cli-cases-v1", "cases": [case]}).encode()
    encoded = encoded.replace(b'"PLACEHOLDER"', value)
    path.write_bytes(encoded)
    artifact["sha256"] = digest(encoded)
    with pytest.raises(ManifestError, match="unrepresentable JSON"):
        execute(json_oracle_experiment)
    assert not output.exists()


def test_json_oracle_verdict_does_not_depend_on_manifest_json_helpers(monkeypatch):
    from benchmarks.agent_tasks import manifest as manifest_module  # noqa: PLC0415

    def unavailable(*args, **kwargs):
        raise AssertionError("unbound manifest helper influenced the oracle")

    for name in ("strict_json", "canonical_bytes", "identity", "digest"):
        monkeypatch.setattr(manifest_module, name, unavailable)
    result = {"status": "ok", "returncode": 0, "stdout_base64": base64.b64encode(b"42").decode()}
    assert json_oracle._case_status(result, json_oracle.canonical_bytes(42)) == "passed"
    assert json_oracle.identity({"value": 42})
