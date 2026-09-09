"""Durable, private review candidates from authenticated consolidation results."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sibyl_core.ai.llm.config import LLMSurface, resolve_llm_config
from sibyl_core.ai.llm.extractor import extraction_schema
from sibyl_core.ai.transport import transport_policy
from sibyl_core.auth.memory_policy import (
    EVAL_ADMISSION_METADATA_KEY,
    EVAL_CONSOLIDATION_METADATA_KEY,
)
from sibyl_core.config import core_config
from sibyl_core.memory_pipeline.source_lifecycle import (
    SOURCE_BINDINGS_KEY,
    SOURCE_VALIDATION_PENDING_KEY,
)
from sibyl_core.services import content_client, content_models
from sibyl_core.services.content_raw_persistence import (
    _raw_memory_from_write,
    reflection_candidate_metadata,
)
from sibyl_core.services.memory_source_validation import (
    SOURCE_VALIDATION_CONTEXT_KEY,
    SourceReadAuthority,
)
from sibyl_core.tasks.consolidation import (
    EVIDENCE_SYSTEM_PROMPT,
    OUTPUT_RETRIES,
    SCHEMA_VERSION,
    SYSTEM_PROMPT,
    AdmittedTaskOutcome,
    ConsolidationResult,
    validate_candidate_content_agreement,
)
from sibyl_core.tasks.episode_evidence import PROJECTION_VERSION
from sibyl_core.tasks.eval_receipts import TaskAssignment, assignment_digest
from sibyl_core.tasks.procedure_evidence import EVIDENCE_PROPOSAL_VERSION, EvidenceProposal

CONSOLIDATION_METADATA_KEY = EVAL_CONSOLIDATION_METADATA_KEY


class ConsolidationConflict(ValueError):
    """An operation identity was reused or its original source observation changed."""


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ConsolidationOperation:
    organization_id: str
    principal_id: str
    experiment_id: str
    experiment_revision: str
    arm_id: str
    checkpoint: int
    group_id: str
    attempt_ids: tuple[str, ...]
    mechanism: str
    controller_policy_sha256: str
    extractor_revision: str

    def __post_init__(self) -> None:
        if (
            any(
                not value.strip()
                for value in (
                    self.organization_id,
                    self.principal_id,
                    self.experiment_id,
                    self.experiment_revision,
                    self.arm_id,
                    self.group_id,
                    self.mechanism,
                    self.controller_policy_sha256,
                    self.extractor_revision,
                )
            )
            or self.checkpoint < 0
        ):
            raise ValueError("explicit consolidation identity and policy are required")
        if len(self.attempt_ids) < 2 or len(set(self.attempt_ids)) != len(self.attempt_ids):
            raise ValueError("distinct consolidation attempts are required")

    @property
    def key(self) -> str:
        return _digest(
            [
                "sibyl-consolidation-v1",
                self.organization_id,
                self.principal_id,
                self.experiment_id,
                self.experiment_revision,
                self.arm_id,
                self.checkpoint,
                self.group_id,
            ]
        )

    @property
    def request_sha256(self) -> str:
        return _digest(
            [
                self.key,
                sorted(self.attempt_ids),
                self.mechanism,
                self.controller_policy_sha256,
                self.extractor_revision,
            ]
        )


@dataclass(frozen=True)
class StoredConsolidation:
    operation_id: str
    status: Literal["candidate", "abstained", "rejected", "gone"]
    memory: content_models.RawMemory | None
    build_receipt: dict[str, Any] | None = None


# One RETURN statement is an implicit atomic transaction on both SDK transports.
_STORE = """
RETURN {
LET $existing = (SELECT * FROM eval_consolidations
    WHERE uuid = $operation_id AND organization_id = $organization_id LIMIT 1)[0];
IF $existing != NONE {
    IF $existing.principal_id != $principal_id
        OR $existing.request_sha256 != $request_sha256 {
        THROW 'consolidation conflict: immutable request differs';
    };
} ELSE {
    FOR $source IN $sources {
        LET $attempt = (SELECT * FROM eval_attempts WHERE organization_id = $organization_id
            AND experiment_id = $experiment_id AND attempt_id = $source.attempt_id LIMIT 1)[0];
        LET $memory = (SELECT * FROM raw_captures WHERE organization_id = $organization_id
            AND uuid = $source.capture_id LIMIT 1)[0];
        IF $attempt = NONE OR $attempt.capture_id != $source.capture_id
            OR $attempt.receipt_sha256 != $source.receipt_sha256
            OR $attempt.episode_sha256 != $source.episode_sha256
            OR $attempt.assignment_sha256 != $source.assignment_sha256
            OR $attempt.assignment_json != $source.assignment_json
            OR $attempt.receipt_base64 != $source.receipt_base64
            OR $attempt.outcome_sha256 != $source.outcome_sha256
            OR $attempt.transcript_sha256 != $source.transcript_sha256
            OR $attempt.admitted_at = NONE
            OR $memory = NONE OR $memory.deleted_at != NONE
            OR $memory.principal_id != $principal_id OR $memory.memory_scope != 'private'
            OR $memory.metadata != $source.metadata
            OR $memory.review_state != $source.review_state
            OR $memory.source_id != $source.source_id
            OR $memory.revision != $source.revision
            OR crypto::sha256($memory.raw_content) != $source.episode_sha256
            OR $memory.metadata.eval_admission != $source.admission_stamp {
            THROW 'consolidation conflict: original admitted source changed';
        };
    };
    IF $candidate != NONE { CREATE raw_captures CONTENT $candidate; };
    CREATE eval_consolidations CONTENT $ledger;
};
LET $stored = (SELECT * FROM eval_consolidations
    WHERE uuid = $operation_id AND organization_id = $organization_id LIMIT 1)[0];
LET $memory = (SELECT * FROM raw_captures WHERE organization_id = $organization_id
    AND uuid = $stored.candidate_id LIMIT 1)[0];
RETURN { ledger: $stored, memory: $memory };
};
"""


def _decode_build_receipt(ledger: dict) -> dict[str, Any] | None:
    encoded_receipt = ledger.get("build_receipt_json")
    try:
        receipt = json.loads(encoded_receipt) if encoded_receipt is not None else None
        if encoded_receipt is not None:
            allowed_statuses = (
                {"abstained", "rejected"} if ledger["result_kind"] == "abstained" else {"proposed"}
            )
            if (
                not isinstance(receipt, dict)
                or receipt.get("schema_version") != SCHEMA_VERSION
                or receipt.get("status") not in allowed_statuses
                or not isinstance(receipt.get("usage"), dict)
                or (
                    ledger["result_kind"] == "abstained"
                    and (
                        not isinstance(receipt.get("reason"), str) or not receipt["reason"].strip()
                    )
                )
                or json.dumps(
                    receipt,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                != encoded_receipt
            ):
                raise ValueError("receipt has an invalid schema or outcome")
    except (TypeError, ValueError) as exc:
        raise ConsolidationConflict("stored consolidation receipt is invalid") from exc
    return receipt


def _decode(operation: ConsolidationOperation, row: dict) -> StoredConsolidation:
    ledger = row["ledger"]
    if (
        ledger["principal_id"] != operation.principal_id
        or ledger["request_sha256"] != operation.request_sha256
    ):
        raise ConsolidationConflict("immutable consolidation request differs")
    receipt = _decode_build_receipt(ledger)
    if ledger["result_kind"] == "abstained":
        status: Literal["abstained", "rejected"] = (
            "rejected" if receipt and receipt.get("status") == "rejected" else "abstained"
        )
        return StoredConsolidation(operation.key, status, None, receipt)
    record = row.get("memory")
    memory = content_models.raw_memory_from_record(record) if record else None
    if memory is None or memory.deleted_at is not None:
        return StoredConsolidation(operation.key, "gone", None, receipt)
    if (
        memory.principal_id != operation.principal_id
        or memory.memory_scope != "private"
        or memory.metadata.get(CONSOLIDATION_METADATA_KEY) != operation.key
    ):
        raise ConsolidationConflict("stored consolidation candidate was replaced")
    return StoredConsolidation(operation.key, "candidate", memory, receipt)


async def get_stored_consolidation(operation: ConsolidationOperation) -> StoredConsolidation | None:
    """Resolve replay before extraction; a purged candidate remains permanently gone."""
    async with content_client.surreal_content_client() as client:
        rows = content_client.normalize_records(
            await client.execute_query(
                """
