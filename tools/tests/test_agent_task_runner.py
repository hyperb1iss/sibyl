"""Actual process and snapshot checks for the trusted development task adapter."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unicodedata
from dataclasses import asdict
from pathlib import Path

import pytest
from benchmarks.agent_tasks.manifest import (
    Arm,
    Manifest,
    ManifestError,
    digest,
    identity,
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


def native_payload(source_revision=7):
    """Small Unicode rendering with actual core receipt generation."""
    item = ContextItem(
        id="source-record",
        type="note",
        name="Café 💜",
        content="A verified observation. " * 30,
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
    rendered = render_context_pack(pack, max_content_chars=80)
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
def test_native_binding_automatically_retains_input_outside_controller_workspace(experiment, split):
    experiment[0]["tasks"][0]["split"] = split
    payload = native_payload()
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
        "status": "native_render_v1",
        "native_payload_sha256": arm["native_render_payload"]["sha256"],
        "render_schema_version": "sibyl-context-render-v1",
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
