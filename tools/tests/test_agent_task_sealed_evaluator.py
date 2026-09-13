"""Synthetic evaluator boundaries; no final task corpus or provider execution."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from benchmarks.agent_tasks import coding_controller, json_oracle, sealed_evaluator
from benchmarks.agent_tasks.manifest import JsonOracleChecker, canonical_bytes, digest, identity
from benchmarks.agent_tasks.sealed_evaluator import (
    SealedEvaluator,
    authorize_submission,
    encode_submission,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sibyl_core.tasks.eval_receipts import ReceiptError, TaskAssignment, verify_learning_evidence

CASE_COUNT = 2


@pytest.fixture
def evaluator_case(tmp_path, monkeypatch):
    inputs = {}

    def artifact(name, content):
        inputs[name] = content
        return {"path": name, "sha256": digest(content)}

    checker = JsonOracleChecker.model_validate(
        {
            "schema_version": "sibyl-json-cli-oracle-v1",
            "oracle": artifact(
                "oracle.json",
                canonical_bytes(
                    {
                        "schema_version": "sibyl-json-cli-cases-v1",
                        "cases": [
                            {"id": "first", "input": {"value": 21}, "expected": 42},
                            {"id": "second", "input": {"value": 4}, "expected": 8},
                        ],
                    }
                ),
            ),
            "runtime": artifact("runtime.py", Path(coding_controller.__file__).read_bytes()),
            "evaluator": artifact("evaluator.py", Path(json_oracle.__file__).read_bytes()),
            "argv": ["node", "app.js"],
            "image": os.environ.get("SIBYL_NATIVE_ORACLE_IMAGE", "sha256:" + "a" * 64),
            "docker": shutil.which("docker") or "/usr/bin/docker",
            "timeout_seconds": 5.0,
            "memory_mb": 256,
        }
    )
    assignment = TaskAssignment(
        organization_id="org",
        owner_principal_id="owner",
        experiment_id="synthetic-final",
        experiment_revision="v1",
        task_id="task",
        task_revision="v1",
        task_sha256="1" * 64,
        family_id="family",
        split="sealed",
        arm_id="sibyl_consolidation",
        checkpoint=10,
        seed=1,
        memory_pack_sha256="2" * 64,
        controller_policy_sha256="3" * 64,
        checker_sha256=identity(checker.model_dump(mode="json")),
        oracle_sha256=checker.oracle.sha256,
        evaluator_sha256=checker.evaluator.sha256,
        runtime_sha256=checker.runtime.sha256,
        image=checker.image,
        attempt_id="a" * 32,
    )
    sender = Ed25519PrivateKey.generate()
    evaluator = SealedEvaluator(
        tmp_path / "persistent",
        assignment,
        checker,
        inputs,
        "issuer",
        Ed25519PrivateKey.generate(),
        sender.public_key(),
        "synthetic-vm",
        15.0,
    )
    evaluator.state_root.mkdir(mode=0o700)
    workspace = tmp_path / "submission"
    workspace.mkdir()
    (workspace / "app.js").write_text(
        "let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>console.log(JSON.parse(s).value*2));"
    )
    bundle = encode_submission(workspace, b"actual synthetic controller trace")
    yield evaluator, sender, bundle, tmp_path / "work"
    receipts = os.environ.get("SIBYL_SEALED_FIXTURE_RECEIPTS")
    if receipts:
        names = [
            json.loads(path.read_bytes())["container"]
            for path in evaluator.state_root.glob("*/case-*.json")
        ]
        absent = []
        for name in names:
            result = subprocess.run(  # noqa: S603 - exact owned synthetic fixture name
                [checker.docker, "inspect", name], capture_output=True, check=False
            )
            assert result.returncode != 0
            absent.append(name)
        receipt = Path(receipts) / (digest(str(tmp_path).encode()) + ".json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_bytes(
            canonical_bytes({"owned_container_names": names, "confirmed_absent": absent})
        )


@pytest.fixture
def fake_oracle(monkeypatch):
    calls = []
    monkeypatch.setattr(coding_controller, "_container_user", lambda *_: "1000:1000")

    def execute(options, **kwargs):
        calls.append(kwargs)
        value = json.loads(kwargs["stdin"])["value"] * 2
        return {
            "status": "ok",
            "returncode": 0,
            "stdout_base64": base64.b64encode(canonical_bytes(value)).decode(),
            "cleanup": {"terminated": True},
        }

    monkeypatch.setattr(coding_controller, "execute_container", execute)
    return calls


def evaluate(case):
    owner, sender, bundle, work = case
    return owner.evaluate(
        bundle, authorize_submission(owner.assignment, bundle, sender), workspace_root=work
    )


def test_sealed_actual_signing_replay_and_learning_refusal(evaluator_case, fake_oracle):
    first = evaluate(evaluator_case)
    owner, sender, bundle, work = evaluator_case
    assert json.loads(first)["payload"]["status"] == "scored"
    assert len(fake_oracle) == CASE_COUNT
    assert (
        replace(owner, instance_id="replacement-vm").evaluate(
            bundle, authorize_submission(owner.assignment, bundle, sender), workspace_root=work
        )
        == first
    )
    assert len(fake_oracle) == CASE_COUNT
    cell = next(path for path in owner.state_root.iterdir() if path.is_dir())
    with pytest.raises(ReceiptError, match="only learning assignments"):
        verify_learning_evidence(
            (cell / "signed-outcome.json").read_bytes(),
            trusted_public_key=owner.signing_key.public_key(),
            trusted_issuer_id=owner.issuer_id,
            expected_assignment=owner.assignment,
            expected_controller_policy_sha256=owner.assignment.controller_policy_sha256,
            outcome_bytes=(cell / "outcome.json").read_bytes(),
            transcript_bytes=b"actual synthetic controller trace",
            episode_bytes=(cell / "audit.json").read_bytes(),
        )


@pytest.mark.parametrize(
    "mutation", ["sender", "assignment", "bundle", "path", "link", "duplicate"]
)
def test_sealed_submission_denials(evaluator_case, fake_oracle, mutation):
    owner, sender, bundle, work = evaluator_case
    authorization = authorize_submission(owner.assignment, bundle, sender)
    if mutation == "sender":
        authorization = authorize_submission(owner.assignment, bundle, Ed25519PrivateKey.generate())
    elif mutation == "assignment":
        owner = replace(owner, assignment=owner.assignment.model_copy(update={"checkpoint": 3}))
    else:
        value = json.loads(bundle)
        if mutation == "bundle":
            value["transcript"] = base64.b64encode(b"changed").decode()
        elif mutation == "path":
            value["snapshot"][1]["path"] = "../outside"
        elif mutation == "link":
            value["snapshot"][1]["kind"] = "symlink"
        else:
            value["snapshot"].append(value["snapshot"][1])
        bundle = canonical_bytes(value)
        if mutation != "bundle":
            authorization = authorize_submission(owner.assignment, bundle, sender)
    with pytest.raises(ValueError, match=r"authenticated|authorization|submission|path"):
        owner.evaluate(bundle, authorization, workspace_root=work)
    assert not fake_oracle


def test_sealed_first_bundle_cannot_change(evaluator_case, fake_oracle):
    evaluate(evaluator_case)
    owner, sender, bundle, work = evaluator_case
    value = json.loads(bundle)
    value["transcript"] = base64.b64encode(b"second submission").decode()
    changed = canonical_bytes(value)
    with pytest.raises(ValueError, match="first submitted"):
        owner.evaluate(
            changed, authorize_submission(owner.assignment, changed, sender), workspace_root=work
        )
    assert len(fake_oracle) == CASE_COUNT


@pytest.mark.parametrize(
    "boundary", ["grading-started.json", "outcome.json", "signed-outcome.json"]
)
def test_sealed_interrupted_owner_never_regrades(
    evaluator_case, fake_oracle, monkeypatch, boundary
):
    original = sealed_evaluator._write

    def stop(path, data):
        original(path, data)
        if path.name == boundary:
            raise KeyboardInterrupt("synthetic process interruption")

    monkeypatch.setattr(sealed_evaluator, "_write", stop)
    with pytest.raises(KeyboardInterrupt):
        evaluate(evaluator_case)
    calls = len(fake_oracle)
    monkeypatch.setattr(sealed_evaluator, "_write", original)
    result = json.loads(evaluate(evaluator_case))["payload"]
    assert result["status"] == (
        "grading_unknown" if boundary == "grading-started.json" else "scored"
    )
    assert len(fake_oracle) == calls


@pytest.mark.skipif(
    not os.environ.get("SIBYL_NATIVE_ORACLE_IMAGE"), reason="explicit owned native image required"
)
@pytest.mark.parametrize("program", ["correct", "wrong", "escape"])
def test_sealed_native_candidate_boundary(evaluator_case, program):
    owner, sender, bundle, work = evaluator_case
    value = json.loads(bundle)
    if program == "wrong":
        code = "console.log(0)"
    elif program == "escape":
        code = "const fs=require('fs');const net=require('net');if(fs.existsSync('/var/run/docker.sock')||fs.existsSync('/oracle.json')||fs.existsSync('/signing-key'))process.exit(9);let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>{const c=net.connect({host:'192.0.2.1',port:80});c.on('connect',()=>process.exit(9));c.on('error',()=>console.log(JSON.parse(s).value*2));});"
    else:
        code = base64.b64decode(value["contents"]["app.js"]).decode()
    value["contents"]["app.js"] = base64.b64encode(code.encode()).decode()
    value["snapshot"][1]["sha256"] = digest(code.encode())
    bundle = canonical_bytes(value)
    result = owner.evaluate(
        bundle, authorize_submission(owner.assignment, bundle, sender), workspace_root=work
    )
    assert json.loads(result)["payload"]["status"] == "scored"
    cell = next(path for path in owner.state_root.iterdir() if path.is_dir())
    outcome = json.loads((cell / "outcome.json").read_bytes())
    assert outcome["passed"] is (program != "wrong")
    assert len(outcome["cases"]) == CASE_COUNT
    for path in cell.glob("case-*.json"):
        name = json.loads(path.read_bytes())["container"]
        assert (
            subprocess.run(  # noqa: S603 - pinned local Docker and owned fixture identities
                [owner.checker.docker, "inspect", name], capture_output=True, check=False
            ).returncode
            != 0
        )


def test_sealed_unknown_on_replacement_vm_stays_unfinalized(
    evaluator_case, fake_oracle, monkeypatch
):
    original = sealed_evaluator._write

    def stop(path, data):
        original(path, data)
        if path.name == "grading-started.json":
            raise KeyboardInterrupt

    monkeypatch.setattr(sealed_evaluator, "_write", stop)
    with pytest.raises(KeyboardInterrupt):
        evaluate(evaluator_case)
    monkeypatch.setattr(sealed_evaluator, "_write", original)
    owner, sender, bundle, work = evaluator_case
    result = replace(owner, instance_id="unrelated-new-vm").evaluate(
        bundle, authorize_submission(owner.assignment, bundle, sender), workspace_root=work
    )
    assert json.loads(result)["payload"]["operationally_finalized"] is False
    assert not list(owner.state_root.glob("*/terminal.json"))
    assert not fake_oracle


@pytest.mark.skipif(
    not os.environ.get("SIBYL_NATIVE_ORACLE_IMAGE"), reason="explicit owned native image required"
)
@pytest.mark.parametrize("foreign_mount", [False, True])
def test_sealed_native_interruption_reconciles_exact_container(
    evaluator_case, monkeypatch, foreign_mount
):
    owner = evaluator_case[0]
    launched = []

    def start_and_interrupt(options, **kwargs):
        argv = coding_controller.container_argv(
            options,
            kwargs["name"],
            Path(
                kwargs["argv"][kwargs["argv"].index("--mount") + 1]
                .split("src=", 1)[1]
                .split(",dst=", 1)[0]
            ),
            ["node", "-e", "setInterval(()=>{},1000)"],
            read_only=True,
        )
        if foreign_mount:
            argv[argv.index("--mount") + 1] = (
                "type=bind,src=" + str(owner.state_root.parent) + ",dst=/workspace,readonly"
            )
        argv.insert(2, "--detach")
        result = subprocess.run(  # noqa: S603 - actual pinned fixture container argv
            argv, capture_output=True, check=True, env=kwargs["environment"]
        )
        launched.append(result.stdout.decode().strip())
        raise KeyboardInterrupt("evaluator process lost during candidate execution")

    monkeypatch.setattr(coding_controller, "execute_container", start_and_interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            evaluate(evaluator_case)
        assert len(launched) == 1
        assert (
            subprocess.run(  # noqa: S603 - pinned local Docker and owned fixture identities
                [owner.checker.docker, "inspect", launched[0]], capture_output=True, check=False
            ).returncode
            == 0
        )
        result = json.loads(evaluate(evaluator_case))["payload"]
        assert result["status"] == "grading_unknown"
        assert result["operationally_finalized"] is (not foreign_mount)
        assert result["cleanup_status"] == ("unverified" if foreign_mount else "verified")
        assert len(launched) == 1
        assert (
            subprocess.run(  # noqa: S603 - pinned local Docker and owned fixture identities
                [owner.checker.docker, "inspect", launched[0]], capture_output=True, check=False
            ).returncode
            == 0
        ) is foreign_mount
        assert bool(list(owner.state_root.glob("*/terminal.json"))) is (not foreign_mount)
    finally:
        for identity_ in launched:
            subprocess.run(  # noqa: S603 - pinned local Docker and owned fixture identities
                [owner.checker.docker, "rm", "--force", identity_], capture_output=True, check=False
            )


def test_sealed_concurrent_duplicate_uses_one_grade(evaluator_case, fake_oracle, monkeypatch):
    entered, release = Event(), Event()
    original = coding_controller.execute_container

    def slow(options, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(options, **kwargs)

    monkeypatch.setattr(coding_controller, "execute_container", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(evaluate, evaluator_case)
        assert entered.wait(5)
        second = pool.submit(evaluate, evaluator_case)
        release.set()
        assert first.result() == second.result()
    assert len(fake_oracle) == CASE_COUNT


@pytest.mark.parametrize("artifact", ["outcome.json", "signed-outcome.json", "audit.json"])
def test_sealed_terminal_replay_revalidates_retained_evidence(
    evaluator_case, fake_oracle, artifact
):
    evaluate(evaluator_case)
    owner = evaluator_case[0]
    cell = next(path for path in owner.state_root.iterdir() if path.is_dir())
    (cell / artifact).write_bytes(b"{}")
    with pytest.raises(ValueError, match=r"snapshot|receipt|episode|audit|evidence"):
        evaluate(evaluator_case)
    assert len(fake_oracle) == CASE_COUNT


@pytest.mark.skipif(
    not os.environ.get("SIBYL_NATIVE_ORACLE_IMAGE"), reason="explicit owned native image required"
)
@pytest.mark.parametrize("boundary", ["grading-started.json", "outcome.json"])
def test_sealed_native_fresh_process_recovery(evaluator_case, boundary):
    owner, sender, bundle, work = evaluator_case
    config = work.parent / "private-synthetic-config.json"
    config.write_bytes(
        canonical_bytes(
            {
                "state": str(owner.state_root),
                "work": str(work),
                "assignment": owner.assignment.model_dump(mode="json"),
                "checker": owner.checker.model_dump(mode="json"),
                "inputs": {key: value.hex() for key, value in owner.inputs.items()},
                "signer": owner.signing_key.private_bytes_raw().hex(),
                "sender": sender.private_bytes_raw().hex(),
                "bundle": bundle.hex(),
            }
        )
    )
    config.chmod(0o600)
    script = work.parent / "synthetic-process.py"
    script.write_text("""
