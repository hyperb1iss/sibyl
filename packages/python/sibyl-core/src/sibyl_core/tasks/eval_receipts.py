"""Authenticate eval evidence against an independently registered assignment.

Signatures establish the issuer and artifact bindings, not execution isolation
or completeness. Admission must persist the returned identity atomically with
its source write; verification alone neither consumes nor prevents replay.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S+$")]
Status = Literal[
    "passed",
    "task_failed",
    "candidate_failed",
    "candidate_protocol_invalid",
    "candidate_timeout",
    "oracle_runtime_error",
    "oracle_timeout",
]
TASK_STATUSES = frozenset(
    {"passed", "task_failed", "candidate_failed", "candidate_protocol_invalid", "candidate_timeout"}
)
SIGNATURE_DOMAIN = b"sibyl-eval-outcome-v1\x00"


class ReceiptError(ValueError):
    """Evidence is malformed, unauthenticated, or outside the expected assignment."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TaskAssignment(FrozenModel):
    schema_version: Literal["sibyl-eval-assignment-v1"] = "sibyl-eval-assignment-v1"
    organization_id: Identifier
    owner_principal_id: Identifier
    experiment_id: Identifier
    experiment_revision: Identifier
    task_id: Identifier
    task_revision: Identifier
    task_sha256: SHA256
    family_id: Identifier
    split: Literal["learning", "development", "sealed"]
    arm_id: Identifier
    checkpoint: int = Field(ge=0)
    seed: int = Field(ge=0)
    memory_pack_sha256: SHA256
    controller_policy_sha256: SHA256
    checker_sha256: SHA256
    oracle_sha256: SHA256
    evaluator_sha256: SHA256
    runtime_sha256: SHA256
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class EvalOutcome(FrozenModel):
    schema_version: Literal["sibyl-eval-outcome-v1"] = "sibyl-eval-outcome-v1"
    issuer_id: Identifier
    assignment: TaskAssignment
    assignment_sha256: SHA256
    snapshot_sha256: SHA256
    checker_sha256: SHA256
    oracle_sha256: SHA256
    evaluator_sha256: SHA256
    runtime_sha256: SHA256
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    outcome_sha256: SHA256
    transcript_sha256: SHA256
    episode_sha256: SHA256
    status: Status
    success: bool


class SignedOutcome(FrozenModel):
    schema_version: Literal["sibyl-signed-eval-outcome-v1"] = "sibyl-signed-eval-outcome-v1"
    payload: EvalOutcome
    signature: str


@dataclass(frozen=True)
class VerifiedOutcome:
    """Authenticated bytes ready for persistent admission, not an admission receipt."""

    outcome: EvalOutcome
    receipt_sha256: str
    admission_id: str
    outcome_bytes: bytes
    transcript_bytes: bytes
    episode_bytes: bytes


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def assignment_digest(assignment: TaskAssignment) -> str:
    return _digest(_canonical(assignment.model_dump(mode="json")))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ReceiptError("non-finite JSON number")


def _json(value: bytes) -> Any:
    try:
        parsed = json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
        _canonical(parsed)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReceiptError("invalid or unrepresentable receipt JSON") from exc
    return parsed


def _result_fields(outcome_bytes: bytes, assignment: TaskAssignment) -> dict[str, Any]:
    artifact = _json(outcome_bytes)
    if (
        not isinstance(artifact, dict)
        or artifact.get("schema_version") != "sibyl-json-cli-outcome-v1"
    ):
        raise ReceiptError("unsupported oracle outcome artifact")
    if artifact.get("attempt_id") != assignment.attempt_id:
        raise ReceiptError("oracle outcome belongs to another attempt")
    for field in ("checker_sha256", "oracle_sha256", "evaluator_sha256", "runtime_sha256", "image"):
        if artifact.get(field) != getattr(assignment, field):
            raise ReceiptError("oracle outcome differs from the registered execution policy")
    status = artifact.get("status")
    passed = artifact.get("passed")
    if type(passed) is not bool or passed != (status == "passed"):
        raise ReceiptError("oracle status and success disagree")
    return {
        key: artifact.get(key)
        for key in (
            "snapshot_sha256",
            "checker_sha256",
            "oracle_sha256",
            "evaluator_sha256",
            "runtime_sha256",
            "image",
        )
    } | {"status": status, "success": passed}


