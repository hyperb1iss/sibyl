"""Collect signed learning evidence with a trusted, installed controller.

The host operator and harness are trusted. Candidate containers receive no API
or signing credentials. Shared-user Docker access provides no sealed grading or
hostile-controller isolation, and these receipts make neither claim.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from benchmarks.agent_tasks import coding_controller, runner
from benchmarks.agent_tasks.manifest import (
    Arm,
    JsonOracleChecker,
    Manifest,
    ManifestError,
    Task,
    canonical_bytes,
    checker_artifacts,
    digest,
    identity,
    load_manifest,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sibyl_core.tasks.eval_receipts import (
    TaskAssignment,
    sign_outcome,
    verify_learning_evidence,
)

ASSURANCE = "trusted_learning_harness_v1"
LEARNING_STATUSES = {
    "passed",
    "task_failed",
    "candidate_failed",
    "candidate_protocol_invalid",
    "candidate_timeout",
}


def _json(data: bytes) -> Any:
    try:
        value = coding_controller._strict_json(data)
        canonical_bytes(value)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ManifestError("learning evidence contains invalid JSON") from exc
    return value


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ManifestError(f"learning artifact is not a regular file: {path.name}")
    return path.read_bytes()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def controller_policy(manifest: Manifest) -> dict[str, Any]:
    """Return the exact policy to authorize independently in the eval issuer registry."""
    return {
        "schema_version": "sibyl-trusted-learning-policy-v1",
        "assurance": ASSURANCE,
        "sealed_isolation": False,
        "controller": manifest.controller.model_dump(mode="json"),
        "model": manifest.controller_model,
        "tools": manifest.controller_tools,
        "budget": manifest.controller_budget.model_dump(mode="json"),
        "controller_timeout_seconds": manifest.controller_timeout_seconds,
        "checker_timeout_seconds": manifest.checker_timeout_seconds,
        "dependency_lock": manifest.dependency_lock.model_dump(mode="json"),
        "runtime_sha256": manifest.runtime_sha256,
        "harness_sources": {
            name: digest(Path(__file__).with_name(name).read_bytes())
            for name in ("learning_run.py", "runner.py", "manifest.py")
        },
    }


def _selected(
    path: Path,
    task_id: str,
    arm_id: str,
) -> tuple[Manifest, Task, Arm, dict[str, bytes]]:
    manifest, inputs = load_manifest(path)
    task = next((item for item in manifest.tasks if item.id == task_id), None)
    arm = next((item for item in manifest.arms if item.id == arm_id), None)
    if task is None or arm is None:
        raise ManifestError("unknown learning task or arm")
    if task.split != "learning" or not isinstance(task.checker, JsonOracleChecker):
        raise ManifestError("trusted learning requires a learning-only JSON oracle task")
    installed = Path(coding_controller.__file__).read_bytes()
    if inputs[manifest.controller.script.path] != installed:
        raise ManifestError("learning requires the installed fixed coding controller")
    if manifest.controller_tools != coding_controller.REQUIRED_TOOLS:
        raise ManifestError("learning requires the fixed controller tool contract")
    if manifest.controller_api_key_env != "OPENROUTER_API_KEY":
        raise ManifestError("learning requires an explicitly credentialed real controller")
    # Require the entire executable command to be frozen, without PATH discovery.
    args = manifest.controller.args
    pairs = dict(zip(args[::2], args[1::2], strict=True))
    required = {"--image", "--tool-timeout", "--memory-mb", "--docker"}
    if (
        len(pairs) * 2 != len(args)
        or not required <= pairs.keys()
        or pairs.keys() - required - {"--docker-host"}
    ):
        raise ManifestError("controller arguments must be unique explicit runtime options")
    if (
        pairs["--image"] != task.checker.image
        or pairs["--docker"] != task.checker.docker
        or pairs.get("--docker-host") != task.checker.docker_host
        or not math.isfinite(float(pairs["--tool-timeout"]))
        or float(pairs["--tool-timeout"]) <= 0
        or int(pairs["--memory-mb"]) <= 0
    ):
        raise ManifestError("controller and checker execution policies differ")
    return manifest, task, arm, inputs


def _freeze(
    output: Path,
    manifest: Manifest,
    task: Task,
    arm: Arm,
    inputs: dict[str, bytes],
) -> Path:
    selected = manifest.model_copy(
        update={
            "tasks": [task],
            "arms": [arm],
            "experiences": [e for e in manifest.experiences if e.id in arm.learning_source_ids],
        }
    )
    paths = {
        manifest.dependency_lock.path,
        manifest.controller.script.path,
        task.prompt.path,
        arm.memory_pack.path,
        *(item.path for item in checker_artifacts(task.checker)),
        *(item.artifact.path for item in task.workspace),
        *(item.artifact.path for item in selected.experiences),
    }
    if arm.native_render_payload is not None:
        paths.add(arm.native_render_payload.path)
    for name in sorted(paths):
        _write(output / "frozen" / "inputs" / name, inputs[name])
    # The extra directory keeps a supplied artifact named manifest.json distinct.
    frozen = output / "frozen" / "inputs" / "_learning_manifest.json"
    if frozen.exists():
        raise ManifestError("input collides with the learning manifest")
    _write(frozen, canonical_bytes(selected.model_dump(mode="json")))
    return frozen


def _trace_records(data: bytes, receipt: dict[str, Any]) -> list[dict[str, Any]]:
    if not data or not data.endswith(b"\n"):
        raise ManifestError("controller trace is incomplete")
    records = [_json(line) for line in data.splitlines()]
    fields = {"schema_version", "index", "attempt_id", "request_id", "kind", "payload"}
    for index, record in enumerate(records):
        if (
            not isinstance(record, dict)
            or set(record) != fields
            or not isinstance(record["payload"], dict)
        ):
            raise ManifestError("controller trace record shape is invalid")
        if (
            record["schema_version"] != coding_controller.TRACE_SCHEMA_VERSION
            or type(record["index"]) is not int
            or record["index"] != index
            or record["attempt_id"] != receipt["attempt_id"]
            or record["request_id"] != receipt["request_id"]
        ):
            raise ManifestError("controller trace identity or sequence mismatch")
    return records


def _validate_trace(
    data: bytes, receipt: dict[str, Any], manifest: Manifest
) -> list[dict[str, Any]]:
    records = _trace_records(data, receipt)
    minimum_records = 4  # Start, model request/response, terminal.
    if len(records) < minimum_records:
        raise ManifestError("controller trace does not cover a completed model call")
    start, terminal = records[0], records[-1]
    if (
        start["kind"] != "start"
        or start["payload"].get("script_sha256") != manifest.controller.script.sha256
    ):
        raise ManifestError("controller trace start differs from the installed controller")
    expected = {key: receipt[key] for key in coding_controller.PINNED_FIELDS}
    if start["payload"].get("request") != expected:
        raise ManifestError("controller trace start differs from registered execution")
    if (
        terminal["kind"] != "terminal"
        or terminal["payload"].get("reason") != "stop"
        or terminal["payload"].get("exit") != 0
    ):
        raise ManifestError("controller trace has no successful terminal record")
    usage = receipt["usage"]
    if terminal["payload"].get("usage") != {
        key: usage[key] for key in coding_controller.BUDGET_FIELDS
    }:
        raise ManifestError("controller terminal usage differs from runner usage")
    pending: tuple[str, Any] | None = None
    tools = 0
    for record in records[1:-1]:
        kind, payload = record["kind"], record["payload"]
        if kind in {"model_request", "tool_call"} and pending is None:
            pending = (kind, payload.get("tool_call_id"))
        elif kind == "model_response" and pending == ("model_request", None):
            pending = None
        elif kind == "tool_result" and pending == ("tool_call", payload.get("tool_call_id")):
            if payload.get("index") != tools or payload.get("status") == "operational":
                raise ManifestError("controller tool evidence is incomplete")
            tools += 1
            pending = None
        else:
            raise ManifestError("controller trace action/result order is invalid")
    if pending is not None or tools != receipt["usage"]["tool_calls"]:
        raise ManifestError("controller trace does not cover reported actions")
    return records


def _runner_receipt(
    output: Path, assignment: TaskAssignment, manifest: Manifest
) -> tuple[dict[str, Any], runner.ControllerResult]:
    receipt = _json(_read(output / "receipt.json"))
    expected = {
        "attempt_id": assignment.attempt_id,
        "experiment_id": assignment.experiment_id,
        "task_id": assignment.task_id,
        "task_family_id": assignment.family_id,
        "task_split": "learning",
        "task_sha256": assignment.task_sha256,
        "arm_id": assignment.arm_id,
        "seed": assignment.seed,
        "memory_pack_sha256": assignment.memory_pack_sha256,
        "manifest_sha256": identity(manifest.model_dump(mode="json")),
        "sealed_isolation": False,
    }
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
        raise ManifestError("terminal runner receipt differs from registered inputs")
    if identity({k: v for k, v in receipt.items() if k != "receipt_sha256"}) != receipt.get(
        "receipt_sha256"
    ):
        raise ManifestError("terminal runner receipt hash mismatch")
    if (
        receipt.get("status") not in LEARNING_STATUSES
        or receipt.get("budget_status") != "within_reported_budget"
    ):
        raise ManifestError("runner did not finish an eligible learning attempt within budget")
    controller = receipt.get("controller", {})
    if (
        controller.get("returncode") != 0
        or controller.get("timed_out") is not False
        or controller.get("process_group_quiescent") is not True
    ):
        raise ManifestError("controller did not terminate cleanly")
    protocol = runner.ControllerResult.model_validate(
        _json(_read(output / "controller-stdout.txt"))
    )
    usage = receipt.get("usage", {})
    if runner._controller_usage(protocol) != usage:
        raise ManifestError("retained controller output differs from terminal runner usage")
    if usage.get("complete") is not True or usage.get("synthetic") is not False:
        raise ManifestError("learning requires complete real-controller usage")
    if runner._budget_status(usage, manifest.controller_budget) != "within_reported_budget":
        raise ManifestError("learning usage exceeds its registered budget")
    return receipt, protocol


def _terminal_evidence(
    output: Path,
    assignment: TaskAssignment,
    manifest: Manifest,
    task: Task,
    arm: Arm,
) -> tuple[bytes, bytes, bytes]:
    receipt, protocol = _runner_receipt(output, assignment, manifest)
    snapshot = receipt.get("controller_final_snapshot_sha256")
    if (
        snapshot != receipt.get("checker_input_snapshot_sha256")
        or identity(runner.snapshot(output / "checker-workspace")[0]) != snapshot
    ):
        raise ManifestError("retained submission snapshot differs from the checked candidate")
    if (
        identity(_json(_read(output / "final-snapshot.json"))) != snapshot
        or identity(runner.snapshot(output / "controller-workspace")[0]) != snapshot
    ):
        raise ManifestError("retained controller snapshot differs from its receipt")
    transcript = _read(output / "controller-trace.jsonl")
    if protocol.trace_sha256 != digest(transcript):
        raise ManifestError("controller output trace hash differs from retained evidence")
    if (
        receipt.get("controller_trace", {}).get("sha256") != digest(transcript)
        or receipt["controller_trace"].get("declared_complete") is not True
    ):
        raise ManifestError("retained controller trace differs from its receipt")
    records = _validate_trace(transcript, receipt, manifest)
    outcome = _read(output / "oracle-outcome.json")
    decoded = _json(outcome)
    if (
        decoded.get("status") != receipt["status"]
        or decoded.get("passed") != receipt["success"]
        or decoded.get("snapshot_sha256") != snapshot
    ):
        raise ManifestError("oracle and terminal runner decisions disagree")
    if decoded.get("sealed_isolation") is not False or identity(decoded) != receipt.get(
        "checker", {}
    ).get("outcome_sha256"):
        raise ManifestError("oracle bytes differ from the retained runner decision")
    for field in (
        "attempt_id",
        "checker_sha256",
        "oracle_sha256",
        "evaluator_sha256",
        "runtime_sha256",
        "image",
    ):
        if decoded.get(field) != getattr(assignment, field):
            raise ManifestError("oracle differs from its registered execution policy")
    artifacts = [
        task.prompt,
        arm.memory_pack,
        manifest.controller.script,
        *checker_artifacts(task.checker),
        *(item.artifact for item in task.workspace),
    ]
    for artifact in artifacts:
        if digest(_read(output / "inputs" / artifact.path)) != artifact.sha256:
            raise ManifestError("retained input differs from its registered artifact")
    prompt = _read(output / "inputs" / task.prompt.path).decode("utf-8")
    episode = (
        canonical_bytes(
            {
                "schema_version": "sibyl-learning-episode-v1",
                "assurance": ASSURANCE,
                "sealed_isolation": False,
                "assignment": assignment.model_dump(mode="json"),
                "goal": prompt,
                "trace": records,
                "outcome": decoded,
                "input_memory_pack_sha256": arm.memory_pack.sha256,
            }
        )
        + b"\n"
    )
    return outcome, transcript, episode


async def retry_learning_admission(output: Path, *, client: httpx.AsyncClient) -> dict[str, Any]:
    """Resend the retained signed request only; never invoke controller or signer."""
    bundle = _json(_read(output / "admission-bundle.json"))
    if (
        not isinstance(bundle, dict)
        or bundle.get("schema_version") != "sibyl-learning-admission-bundle-v1"
    ):
        raise ManifestError("unsupported learning admission bundle")
    assignment = TaskAssignment.model_validate(bundle["assignment"])
    body = bundle["request"]
    receipt = base64.b64decode(body["receipt_base64"], validate=True)
    if digest(receipt) != bundle["receipt_sha256"]:
        raise ManifestError("retained admission request was changed")
    path = f"memory/eval/experiments/{quote(assignment.experiment_id, safe='')}/attempts/{assignment.attempt_id}/admit"
    response = await client.post(path, json=body, follow_redirects=False)
    response.raise_for_status()
    result = response.json()
    for key in ("admission_id", "receipt_sha256", "memory_id"):
        if result.get(key) != bundle[key]:
            raise ManifestError("admission response differs from the retained signed attempt")
    if type(result.get("revision")) is not int or result["revision"] < 1:
        raise ManifestError("admission response has no stored source revision")
    runner._write_json(output / "admission-response.json", result)
    return result


async def collect_learning_attempt(
    manifest_path: Path,
    *,
    task_id: str,
    arm_id: str,
    output: Path,
    organization_id: str,
    owner_principal_id: str,
    issuer_id: str,
    checkpoint: int,
    expected_policy_sha256: str,
    client: httpx.AsyncClient,
    load_signing_key: Callable[[], Ed25519PrivateKey],
) -> dict[str, Any]:
    """Register frozen inputs, execute once, retain signed evidence, then admit it."""
    manifest, task, arm, inputs = _selected(manifest_path, task_id, arm_id)
    policy = controller_policy(manifest)
    if identity(policy) != expected_policy_sha256:
        raise ManifestError("controller policy is not the independently authorized policy")
    assert isinstance(task.checker, JsonOracleChecker)
    assignment = TaskAssignment(
        organization_id=organization_id,
        owner_principal_id=owner_principal_id,
        experiment_id=manifest.experiment_id,
        experiment_revision=identity(manifest.model_dump(mode="json")),
        task_id=task.id,
        task_revision=identity(task.model_dump(mode="json")),
        task_sha256=identity(task.model_dump(mode="json")),
        family_id=task.family_id,
        split="learning",
        arm_id=arm.id,
        checkpoint=checkpoint,
        seed=manifest.seed,
        memory_pack_sha256=arm.memory_pack.sha256,
        controller_policy_sha256=expected_policy_sha256,
        checker_sha256=identity(task.checker.model_dump(mode="json")),
        oracle_sha256=task.checker.oracle.sha256,
        evaluator_sha256=task.checker.evaluator.sha256,
        runtime_sha256=task.checker.runtime.sha256,
        image=task.checker.image,
        attempt_id=uuid4().hex,
    )
    output = output.parent.resolve() / output.name
    if output.is_symlink() or output.is_relative_to(manifest_path.parent.resolve()):
        raise ManifestError("learning output must be outside the frozen input directory")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    frozen_path = _freeze(output, manifest, task, arm, inputs)
    frozen_digest = identity(load_manifest(frozen_path)[0].model_dump(mode="json"))
    _write(output / "assignment.json", canonical_bytes(assignment.model_dump(mode="json")))
    _write(output / "policy.json", canonical_bytes(policy))
    response = await client.post(
        "memory/eval/assignments",
        json={"issuer_id": issuer_id, "assignment": assignment.model_dump(mode="json")},
        follow_redirects=False,
    )
    response.raise_for_status()
    if TaskAssignment.model_validate(response.json()) != assignment:
        raise ManifestError("registered assignment differs from the planned attempt")
    frozen, selected_task, selected_arm, _ = _selected(frozen_path, task_id, arm_id)
    if identity(frozen.model_dump(mode="json")) != frozen_digest:
        raise ManifestError("registered frozen inputs changed before execution")
    if identity(controller_policy(frozen)) != expected_policy_sha256:
        raise ManifestError("frozen controller policy changed after registration")
    _write(
        output / "execution-started.json", canonical_bytes({"attempt_id": assignment.attempt_id})
    )
    await asyncio.to_thread(
        runner.run_task,
        frozen_path,
        task_id=task_id,
        arm_id=arm_id,
        output=output / "execution",
        attempt_id=assignment.attempt_id,
    )
    outcome, transcript, episode = _terminal_evidence(
        output / "execution", assignment, frozen, selected_task, selected_arm
    )
    key = load_signing_key()
    receipt = sign_outcome(
        assignment=assignment,
        issuer_id=issuer_id,
        private_key=key,
        outcome_bytes=outcome,
        transcript_bytes=transcript,
        episode_bytes=episode,
    )
    verified = verify_learning_evidence(
        receipt,
        trusted_public_key=key.public_key(),
        trusted_issuer_id=issuer_id,
        expected_assignment=assignment,
        expected_controller_policy_sha256=expected_policy_sha256,
        outcome_bytes=outcome,
        transcript_bytes=transcript,
        episode_bytes=episode,
    )
    material = {
        "receipt": receipt,
        "outcome": outcome,
        "transcript": transcript,
        "episode": episode,
    }
    for name, data in material.items():
        _write(output / f"signed-{name}.bin", data)
    bundle = {
        "schema_version": "sibyl-learning-admission-bundle-v1",
        "assurance": ASSURANCE,
        "sealed_isolation": False,
        "assignment": assignment.model_dump(mode="json"),
        "request": {
            "issuer_id": issuer_id,
            **{
                f"{name}_base64": base64.b64encode(data).decode("ascii")
                for name, data in material.items()
            },
        },
        "receipt_sha256": verified.receipt_sha256,
        "admission_id": verified.admission_id,
        "memory_id": str(uuid5(NAMESPACE_URL, "sibyl-eval:" + verified.admission_id)),
    }
    _write(output / "admission-bundle.json", canonical_bytes(bundle))
    return await retry_learning_admission(output, client=client)


def _signing_key(path: Path) -> Ed25519PrivateKey:
    if (
        path.is_symlink()
        or not stat.S_ISREG(path.stat().st_mode)
        or stat.S_IMODE(path.stat().st_mode) & 0o077
    ):
        raise ManifestError("signing key must be a private regular file")
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("policy")
    inspect.add_argument("--manifest", type=Path, required=True)
    for name in ("run", "admit"):
        command = commands.add_parser(name)
        command.add_argument("--api-url", required=True, help="Sibyl API base URL, including /api/")
        command.add_argument("--api-token-env", default="SIBYL_EVAL_API_TOKEN")
        command.add_argument("--output", type=Path, required=True)
        if name == "run":
            command.add_argument("--manifest", type=Path, required=True)
            for field in ("task", "arm", "organization", "owner", "issuer", "policy-sha256"):
                command.add_argument("--" + field, required=True)
            command.add_argument("--checkpoint", type=int, required=True)
            command.add_argument("--signing-key", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "policy":
        manifest, _ = load_manifest(args.manifest)
        policy = controller_policy(manifest)
        sys.stdout.write(
            json.dumps(
                {
                    "experiment_id": manifest.experiment_id,
                    "experiment_revision": identity(manifest.model_dump(mode="json")),
                    "policy": policy,
                    "sha256": identity(policy),
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 0

    async def execute():
        token = os.environ.get(args.api_token_env)
        if not token or any(c in token for c in "\r\n\x00"):
            raise ManifestError("explicit API token environment variable is missing or invalid")
        async with httpx.AsyncClient(
            base_url=args.api_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            if args.command == "admit":
                return await retry_learning_admission(args.output, client=client)
            return await collect_learning_attempt(
                args.manifest,
                task_id=args.task,
                arm_id=args.arm,
                output=args.output,
                organization_id=args.organization,
                owner_principal_id=args.owner,
                issuer_id=args.issuer,
                checkpoint=args.checkpoint,
                expected_policy_sha256=args.policy_sha256,
                client=client,
                load_signing_key=lambda: _signing_key(args.signing_key),
            )

    result = asyncio.run(execute())
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
