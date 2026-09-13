"""Evaluator-owned submission and grading receipts, without sealed-run activation.

The caller supplies trusted policy, keys and persistent private state separately
from untrusted submission bytes. VM isolation and model transport qualification
are prerequisites of a future executor, not properties asserted by this owner.
"""

from __future__ import annotations

import base64
import fcntl
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks.agent_tasks import coding_controller, json_oracle
from benchmarks.agent_tasks.learning_run import _read, _write
from benchmarks.agent_tasks.manifest import (
    canonical_bytes,
    digest,
    identity,
    relative_path,
    strict_json,
)
from benchmarks.agent_tasks.runner import snapshot
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from sibyl_core.tasks.eval_receipts import assignment_digest, sign_outcome, verify_outcome

if TYPE_CHECKING:
    from benchmarks.agent_tasks.manifest import JsonOracleChecker
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from sibyl_core.tasks.eval_receipts import TaskAssignment

SUBMISSION_DOMAIN = b"sibyl-sealed-submission-v1\x00"
TERMINAL_DOMAIN = b"sibyl-sealed-evaluator-terminal-v1\x00"
MAX_PERMISSIONS = 0o777

ARMS = {"no_memory", "raw_retrieval", "simple_summary", "sibyl_consolidation"}


def encode_submission(workspace: Path, transcript: bytes) -> bytes:
    """Capture exact regular workspace bytes; the sender signs the returned digest."""
    entries, contents = snapshot(workspace)
    return canonical_bytes(
        {
            "schema_version": "sibyl-sealed-submission-v1",
            "snapshot": entries,
            "contents": {name: base64.b64encode(data).decode() for name, data in contents.items()},
            "transcript": base64.b64encode(transcript).decode(),
        }
    )


def authorize_submission(
    assignment: TaskAssignment, bundle: bytes, key: Ed25519PrivateKey
) -> bytes:
    """Run only in the trusted submission sender, never in the candidate workspace."""
    payload = {"assignment_sha256": assignment_digest(assignment), "bundle_sha256": digest(bundle)}
    return canonical_bytes(
        {
            "payload": payload,
            "signature": base64.b64encode(
                key.sign(SUBMISSION_DOMAIN + canonical_bytes(payload))
            ).decode(),
        }
    )