RETURN {
LET $ledger = (SELECT * FROM eval_consolidations
    WHERE uuid = $operation_id AND organization_id = $organization_id LIMIT 1)[0];
RETURN { ledger: $ledger, memory: (SELECT * FROM raw_captures
    WHERE organization_id = $organization_id AND uuid = $ledger.candidate_id LIMIT 1)[0] };
};
""",
                operation_id=operation.key,
                organization_id=operation.organization_id,
            )
        )
    if len(rows) != 1:
        raise ConsolidationConflict("consolidation lookup returned no snapshot")
    return _decode(operation, rows[0]) if rows[0].get("ledger") else None


async def store_consolidation(
    operation: ConsolidationOperation,
    result: ConsolidationResult,
) -> StoredConsolidation:
    """Persist a server-verified proposal under its original source observations.

    The caller authenticates and revalidates the admitted group before invoking
    this writer. The transaction closes the last read-to-write revision gap.
    """
    existing = await get_stored_consolidation(operation)
    if existing is not None:
        return existing
    build_receipt = deepcopy(result.receipt)
    status = build_receipt.get("status")
    if (
        (result.candidate is not None and status != "proposed")
        or (result.candidate is None and status not in {"abstained", "rejected"})
        or (
            status == "abstained"
            and build_receipt.get("reason") != result.proposal.abstention_reason
        )
        or (status in {"abstained", "rejected"} and not build_receipt.get("reason"))
    ):
        raise ConsolidationConflict("consolidation receipt disagrees with its outcome")
    result_kind = "candidate" if result.candidate is not None else "abstained"
    encoded_receipt = json.dumps(
        build_receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    _decode_build_receipt({"result_kind": result_kind, "build_receipt_json": encoded_receipt})
    group = result.group
    if result.candidate is not None and validate_candidate_content_agreement(
        result.candidate, group=group
    ):
        raise ConsolidationConflict("candidate no longer agrees with its evidence")
    if (
        group.organization_id != operation.organization_id
        or group.owner_principal_id != operation.principal_id
        or group.group_id != operation.group_id
        or group.mechanism != operation.mechanism
        or {e.session_id for e in group.episodes} != set(operation.attempt_ids)
    ):
        raise ConsolidationConflict("proposal is outside the operation cohort")
    sources: list[dict[str, Any]] = []
    bindings = {}
    for episode in group.episodes:
        if not isinstance(episode.outcome, AdmittedTaskOutcome) or len(episode.stored_sources) != 1:
            raise ConsolidationConflict("only joined admitted episodes may be persisted")
        source = episode.stored_sources[0]
        bindings[source.source_id] = source.observed_revision
        sources.append(
            {
                "attempt_id": episode.session_id,
                "capture_id": source.source_id,
                "revision": source.observed_revision,
                "receipt_sha256": episode.outcome.receipt_sha256,
                "episode_sha256": episode.artifact_sha256,
                "admission_id": episode.outcome.admission_id,
                "assignment_sha256": episode.outcome.assignment_sha256,
                "outcome_sha256": episode.outcome.outcome_sha256,
                "transcript_sha256": episode.outcome.transcript_sha256,
                "admission_stamp": {
                    "admission_id": episode.outcome.admission_id,
                    "assignment_sha256": episode.outcome.assignment_sha256,
                    "receipt_sha256": episode.outcome.receipt_sha256,
                },
            }
        )
    # Preserve extraction observations, while binding every current lifecycle
    # field checked here into the final transaction. Direct authorized record
    # writes need not have used the normal revision-incrementing helpers.
    async with content_client.surreal_content_client() as client:
        records = await content_client.select_many(
            client,
            "SELECT * FROM raw_captures WHERE organization_id = $organization_id AND uuid IN $ids;",
            organization_id=operation.organization_id,
            ids=list(bindings),
        )
        attempts = await content_client.select_many(
            client,
            "SELECT * FROM eval_attempts WHERE organization_id = $organization_id "
            "AND experiment_id = $experiment_id AND attempt_id IN $attempt_ids;",
            organization_id=operation.organization_id,
            experiment_id=operation.experiment_id,
            attempt_ids=list(operation.attempt_ids),
        )
    admitted = {str(row["attempt_id"]): row for row in attempts}
    current = {str(record["uuid"]): record for record in records}
    for source in sources:
        record = current.get(source["capture_id"])
        if record is None:
            raise ConsolidationConflict("original admitted source changed")
        ledger = admitted.get(source["attempt_id"])
        if ledger is None:
            raise ConsolidationConflict("original admitted ledger changed")
        try:
            assignment_json = ledger["assignment_json"]
            receipt_base64 = ledger["receipt_base64"]
            if not isinstance(assignment_json, str) or not isinstance(receipt_base64, str):
                raise ValueError("stored admission artifacts must be strings")
            assignment = TaskAssignment.model_validate_json(assignment_json)
            receipt = base64.b64decode(receipt_base64, validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ConsolidationConflict("original admitted ledger changed") from exc
        if (
            assignment_digest(assignment) != source["assignment_sha256"]
            or hashlib.sha256(receipt).hexdigest() != source["receipt_sha256"]
            or any(
                ledger.get(key) != source[key]
                for key in (
                    "assignment_sha256",
                    "receipt_sha256",
                    "outcome_sha256",
                    "transcript_sha256",
                    "episode_sha256",
                    "capture_id",
                    "attempt_id",
                )
            )
            or ledger.get("admitted_at") is None
            or assignment.owner_principal_id != operation.principal_id
            or assignment.organization_id != operation.organization_id
            or assignment.experiment_id != operation.experiment_id
            or assignment.experiment_revision != operation.experiment_revision
            or assignment.arm_id != operation.arm_id
            or assignment.checkpoint > operation.checkpoint
            or assignment.controller_policy_sha256 != operation.controller_policy_sha256
            or assignment.split != "learning"
        ):
            raise ConsolidationConflict("original admitted ledger changed")
        source.update(
            assignment_json=ledger["assignment_json"], receipt_base64=ledger["receipt_base64"]
        )
        memory = content_models.raw_memory_from_record(record)
        if memory.metadata.get(EVAL_ADMISSION_METADATA_KEY) != source["admission_stamp"]:
            raise ConsolidationConflict("original admission stamp changed")
        if memory.revision != source["revision"] or not content_models.raw_memory_recallable(
            memory
        ):
            raise ConsolidationConflict("original admitted source changed")
        source.update(
            metadata=record.get("metadata", {}),
            review_state=record.get("review_state"),
            source_id=record.get("source_id"),
        )
    candidate = None
    candidate_id = None
    if result.candidate is not None:
        candidate_id = str(uuid5(NAMESPACE_URL, "sibyl-consolidation:" + operation.key))
        proposal = result.candidate
        metadata = reflection_candidate_metadata(
            candidate=proposal, raw_source_ids=tuple(bindings), memory_scope="private"
        )
        memory = _raw_memory_from_write(
            content_models.RawMemoryWrite(
                organization_id=operation.organization_id,
                principal_id=operation.principal_id,
                source_id="consolidation:" + operation.key,
                raw_content=proposal.content,
                title=proposal.title,
                memory_scope="private",
                tags=proposal.tags,
                metadata=metadata,
                capture_surface="reflection_candidate",
                entity_type=proposal.kind,
            ),
            captured_at=content_models.utcnow(),
        )
        memory.metadata.update(
            {
                CONSOLIDATION_METADATA_KEY: operation.key,
                SOURCE_BINDINGS_KEY: bindings,
                SOURCE_VALIDATION_PENDING_KEY: True,
                SOURCE_VALIDATION_CONTEXT_KEY: SourceReadAuthority(
                    operation.principal_id
                ).ceiling_metadata(),
            }
        )
        candidate = content_models.raw_memory_record(replace(memory, id=candidate_id))
    ledger = {
        "uuid": operation.key,
        "organization_id": operation.organization_id,
        "principal_id": operation.principal_id,
        "request_sha256": operation.request_sha256,
        "admission_bindings": [
            {
                **{
                    key: source[key]
                    for key in (
                        "attempt_id",
                        "capture_id",
                        "revision",
                        "receipt_sha256",
                        "episode_sha256",
                        "assignment_sha256",
                        "outcome_sha256",
                        "transcript_sha256",
                        "admission_stamp",
                    )
                },
                "experiment_id": operation.experiment_id,
                "assignment_artifact_sha256": hashlib.sha256(
                    source["assignment_json"].encode()
                ).hexdigest(),
                "receipt_artifact_sha256": hashlib.sha256(
                    source["receipt_base64"].encode()
                ).hexdigest(),
            }
            for source in sources
        ],
        "build_receipt_json": encoded_receipt,
        "candidate_id": candidate_id,
        "result_kind": result_kind,
    }
    async with content_client.surreal_content_client() as client:
        try:
            rows = content_client.normalize_records(
                await client.execute_query(
                    _STORE,
                    operation_id=operation.key,
                    organization_id=operation.organization_id,
                    principal_id=operation.principal_id,
                    experiment_id=operation.experiment_id,
                    request_sha256=operation.request_sha256,
                    sources=sources,
                    candidate=candidate,
                    ledger=ledger,
                )
            )
        except Exception as exc:
            if "consolidation conflict:" in str(exc):
                raise ConsolidationConflict(str(exc)) from exc
            raise
    if len(rows) != 1:
        raise ConsolidationConflict("consolidation did not return its stored result")
    return _decode(operation, rows[0])


@dataclass(frozen=True)
class _ExtractorPolicy:
    model: str
    revision: str
    max_input_chars: int
    max_output_tokens: int
    output_mode: Literal["tool", "native_strict"]
    openrouter_provider: str | None


async def _extractor_policy() -> _ExtractorPolicy:
    """Resolve one extraction policy snapshot, including the actual build limits."""
    config = await resolve_llm_config(LLMSurface.MEMORY)
    max_input_chars = core_config.consolidation_max_input_chars
    max_output_tokens = config.max_tokens.value
    if max_output_tokens is None:
        max_output_tokens = 2_048
    if any(type(value) is not int or value <= 0 for value in (max_input_chars, max_output_tokens)):
        raise ValueError("consolidation build limits must be positive integers")
    revision = _digest(
        {
            "protocol": SCHEMA_VERSION,
            "evidence_projection": PROJECTION_VERSION,
            "evidence_validation": EVIDENCE_PROPOSAL_VERSION,
            "projection_system_sha256": hashlib.sha256(EVIDENCE_SYSTEM_PROMPT.encode()).hexdigest(),
            "projection_schema_sha256": _digest(EvidenceProposal.model_json_schema()),
            "system_prompt": SYSTEM_PROMPT,
            "provider": config.provider.value,
            "model": config.model.value,
            "temperature": config.temperature.value,
            "max_input_chars": max_input_chars,
            "max_output_tokens": max_output_tokens,
            "output_retries": OUTPUT_RETRIES,
            "output_mode": core_config.consolidation_output_mode,
            "wire_schema_sha256": _digest(
                extraction_schema(EvidenceProposal, core_config.consolidation_output_mode)
            ),
            "openrouter_provider": core_config.consolidation_openrouter_provider,
            "input_budget_unit": "system_user_declared_schema_characters",
            "transport": transport_policy(config.to_llm_config()),
        }
    )
    return _ExtractorPolicy(
        config.model.value,
        revision,
        max_input_chars,
        max_output_tokens,
        core_config.consolidation_output_mode,
        core_config.consolidation_openrouter_provider,
    )


async def consolidation_extractor_configuration() -> tuple[str, str]:
    """Identify the effective extraction policy without including credentials."""
    policy = await _extractor_policy()
    return policy.model, policy.revision


async def consolidate_admitted_procedure(
    operation: ConsolidationOperation,
    *,
    trusted_issuer_id: str,
    trusted_public_key: Ed25519PublicKey,
    model_override: str,
) -> StoredConsolidation:
    """Resolve a durable result or build once from the authorized admitted cohort."""
    from sibyl_core.services.eval_consolidation import propose_admitted_procedure

    stored = await get_stored_consolidation(operation)
    if stored is not None:
        return stored
    policy = await _extractor_policy()
    if (policy.model, policy.revision) != (model_override, operation.extractor_revision):
        raise ConsolidationConflict("extraction configuration changed")
    result = await propose_admitted_procedure(
        organization_id=operation.organization_id,
        principal_id=operation.principal_id,
        experiment_id=operation.experiment_id,
        experiment_revision=operation.experiment_revision,
        arm_id=operation.arm_id,
        through_checkpoint=operation.checkpoint,
        attempt_ids=operation.attempt_ids,
        group_id=operation.group_id,
        mechanism=operation.mechanism,
        trusted_issuer_id=trusted_issuer_id,
        trusted_public_key=trusted_public_key,
        expected_controller_policy_sha256=operation.controller_policy_sha256,
        model_override=model_override,
        max_input_chars=policy.max_input_chars,
        max_tokens=policy.max_output_tokens,
        output_mode=policy.output_mode,
        openrouter_provider=policy.openrouter_provider,
    )
    if await _extractor_policy() != policy:
        raise ConsolidationConflict("extraction configuration changed during consolidation")
    return await store_consolidation(operation, result.proposal)
