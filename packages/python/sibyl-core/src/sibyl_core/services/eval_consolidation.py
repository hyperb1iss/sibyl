"""Join admitted learning evidence to current private sources before extraction."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from sibyl_core.auth.memory_policy import EVAL_ADMISSION_METADATA_KEY
from sibyl_core.memory_pipeline.lifecycle import raw_memory_lifecycle_recallable
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services import content_client
from sibyl_core.services.content_models import raw_memory_from_record
from sibyl_core.tasks.consolidation import (
    AdmittedTaskOutcome,
    ConsolidationEpisode,
    ConsolidationGroup,
    ConsolidationResult,
    StoredSourceRef,
    propose_conditional_procedure,
)
from sibyl_core.tasks.eval_receipts import (
    ReceiptError,
    TaskAssignment,
    admission_identity,
    assignment_digest,
    verify_outcome_receipt,
)

_JOIN = """
RETURN {
    attempts: (SELECT * FROM eval_attempts
        WHERE organization_id = $organization_id AND experiment_id = $experiment_id
        AND attempt_id IN $attempt_ids),
    captures: (SELECT * FROM raw_captures WHERE organization_id = $organization_id
        AND uuid IN (SELECT VALUE capture_id FROM eval_attempts
            WHERE organization_id = $organization_id AND experiment_id = $experiment_id
            AND attempt_id IN $attempt_ids))
};
"""


@dataclass(frozen=True)
class AdmittedConsolidationResult:
    """A pending proposal checked against the store before and after extraction.

    Publication must recheck sources again. This result does not authorize a
    scope change or establish entailment or transfer performance.
    """

    proposal: ConsolidationResult
    source_join: str = "authenticated_admission_ledger"
    source_freshness: str = "checked_before_and_after_extraction"


async def load_admitted_consolidation_group(
    *,
    organization_id: str,
    principal_id: str,
    experiment_id: str,
    experiment_revision: str,
    arm_id: str,
    through_checkpoint: int,
    attempt_ids: tuple[str, ...],
    group_id: str,
    mechanism: str,
    trusted_issuer_id: str,
    trusted_public_key: Ed25519PublicKey,
    expected_controller_policy_sha256: str,
) -> ConsolidationGroup:
    """Resolve only stored episodes; callers authorize the explicit private owner."""
    if not organization_id or not principal_id or not experiment_id:
        raise ReceiptError("explicit organization, owner and experiment are required")
    if len(attempt_ids) < 2 or len(set(attempt_ids)) != len(attempt_ids):
        raise ReceiptError("a contrast requires distinct registered attempts")
    if type(through_checkpoint) is not int or through_checkpoint < 0:
        raise ReceiptError("checkpoint must be a nonnegative integer")
    async with content_client.surreal_content_client() as client:
        snapshot = await client.execute_query(
            _JOIN,
            organization_id=organization_id,
            experiment_id=experiment_id,
            attempt_ids=list(attempt_ids),
        )
    return await asyncio.to_thread(
        _validate_admitted_snapshot,
        snapshot,
        organization_id=organization_id,
        principal_id=principal_id,
        experiment_id=experiment_id,
        experiment_revision=experiment_revision,
        arm_id=arm_id,
        through_checkpoint=through_checkpoint,
        attempt_ids=attempt_ids,
        group_id=group_id,
        mechanism=mechanism,
        trusted_issuer_id=trusted_issuer_id,
        trusted_public_key=trusted_public_key,
        expected_controller_policy_sha256=expected_controller_policy_sha256,
    )


def _validate_admitted_snapshot(
    snapshot: object,
    *,
    organization_id: str,
    principal_id: str,
    experiment_id: str,
    experiment_revision: str,
    arm_id: str,
    through_checkpoint: int,
    attempt_ids: tuple[str, ...],
    group_id: str,
    mechanism: str,
    trusted_issuer_id: str,
    trusted_public_key: Ed25519PublicKey,
    expected_controller_policy_sha256: str,
) -> ConsolidationGroup:
    """Validate an owned database snapshot without occupying the request event loop."""
    rows = content_client.normalize_records(snapshot)
    if len(rows) != 1:
        raise ReceiptError("admission source join did not return one snapshot")
    joined = rows[0]
    attempts = {
        str(row["attempt_id"]): row for row in content_client.normalize_records(joined["attempts"])
    }
    captures = {
        str(row["uuid"]): row for row in content_client.normalize_records(joined["captures"])
    }
    if set(attempts) != set(attempt_ids):
        raise ReceiptError("registered learning attempt is missing")
    episodes = []
    for attempt_id in sorted(attempt_ids):
        row = attempts[attempt_id]
        assignment = TaskAssignment.model_validate_json(str(row["assignment_json"]))
        if (
            assignment.organization_id != organization_id
            or assignment.owner_principal_id != principal_id
            or assignment.experiment_id != experiment_id
            or assignment.experiment_revision != experiment_revision
            or assignment.arm_id != arm_id
            or assignment.checkpoint > through_checkpoint
            or assignment.attempt_id != attempt_id
            or assignment.split != "learning"
            or assignment_digest(assignment) != row["assignment_sha256"]
        ):
            raise ReceiptError("registered attempt is outside the learning cohort")
        try:
            receipt = base64.b64decode(str(row.get("receipt_base64") or ""), validate=True)
        except ValueError as exc:
            raise ReceiptError("stored receipt is malformed") from exc
        outcome = verify_outcome_receipt(
            receipt,
            trusted_public_key=trusted_public_key,
            trusted_issuer_id=trusted_issuer_id,
            expected_assignment=assignment,
            expected_controller_policy_sha256=expected_controller_policy_sha256,
        )
        receipt_digest = hashlib.sha256(receipt).hexdigest()
        identity = admission_identity(assignment)
        capture_id = str(uuid5(NAMESPACE_URL, "sibyl-eval:" + identity))
        if (
            row.get("admitted_at") is None
            or row.get("capture_id") != capture_id
            or row.get("receipt_sha256") != receipt_digest
            or any(
                row.get(f"{kind}_sha256") != getattr(outcome, f"{kind}_sha256")
                for kind in ("outcome", "transcript", "episode")
            )
            or outcome.status not in {"passed", "task_failed"}
        ):
            raise ReceiptError("attempt lacks a matching scored admission")
        record = captures.get(capture_id)
        if record is None:
            raise ReceiptError("admitted source is absent")
        memory = raw_memory_from_record(record)
        artifact = memory.raw_content.encode("utf-8")
        if (
            memory.organization_id != organization_id
            or memory.principal_id != principal_id
            or memory.memory_scope != "private"
            or memory.deleted_at is not None
            or not raw_memory_lifecycle_recallable(memory)
            or hashlib.sha256(artifact).hexdigest() != outcome.episode_sha256
            or memory.metadata.get(EVAL_ADMISSION_METADATA_KEY)
            != {
                "admission_id": identity,
                "assignment_sha256": assignment_digest(assignment),
                "receipt_sha256": receipt_digest,
            }
        ):
            raise ReceiptError("admitted source has changed or is no longer available")
        episodes.append(
            ConsolidationEpisode(
                episode_id=identity,
                session_id=attempt_id,
                family_id=assignment.family_id,
                split="learning",
                artifact=artifact,
                artifact_sha256=outcome.episode_sha256,
                environment={
                    "image": assignment.image,
                    "controller_policy_sha256": assignment.controller_policy_sha256,
                },
                stored_sources=(
                    StoredSourceRef(source_id=memory.id, observed_revision=memory.revision),
                ),
                outcome=AdmittedTaskOutcome(
                    receipt_schema_version="sibyl-signed-eval-outcome-v1",
                    task_id=assignment.task_id,
                    attempt_id=attempt_id,
                    status="passed" if outcome.success else "task_failed",
                    success=outcome.success,
                    snapshot_sha256=outcome.snapshot_sha256,
                    receipt_sha256=receipt_digest,
                    admission_id=identity,
                ),
            )
        )
    try:
        return ConsolidationGroup(
            group_id=group_id,
            mechanism=mechanism,
            organization_id=organization_id,
            owner_principal_id=principal_id,
            memory_scope=MemoryScope.PRIVATE,
            environment_compatibility_keys=("image", "controller_policy_sha256"),
            episodes=tuple(episodes),
        )
    except ValidationError as exc:
        raise ReceiptError("Admitted sources do not form a valid consolidation group") from exc


async def propose_admitted_procedure(
    *,
    organization_id: str,
    principal_id: str,
    experiment_id: str,
    experiment_revision: str,
    arm_id: str,
    through_checkpoint: int,
    attempt_ids: tuple[str, ...],
    group_id: str,
    mechanism: str,
    trusted_issuer_id: str,
    trusted_public_key: Ed25519PublicKey,
    expected_controller_policy_sha256: str,
    max_input_chars: int = 40_000,
    max_tokens: int = 2_048,
    model_override: str | None = None,
) -> AdmittedConsolidationResult:
    """Authorize externally, join stored evidence, extract, then check the sources again."""

    async def load() -> ConsolidationGroup:
        return await load_admitted_consolidation_group(
            organization_id=organization_id,
            principal_id=principal_id,
            experiment_id=experiment_id,
            experiment_revision=experiment_revision,
            arm_id=arm_id,
            through_checkpoint=through_checkpoint,
            attempt_ids=attempt_ids,
            group_id=group_id,
            mechanism=mechanism,
            trusted_issuer_id=trusted_issuer_id,
            trusted_public_key=trusted_public_key,
            expected_controller_policy_sha256=expected_controller_policy_sha256,
        )

    group = await load()
    result = await propose_conditional_procedure(
        group,
        max_input_chars=max_input_chars,
        max_tokens=max_tokens,
        model_override=model_override,
    )
    if await load() != group:
        raise ReceiptError("admitted sources changed during consolidation")
    return AdmittedConsolidationResult(proposal=result)
