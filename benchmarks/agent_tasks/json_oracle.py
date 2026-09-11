"""Judge containerized JSON programs without executing candidate code on the host.

The host owns comparisons and runtime admission. Same-user controller execution
still makes these trusted development attempts, not sealed evaluations.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from benchmarks.agent_tasks import coding_controller as runtime
from benchmarks.agent_tasks.manifest import ManifestError
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from benchmarks.agent_tasks.manifest import JsonOracleChecker

# These helpers live in the frozen runtime, not an unbound manifest module.
strict_json = runtime._strict_json
digest = runtime._digest


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def identity(value: Any) -> str:
    return digest(canonical_bytes(value))


class OracleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OracleCase(OracleModel):
    id: str = Field(min_length=1)
    input: Any
    expected: Any


class Oracle(OracleModel):
    schema_version: Literal["sibyl-json-cli-cases-v1"]
    cases: list[OracleCase] = Field(min_length=1)


def validate_oracle_inputs(checker: JsonOracleChecker, inputs: dict[str, bytes]) -> Oracle:
    """Admit only the installed evaluator/runtime bytes; never import a supplied script."""
    for artifact, installed in (
        (checker.runtime, Path(runtime.__file__)),
        (checker.evaluator, Path(__file__)),
    ):
        if (
            digest(inputs[artifact.path]) != artifact.sha256
            or inputs[artifact.path] != installed.read_bytes()
        ):
            raise ManifestError("oracle runtime differs from the installed implementation")
    oracle_bytes = inputs[checker.oracle.path]
    if digest(oracle_bytes) != checker.oracle.sha256:
        raise ManifestError("oracle differs from its frozen artifact")
    try:
        decoded = strict_json(oracle_bytes)
        # Validate every input and expected value before creating an attempt.
        # Parsing a finite-looking exponent can still produce infinity.
        canonical_bytes(decoded)
        oracle = Oracle.model_validate(decoded)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ManifestError("oracle contains invalid or unrepresentable JSON") from exc
    if len({case.id for case in oracle.cases}) != len(oracle.cases):
        raise ManifestError("duplicate oracle case ID")
    return oracle


def _case_status(execution: dict[str, Any], expected_bytes: bytes) -> str:
    if execution["status"] == "operational":
        return "oracle_runtime_error"
    if execution["status"] == "timeout":
        return "candidate_timeout"
    if execution["returncode"] != 0:
        return "candidate_failed"
    try:
        actual = strict_json(base64.b64decode(execution["stdout_base64"], validate=True))
        actual_bytes = canonical_bytes(actual)
    except (ValueError, UnicodeError, RecursionError):
        return "candidate_protocol_invalid"
    return "passed" if actual_bytes == expected_bytes else "task_failed"


def _verify_snapshot(workspace: Path, expected: str) -> None:
    if identity(runtime.inventory(workspace)[0]) != expected:
        raise ValueError("candidate snapshot differs from the frozen submission")


def evaluate_json_oracle(
    checker: JsonOracleChecker,
    *,
    inputs: dict[str, bytes],
    workspace: Path,
    snapshot_sha256: str,
    attempt_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run each case against the frozen read-only submission and compare on the host."""
    oracle = validate_oracle_inputs(checker, inputs)
    receipt: dict[str, Any] = {
        "schema_version": "sibyl-json-cli-outcome-v1",
        "attempt_id": attempt_id,
        "snapshot_sha256": snapshot_sha256,
        "oracle_sha256": checker.oracle.sha256,
        "runtime_sha256": checker.runtime.sha256,
        "evaluator_sha256": checker.evaluator.sha256,
        "checker_sha256": identity(checker.model_dump(mode="json")),
        "image": checker.image,
        "argv": checker.argv,
        "sealed_isolation": False,
        "authentication": "none",
        "cases": [],
        "status": "oracle_runtime_error",
        "passed": False,
    }
    try:
        runtime._require_unprivileged_user()
        _verify_snapshot(workspace, snapshot_sha256)
        options = runtime.Options(
            checker.image,
            checker.timeout_seconds,
            checker.memory_mb,
            checker.docker,
            runtime._container_user(checker.docker, checker.docker_host),
            checker.docker_host,
        )
        environment = runtime._client_environment(checker.docker_host)
        deadline = time.monotonic() + timeout_seconds
        for case in oracle.cases:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                receipt["status"] = "oracle_timeout"
                return receipt
            case_options = replace(options, tool_timeout=min(options.tool_timeout, remaining))
            name = f"sibyl-oracle-{uuid4().hex}"
            stdin = canonical_bytes(case.input) + b"\n"
            expected_bytes = canonical_bytes(case.expected)
            execution = runtime.execute_container(
                case_options,
                name=name,
                argv=runtime.container_argv(
                    case_options, name, workspace, checker.argv, read_only=True, stdin=True
                ),
                environment=environment,
                stdin=stdin,
            )
            status = _case_status(execution, expected_bytes)
            receipt["cases"].append(
                {
                    "id": case.id,
                    "input_sha256": digest(stdin),
                    "expected_sha256": digest(expected_bytes),
                    "status": status,
                    "execution": execution,
                }
            )
            if status == "oracle_runtime_error":
                return receipt
            _verify_snapshot(workspace, snapshot_sha256)
        receipt["status"] = next(
            (case["status"] for case in receipt["cases"] if case["status"] != "passed"),
            "passed",
        )
        receipt["passed"] = receipt["status"] == "passed"
    except (OSError, ValueError, RecursionError, runtime.ControllerError, runtime.Refusal) as exc:
        receipt["status"] = "oracle_runtime_error"
        receipt["detail"] = str(exc)
    return receipt
