"""Permanent assignment registration and transactional verified learning admission.

Callers authorize registration and supply independently configured signing trust.
Signed tenant fields never choose the destination; the stored assignment does.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sibyl_core.auth.memory_policy import EVAL_ADMISSION_METADATA_KEY
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.services import content_client
from sibyl_core.services.content_models import (
    RawMemory,
    SurrealRecord,
    raw_memory_from_record,
    raw_memory_record,
)
from sibyl_core.tasks.eval_receipts import (
    ReceiptError,
    TaskAssignment,
    assignment_digest,
    verify_learning_evidence,
)


class EvalAdmissionConflict(ValueError):
    """A registered attempt or its admitted capture cannot be replaced."""


@dataclass(frozen=True)
class EvalAdmissionResult:
    memory: RawMemory
    assignment: TaskAssignment
    receipt_sha256: str
    admission_id: str


def _attempt_key(organization_id: str, experiment_id: str, attempt_id: str) -> str:
    if not organization_id or not experiment_id or not attempt_id:
        raise ValueError("explicit organization, experiment, and attempt are required")
    return hashlib.sha256(
        json.dumps([organization_id, experiment_id, attempt_id]).encode()
    ).hexdigest()


_REGISTER = """
BEGIN TRANSACTION;
RETURN {
LET $existing = (SELECT * FROM eval_attempts WHERE uuid = $uuid LIMIT 1)[0];
IF $existing != NONE {
    IF $existing.organization_id != $organization_id
        OR $existing.assignment_sha256 != $assignment_sha256 {
        THROW 'eval admission conflict: assignment is immutable';
    };
} ELSE {
    CREATE eval_attempts CONTENT $record;
};
RETURN (SELECT * FROM eval_attempts WHERE uuid = $uuid AND organization_id = $organization_id);
};
COMMIT TRANSACTION;
"""

_ADMIT = """
BEGIN TRANSACTION;
RETURN {
LET $attempt = (SELECT * FROM eval_attempts
    WHERE uuid = $uuid AND organization_id = $organization_id LIMIT 1)[0];
IF $attempt = NONE OR $attempt.assignment_sha256 != $assignment_sha256 {
    THROW 'eval admission conflict: registered assignment changed';
};
IF $attempt.receipt_sha256 != NONE {
    IF $attempt.receipt_sha256 != $receipt_sha256 OR $attempt.capture_id != $capture_id {
        THROW 'eval admission conflict: attempt already admitted different evidence';
    };
} ELSE {
    CREATE raw_captures CONTENT $capture;
    UPDATE eval_attempts SET
        receipt_sha256 = $receipt_sha256, receipt_base64 = $receipt_base64,
        outcome_sha256 = $outcome_sha256, transcript_sha256 = $transcript_sha256,
        episode_sha256 = $episode_sha256, capture_id = $capture_id,
        admitted_at = time::now()
    WHERE uuid = $uuid AND organization_id = $organization_id
        AND assignment_sha256 = $assignment_sha256;
};
LET $memory = (SELECT * FROM raw_captures
    WHERE uuid = $capture_id AND organization_id = $organization_id LIMIT 1)[0];
IF $memory = NONE OR $memory.deleted_at != NONE
    OR $memory.principal_id != $principal_id OR $memory.memory_scope != 'private'
    OR $memory.metadata.eval_admission.receipt_sha256 != $receipt_sha256
    OR crypto::sha256($memory.raw_content) != $episode_sha256 {
    THROW 'eval admission conflict: admitted capture is absent or changed';
};
RETURN $memory;
};
COMMIT TRANSACTION;
"""


async def _transaction(
    client: SurrealContentClient,
    query: str,
    **params: object,
) -> list[SurrealRecord]:
    # The single RETURN block retains the checked client's first-result contract.
    # Its atomic conflict replay validates every statement before returning.
    result = await client.execute_query(query, **params)
    return content_client.normalize_records(result)


async def register_eval_assignment(
    *, organization_id: str, assignment: TaskAssignment
) -> TaskAssignment:
    """Register immutable destination and policy before execution; caller authorizes this write."""
    if assignment.organization_id != organization_id:
        raise EvalAdmissionConflict("assignment organization differs from destination")
    key = _attempt_key(organization_id, assignment.experiment_id, assignment.attempt_id)
    record = {
        "uuid": key,
        "organization_id": organization_id,
        "experiment_id": assignment.experiment_id,
        "attempt_id": assignment.attempt_id,
        "assignment_sha256": assignment_digest(assignment),
        "assignment_json": assignment.model_dump_json(),
    }
    async with content_client.surreal_content_client() as client:
        try:
            await _transaction(client, _REGISTER, **record, record=record)
        except Exception as exc:
            _raise_conflict(exc)
    return assignment


def _raise_conflict(exc: Exception) -> None:
    if "eval admission conflict:" in str(exc):
        raise EvalAdmissionConflict(str(exc)) from exc
    raise exc


async def get_registered_eval_assignment(
    *,
    organization_id: str,
    experiment_id: str,
    attempt_id: str,
) -> TaskAssignment | None:
    key = _attempt_key(organization_id, experiment_id, attempt_id)
    async with content_client.surreal_content_client() as client:
        row = await content_client.select_one(
            client,
            "SELECT assignment_json, assignment_sha256 FROM eval_attempts "
            "WHERE uuid = $uuid AND organization_id = $organization_id LIMIT 1;",
            uuid=key,
            organization_id=organization_id,
        )
    if row is None:
        return None
    assignment = TaskAssignment.model_validate_json(str(row["assignment_json"]))
    if (
        assignment.organization_id != organization_id
        or assignment.experiment_id != experiment_id
        or assignment.attempt_id != attempt_id
        or assignment_digest(assignment) != row["assignment_sha256"]
    ):
        raise EvalAdmissionConflict("stored assignment binding is inconsistent")
    return assignment


async def admit_eval_outcome(
    *,
    organization_id: str,
    experiment_id: str,
    attempt_id: str,
    principal_id: str,
    issuer_id: str,
    trusted_public_key: Ed25519PublicKey,
    expected_controller_policy_sha256: str,
    receipt_bytes: bytes,
    outcome_bytes: bytes,
    transcript_bytes: bytes,
    episode_bytes: bytes,
) -> EvalAdmissionResult:
    """Verify supplied evidence and atomically bind it to one private raw capture."""
    assignment = await get_registered_eval_assignment(
        organization_id=organization_id,
        experiment_id=experiment_id,
        attempt_id=attempt_id,
    )
    if assignment is None:
        raise LookupError("eval assignment not found")
    if assignment.owner_principal_id != principal_id:
        raise PermissionError("eval assignment belongs to another principal")
    verified = verify_learning_evidence(
        receipt_bytes,
        trusted_public_key=trusted_public_key,
        trusted_issuer_id=issuer_id,
        expected_assignment=assignment,
        expected_controller_policy_sha256=expected_controller_policy_sha256,
        outcome_bytes=outcome_bytes,
        transcript_bytes=transcript_bytes,
        episode_bytes=episode_bytes,
    )
    try:
        text = episode_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise ReceiptError("learning episode must be UTF-8 text") from exc
    capture_id = str(uuid5(NAMESPACE_URL, "sibyl-eval:" + verified.admission_id))
    now = datetime.now(UTC)
    memory = RawMemory(
        id=capture_id,
        organization_id=organization_id,
        principal_id=assignment.owner_principal_id,
        source_id="eval:" + verified.admission_id,
        title=f"Learning task {assignment.task_id}",
        raw_content=text,
        capture_surface="verified_eval",
        created_at=now,
        captured_at=now,
        metadata={
            EVAL_ADMISSION_METADATA_KEY: {
                "admission_id": verified.admission_id,
                "assignment_sha256": assignment_digest(assignment),
                "receipt_sha256": verified.receipt_sha256,
            }
        },
    )
    async with content_client.surreal_content_client() as client:
        try:
            rows = await _transaction(
                client,
                _ADMIT,
                uuid=_attempt_key(organization_id, experiment_id, attempt_id),
                organization_id=organization_id,
                assignment_sha256=assignment_digest(assignment),
                capture_id=capture_id,
                capture=raw_memory_record(memory),
                principal_id=principal_id,
                receipt_sha256=verified.receipt_sha256,
                receipt_base64=base64.b64encode(receipt_bytes).decode(),
                outcome_sha256=verified.outcome.outcome_sha256,
                transcript_sha256=verified.outcome.transcript_sha256,
                episode_sha256=verified.outcome.episode_sha256,
            )
        except Exception as exc:
            _raise_conflict(exc)
    if len(rows) != 1:
        raise EvalAdmissionConflict("admission did not return its stored capture")
    return EvalAdmissionResult(
        raw_memory_from_record(rows[0]), assignment, verified.receipt_sha256, verified.admission_id
    )