def _evidence_digests(
    outcome_bytes: bytes, transcript_bytes: bytes, episode_bytes: bytes
) -> dict[str, str]:
    if not outcome_bytes or not transcript_bytes or not episode_bytes:
        raise ReceiptError("outcome, transcript, and episode evidence must be nonempty")
    return {
        "outcome_sha256": _digest(outcome_bytes),
        "transcript_sha256": _digest(transcript_bytes),
        "episode_sha256": _digest(episode_bytes),
    }


def sign_outcome(
    *,
    assignment: TaskAssignment,
    issuer_id: str,
    private_key: Ed25519PrivateKey,
    outcome_bytes: bytes,
    transcript_bytes: bytes,
    episode_bytes: bytes,
) -> bytes:
    """Sign evidence in the oracle-owned process after its own evaluation completes."""
    payload = EvalOutcome(
        issuer_id=issuer_id,
        assignment=assignment,
        assignment_sha256=assignment_digest(assignment),
        **_result_fields(outcome_bytes, assignment),
        **_evidence_digests(outcome_bytes, transcript_bytes, episode_bytes),
    )
    message = SIGNATURE_DOMAIN + _canonical(payload.model_dump(mode="json"))
    signed = SignedOutcome(
        payload=payload, signature=base64.b64encode(private_key.sign(message)).decode()
    )
    return _canonical(signed.model_dump(mode="json"))


def verify_outcome(
    receipt_bytes: bytes,
    *,
    trusted_public_key: Ed25519PublicKey,
    trusted_issuer_id: str,
    expected_assignment: TaskAssignment,
    expected_controller_policy_sha256: str,
    outcome_bytes: bytes,
    transcript_bytes: bytes,
    episode_bytes: bytes,
) -> VerifiedOutcome:
    """Verify against caller-owned trust; no key or assignment is adopted from evidence."""
    try:
        signed = SignedOutcome.model_validate(_json(receipt_bytes))
        signature = base64.b64decode(signed.signature, validate=True)
        trusted_public_key.verify(
            signature, SIGNATURE_DOMAIN + _canonical(signed.payload.model_dump(mode="json"))
        )
    except (ValueError, UnicodeError, RecursionError, InvalidSignature) as exc:
        raise ReceiptError("invalid signed eval outcome") from exc
    payload = signed.payload
    if payload.issuer_id != trusted_issuer_id:
        raise ReceiptError("untrusted outcome issuer")
    if payload.assignment != expected_assignment or payload.assignment_sha256 != assignment_digest(
        expected_assignment
    ):
        raise ReceiptError("outcome differs from the registered assignment")
    if expected_assignment.controller_policy_sha256 != expected_controller_policy_sha256:
        raise ReceiptError("assignment controller policy is not approved")
    expected_fields = _result_fields(outcome_bytes, expected_assignment) | _evidence_digests(
        outcome_bytes, transcript_bytes, episode_bytes
    )
    if any(getattr(payload, field) != value for field, value in expected_fields.items()):
        raise ReceiptError("supplied evidence differs from the signed outcome")
    admission_id = _digest(
        _canonical(
            {
                "domain": "sibyl-eval-admission-v1",
                "organization_id": expected_assignment.organization_id,
                "experiment_id": expected_assignment.experiment_id,
                "attempt_id": expected_assignment.attempt_id,
            }
        )
    )
    return VerifiedOutcome(
        payload,
        _digest(receipt_bytes),
        admission_id,
        outcome_bytes,
        transcript_bytes,
        episode_bytes,
    )


def verify_learning_evidence(
    receipt_bytes: bytes,
    *,
    trusted_public_key: Ed25519PublicKey,
    trusted_issuer_id: str,
    expected_assignment: TaskAssignment,
    expected_controller_policy_sha256: str,
    outcome_bytes: bytes,
    transcript_bytes: bytes,
    episode_bytes: bytes,
) -> VerifiedOutcome:
    """Return only authenticated learning-task results; persistence must prevent replay."""
    verified = verify_outcome(
        receipt_bytes,
        trusted_public_key=trusted_public_key,
        trusted_issuer_id=trusted_issuer_id,
        expected_assignment=expected_assignment,
        expected_controller_policy_sha256=expected_controller_policy_sha256,
        outcome_bytes=outcome_bytes,
        transcript_bytes=transcript_bytes,
        episode_bytes=episode_bytes,
    )
    if verified.outcome.assignment.split != "learning":
        raise ReceiptError("only learning assignments may enter learning admission")
    if verified.outcome.status not in TASK_STATUSES:
        raise ReceiptError("operational failures are not scored learning evidence")
    return verified