def _authenticate(data: bytes, domain: bytes, key: Ed25519PublicKey) -> dict[str, Any]:
    value = strict_json(data)
    if not isinstance(value, dict) or set(value) != {"payload", "signature"}:
        raise ValueError("invalid authenticated envelope")
    try:
        key.verify(
            base64.b64decode(value["signature"], validate=True),
            domain + canonical_bytes(value["payload"]),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ValueError("invalid authenticated envelope") from exc
    if not isinstance(value["payload"], dict):
        raise TypeError("invalid authenticated payload")
    return value["payload"]


def _decode_bundle(data: bytes) -> tuple[list[dict[str, Any]], dict[str, bytes], bytes]:
    value = strict_json(data)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "snapshot",
        "contents",
        "transcript",
    }:
        raise ValueError("invalid submission bundle")
    if value["schema_version"] != "sibyl-sealed-submission-v1":
        raise ValueError("unsupported submission bundle")
    entries, encoded = value["snapshot"], value["contents"]
    if not isinstance(entries, list) or not entries or not isinstance(encoded, dict):
        raise ValueError("submission inventory missing")
    files = _validate_entries(entries)
    if set(encoded) != files:
        raise ValueError("submission content inventory differs")
    try:
        contents = {
            name: base64.b64decode(content, validate=True) for name, content in encoded.items()
        }
        transcript = base64.b64decode(value["transcript"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid submission encoding") from exc
    if not transcript or any(
        digest(contents[e["path"]]) != e["sha256"] for e in entries if e["kind"] == "file"
    ):
        raise ValueError("submission evidence differs")
    return entries, contents, transcript


def _validate_entries(entries: list[dict[str, Any]]) -> set[str]:
    seen = set()
    files = set()
    directories = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("kind") not in {"file", "directory"}:
            raise ValueError("submission contains a link or special entry")
        expected = {"path", "kind", "mode"} | ({"sha256"} if entry["kind"] == "file" else set())
        if (
            set(entry) != expected
            or type(entry["mode"]) is not int
            or not 0 <= entry["mode"] <= MAX_PERMISSIONS
        ):
            raise ValueError("invalid submission entry")
        name = entry["path"]
        if index == 0:
            if name != "." or entry["kind"] != "directory":
                raise ValueError("submission root differs")
        else:
            if not isinstance(name, str):
                raise ValueError("invalid submission path")
            relative_path(name)
            if str(Path(name).parent) not in directories:
                raise ValueError("submission parent is missing or unordered")
        if name in seen:
            raise ValueError("duplicate submission path")
        seen.add(name)
        if entry["kind"] == "directory":
            directories.add(name)
        if entry["kind"] == "file":
            files.add(name)
    return files


def _persist_identical(path: Path, data: bytes, error: str) -> None:
    if path.exists():
        if _read(path) != data:
            raise ValueError(error)
    else:
        _write(path, data)


def _public_digest(key: Ed25519PublicKey) -> str:
    return digest(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


@dataclass(frozen=True)
class SealedEvaluator:
    """Trusted evaluator policy and persistent state, never loaded from a submission."""

    state_root: Path
    assignment: TaskAssignment
    checker: JsonOracleChecker
    inputs: dict[str, bytes] = field(repr=False)
    issuer_id: str
    signing_key: Ed25519PrivateKey = field(repr=False)
    sender_key: Ed25519PublicKey = field(repr=False)
    instance_id: str
    timeout_seconds: float

    def _registration(self) -> dict[str, Any]:
        assignment = self.assignment
        if (
            assignment.split != "sealed"
            or assignment.arm_id not in ARMS
            or assignment.checkpoint not in {0, 1, 3, 10}
        ):
            raise ValueError("registered sealed cell required")
        if not self.instance_id or not self.issuer_id or self.timeout_seconds <= 0:
            raise ValueError("evaluator identity and timeout required")
        json_oracle.validate_oracle_inputs(self.checker, self.inputs)
        expected = {
            "checker_sha256": identity(self.checker.model_dump(mode="json")),
            "oracle_sha256": self.checker.oracle.sha256,
            "runtime_sha256": self.checker.runtime.sha256,
            "evaluator_sha256": self.checker.evaluator.sha256,
            "image": self.checker.image,
        }
        if any(getattr(assignment, key) != value for key, value in expected.items()):
            raise ValueError("checker differs from private assignment")
        return {
            "assignment": assignment.model_dump(mode="json"),
            "issuer_id": self.issuer_id,
            "issuer_key_sha256": _public_digest(self.signing_key.public_key()),
            "sender_key_sha256": _public_digest(self.sender_key),
            "timeout_seconds": self.timeout_seconds,
        }

    def evaluate(self, bundle: bytes, authorization: bytes, *, workspace_root: Path) -> bytes:
        """Bind the first submission, grade once, and replay exact authenticated receipts."""
        registration = self._registration()
        expected = {
            "assignment_sha256": assignment_digest(self.assignment),
            "bundle_sha256": digest(bundle),
        }
        if _authenticate(authorization, SUBMISSION_DOMAIN, self.sender_key) != expected:
            raise ValueError("submission authorization differs")
        entries, contents, transcript = _decode_bundle(bundle)
        root = self.state_root
        if not root.is_dir():
            raise ValueError("existing durable evaluator state root required")
        if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) & 0o077:
            raise ValueError("evaluator state must be private")
        if root.resolve().is_relative_to(
            workspace_root.resolve()
        ) or workspace_root.resolve().is_relative_to(root.resolve()):
            raise ValueError("candidate workspace overlaps evaluator state")
        cell_id = identity(
            {
                "org": self.assignment.organization_id,
                "experiment": self.assignment.experiment_id,
                "attempt": self.assignment.attempt_id,
            }
        )
        cell = root / cell_id
        cell.mkdir(exist_ok=True, mode=0o700)
        if cell.is_symlink():
            raise ValueError("invalid evaluator cell")
        root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(root_descriptor)
        finally:
            os.close(root_descriptor)
        descriptor = os.open(cell / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return self._evaluate_locked(
                cell, registration, bundle, entries, contents, transcript, workspace_root
            )
        finally:
            os.close(descriptor)

    def _evaluate_locked(
        self, cell, registration, bundle, entries, contents, transcript, workspace_root
    ):
        registered = canonical_bytes(registration)
        _persist_identical(
            cell / "registration.json", registered, "cell registration cannot change"
        )
        _persist_identical(cell / "submission.json", bundle, "first submitted bundle cannot change")
        terminal = cell / "terminal.json"
        if terminal.exists():
            data = _read(terminal)
            payload = _authenticate(data, TERMINAL_DOMAIN, self.signing_key.public_key())
            if payload["registration_sha256"] != digest(registered) or payload[
                "bundle_sha256"
            ] != digest(bundle):
                raise ValueError("terminal cell identity differs")
            if payload["status"] == "scored":
                recorded_receipt = _read(cell / "signed-outcome.json")
                if digest(recorded_receipt) != payload["outcome_receipt_sha256"]:
                    raise ValueError("terminal outcome receipt changed")
                recorded_outcome = _read(cell / "outcome.json")
                self._validate_outcome(recorded_outcome, entries)
                verify_outcome(
                    recorded_receipt,
                    trusted_public_key=self.signing_key.public_key(),
                    trusted_issuer_id=self.issuer_id,
                    expected_assignment=self.assignment,
                    expected_controller_policy_sha256=self.assignment.controller_policy_sha256,
                    outcome_bytes=recorded_outcome,
                    transcript_bytes=transcript,
                    episode_bytes=_read(cell / "audit.json"),
                )
            return data
        outcome_path = cell / "outcome.json"
        if outcome_path.exists():
            outcome = _read(outcome_path)
        elif (cell / "grading-started.json").exists():
            cleanup = self._reconcile_unknown(cell)
            return self._terminal(
                cell, registered, bundle, "grading_unknown", None, cleanup=cleanup
            )
        else:
            workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(
                dir=workspace_root, prefix="sealed-grade-"
            ) as directory:
                workspace = Path(directory)
                coding_controller._write_tree(workspace, entries, contents)
                actual, _ = snapshot(workspace)
                if actual != entries:
                    raise ValueError("imported submission snapshot differs")
                _write(
                    cell / "grading-started.json",
                    canonical_bytes(
                        {
                            "instance_id": self.instance_id,
                            "snapshot_sha256": identity(entries),
                            "bundle_sha256": digest(bundle),
                        }
                    ),
                )

                def before_case(case_id: str, name: str) -> None:
                    _write(
                        cell / ("case-" + digest(case_id.encode()) + ".json"),
                        canonical_bytes(
                            {
                                "case_id": case_id,
                                "container": name,
                                "image": self.checker.image,
                                "instance_id": self.instance_id,
                                "workspace": str(workspace),
                            }
                        ),
                    )

                result = json_oracle.evaluate_json_oracle(
                    self.checker,
                    inputs=self.inputs,
                    workspace=workspace,
                    snapshot_sha256=identity(entries),
                    attempt_id=self.assignment.attempt_id,
                    timeout_seconds=self.timeout_seconds,
                    before_case=before_case,
                )
                oracle = json_oracle.validate_oracle_inputs(self.checker, self.inputs)
                if result["passed"] and [row["id"] for row in result["cases"]] != [
                    case.id for case in oracle.cases
                ]:
                    raise ValueError("passing oracle omitted registered cases")
                outcome = canonical_bytes(result)
                _write(outcome_path, outcome)
        self._validate_outcome(outcome, entries)
        audit = canonical_bytes(
            {
                "schema_version": "sibyl-sealed-audit-v1",
                "assignment_sha256": assignment_digest(self.assignment),
                "snapshot_sha256": identity(entries),
                "transcript_sha256": digest(transcript),
            }
        )
        receipt = sign_outcome(
            assignment=self.assignment,
            issuer_id=self.issuer_id,
            private_key=self.signing_key,
            outcome_bytes=outcome,
            transcript_bytes=transcript,
            episode_bytes=audit,
        )
        verify_outcome(
            receipt,
            trusted_public_key=self.signing_key.public_key(),
            trusted_issuer_id=self.issuer_id,
            expected_assignment=self.assignment,
            expected_controller_policy_sha256=self.assignment.controller_policy_sha256,
            outcome_bytes=outcome,
            transcript_bytes=transcript,
            episode_bytes=audit,
        )
        for name, data in (("audit.json", audit), ("signed-outcome.json", receipt)):
            _persist_identical(cell / name, data, "persisted outcome evidence differs")
        decoded = strict_json(outcome)
        cases = decoded["cases"]
        complete_cleanup = len(list(cell.glob("case-*.json"))) == len(cases) and all(
            row.get("execution", {}).get("cleanup", {}).get("terminated") is True for row in cases
        )
        cleanup = None if complete_cleanup else self._reconcile_unknown(cell)
        return self._terminal(cell, registered, bundle, "scored", receipt, cleanup=cleanup)

    def _validate_outcome(self, outcome: bytes, entries: list[dict[str, Any]]) -> None:
        result = strict_json(outcome)
        if result.get("snapshot_sha256") != identity(entries):
            raise ValueError("oracle snapshot differs from bound submission")
        oracle = json_oracle.validate_oracle_inputs(self.checker, self.inputs)
        if result.get("passed") and [row["id"] for row in result["cases"]] != [
            case.id for case in oracle.cases
        ]:
            raise ValueError("passing oracle omitted registered cases")

    def _reconcile_unknown(self, cell):
        started = strict_json(_read(cell / "grading-started.json"))
        records = []
        if started["instance_id"] != self.instance_id:
            return {
                "verified": False,
                "reason": "original evaluator instance unavailable",
                "containers": records,
            }
        options = coding_controller.Options(
            self.checker.image,
            self.checker.timeout_seconds,
            self.checker.memory_mb,
            self.checker.docker,
            "0:0",
            self.checker.docker_host,
        )
        environment = coding_controller._client_environment(self.checker.docker_host)

        def inspect_container(name):
            try:
                result = subprocess.run(  # noqa: S603 - executable and daemon are registered evaluator policy.
                    [self.checker.docker, "container", "inspect", name],
                    capture_output=True,
                    env=environment,
                    timeout=coding_controller.CLEANUP_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return "unavailable", None
            if result.returncode:
                text = result.stderr.decode("utf-8", "replace").lower()
                return (
                    "absent"
                    if "no such container" in text or "no such object" in text
                    else "unavailable"
                ), None
            rows = strict_json(result.stdout)
            if not isinstance(rows, list) or len(rows) != 1:
                return "unavailable", None
            return "present", rows[0]

        verified = True
        for path in sorted(cell.glob("case-*.json")):
            recorded = strict_json(_read(path))
            status, actual = inspect_container(recorded["container"])
            if status == "absent":
                records.append({"container": recorded["container"], "status": "absent"})
                continue
            binds = (
                [mount for mount in actual.get("Mounts", []) if mount.get("Type") == "bind"]
                if actual
                else []
            )
            if (
                actual is None
                or recorded["instance_id"] != self.instance_id
                or actual.get("Name") != "/" + recorded["container"]
                or actual.get("Image") != recorded["image"]
                or actual.get("HostConfig", {}).get("NetworkMode") != "none"
                or len(binds) != 1
                or binds[0].get("Source") != recorded["workspace"]
                or binds[0].get("Destination") != "/workspace"
                or binds[0].get("RW") is not False
            ):
                verified = False
                records.append(
                    {"container": recorded["container"], "status": "unverified_identity"}
                )
                continue
            outcome = coding_controller._cleanup_container(options, actual["Id"], environment)
            gone, _ = inspect_container(actual["Id"])
            complete = outcome["terminated"] and gone == "absent"
            verified = verified and complete
            records.append(
                {"container": recorded["container"], "id": actual["Id"], "terminated": complete}
            )
        result = {"verified": verified, "containers": records}
        _write(
            cell / ("recovery-" + str(len(list(cell.glob("recovery-*.json")))) + ".json"),
            canonical_bytes(result),
        )
        return result

    def _terminal(self, cell, registration, bundle, status, outcome, *, cleanup=None):
        payload = {
            "schema_version": "sibyl-sealed-evaluator-terminal-v1",
            "status": status,
            "registration_sha256": digest(registration),
            "bundle_sha256": digest(bundle),
            "outcome_receipt_sha256": digest(outcome) if outcome else None,
            "sealed_execution_qualified": False,
            "cleanup_status": ("verified" if cleanup["verified"] else "unverified")
            if cleanup is not None
            else "oracle_recorded",
            "operationally_finalized": cleanup is None or cleanup["verified"],
        }
        data = canonical_bytes(
            {
                "payload": payload,
                "signature": base64.b64encode(
                    self.signing_key.sign(TERMINAL_DOMAIN + canonical_bytes(payload))
                ).decode(),
            }
        )
        if payload["operationally_finalized"]:
            _write(cell / "terminal.json", data)
        return data