import json, os, sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from benchmarks.agent_tasks import sealed_evaluator as s
from benchmarks.agent_tasks.manifest import JsonOracleChecker
from sibyl_core.tasks.eval_receipts import TaskAssignment
v=json.loads(Path(sys.argv[1]).read_bytes())
sender=Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v['sender']))
owner=s.SealedEvaluator(Path(v['state']),TaskAssignment.model_validate(v['assignment']),
 JsonOracleChecker.model_validate(v['checker']),{k:bytes.fromhex(x) for k,x in v['inputs'].items()},
 'issuer',Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v['signer'])),sender.public_key(),
 'original-vm' if sys.argv[2]!='resume' else 'replacement-vm',15.0)
if sys.argv[2]=='resume':
 def forbidden(*args,**kwargs): raise AssertionError('recovery redispatched oracle')
 s.json_oracle.evaluate_json_oracle=forbidden
else:
 original=s._write
 def crash(path,data):
  original(path,data)
  if path.name==sys.argv[2]: os._exit(17)
 s._write=crash
bundle=bytes.fromhex(v['bundle'])
result=owner.evaluate(bundle,s.authorize_submission(owner.assignment,bundle,sender),workspace_root=Path(v['work']))
Path(sys.argv[3]).write_bytes(result)
""")
    result_path = work.parent / "recovered.json"
    argv = [sys.executable, str(script), str(config), boundary, str(result_path)]
    crashed = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603 - owned synthetic helper
    expected_exit = 17
    assert crashed.returncode == expected_exit, crashed.stderr.decode()
    argv[-2] = "resume"
    resumed = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603 - owned synthetic helper
    assert resumed.returncode == 0, resumed.stderr.decode()
    result = json.loads(result_path.read_bytes())["payload"]
    scored = boundary == "outcome.json"
    assert result["status"] == ("scored" if scored else "grading_unknown")
    assert result["operationally_finalized"] is scored
    cell = next(path for path in owner.state_root.iterdir() if path.is_dir())
    assert len(list(cell.glob("case-*.json"))) == (CASE_COUNT if scored else 0)
    for path in cell.glob("case-*.json"):
        name = json.loads(path.read_bytes())["container"]
        inspected = subprocess.run(  # noqa: S603 - recorded owned fixture identity
            [owner.checker.docker, "inspect", name], capture_output=True, check=False
        )
        assert inspected.returncode != 0


def test_sealed_requires_provisioned_durable_root(evaluator_case, fake_oracle):
    owner, sender, bundle, work = evaluator_case
    owner.state_root.rmdir()
    with pytest.raises(ValueError, match="existing durable"):
        owner.evaluate(
            bundle, authorize_submission(owner.assignment, bundle, sender), workspace_root=work
        )
    assert not fake_oracle


def test_sealed_cell_directory_sync_precedes_grade(evaluator_case, fake_oracle, monkeypatch):
    owner = evaluator_case[0]
    syncs = []
    original_sync = os.fsync
    original_grade = json_oracle.evaluate_json_oracle
    root_identity = owner.state_root.stat().st_ino

    def sync(descriptor):
        syncs.append(os.fstat(descriptor).st_ino)
        original_sync(descriptor)

    def grade(*args, **kwargs):
        assert root_identity in syncs
        return original_grade(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(json_oracle, "evaluate_json_oracle", grade)
    evaluate(evaluator_case)
