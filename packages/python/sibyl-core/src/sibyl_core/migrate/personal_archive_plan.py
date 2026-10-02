"""Immutable contracts for checked, additive personal archive imports."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from enum import StrEnum
from typing import Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

type ArchiveWitnessScheme = Literal["native-full-v1", "graph-association-authority-v2"]

CURRENT_ARCHIVE_WITNESS_SCHEME: Literal["graph-association-authority-v2"] = (
    "graph-association-authority-v2"
)


class ArchiveContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArchiveDisposition(StrEnum):
    CREATED = "created"
    SKIPPED = "skipped"
    CONFLICTED = "conflicted"
    QUARANTINED = "quarantined"


class ArchiveKind(StrEnum):
    RAW_CAPTURE = "raw_capture"
    GRAPH_ENTITY = "graph_entity"
    GRAPH_RELATIONSHIP = "graph_relationship"
    GRAPH_EPISODE = "graph_episode"
    GRAPH_MENTION = "graph_mention"
    CRAWL_SOURCE = "crawl_source"
    CRAWLED_DOCUMENT = "crawled_document"
    DOCUMENT_CHUNK = "document_chunk"
    DERIVED_FROM = "content_derived_from"
    CHUNK_OF = "content_chunk_of"
    SUPERSEDES = "content_supersedes"
    EXTRACTED_INTO = "content_extracted_into"
    SOURCE_STATE = "source_state"
    SOURCE_ASSOCIATION = "source_association"
    EVAL_ATTEMPT = "eval_attempt"
    EVAL_CONSOLIDATION = "eval_consolidation"
    IDEMPOTENCY_RECORD = "api_idempotency_record"
    SOURCE_IMPORT = "source_import"
    CHANGEFEED_CURSOR = "content_changefeed_cursor"
    DREAM_CHECKPOINT = "dream_source_checkpoint"
    DREAM_CURSOR = "dream_source_cursor"
    VALIDATION_EXECUTION = "memory_validation_execution"
    VALIDATION_ATTEMPT = "memory_validation_attempt"
    VALIDATION_RECEIPT = "validation_receipt_payload"
    SYSTEM_SETTING = "system_setting"
    TELEMETRY_ROLLUP = "telemetry_rollup"
    BACKUP_SETTING = "backup_setting"
    BACKUP_RECORD = "backup_record"


class ArchiveSourceOrigin(ArchiveContractModel):
    """Declared foreign labels provide consistency, never source authority."""

    organization_id: str = Field(min_length=1)
    source_store: str = Field(min_length=1)

    @field_validator("organization_id")
    @classmethod
    def canonical_organization(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("archive organization must be a canonical UUID")
        return value


class ArchiveAudience(ArchiveContractModel):
    memory_scope: Literal["private", "project", "team"]
    scope_key: str = Field(min_length=1)


class ArchiveMappings(ArchiveContractModel):
    source_private_owner_id: str = Field(min_length=1)
    projects: dict[str, str] = Field(default_factory=dict)
    teams: dict[str, str] = Field(default_factory=dict)
    quarantine: ArchiveAudience

    @field_validator("projects", "teams")
    @classmethod
    def nonempty_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not source.strip() or not target.strip() for source, target in value.items()):
            raise ValueError("archive mapping identities must be nonempty")
        return value


class ArchiveCredentialCeiling(ArchiveContractModel):
    credential_kind: Literal["session", "api_key"]
    api_key_id: str | None = None
    rest_scopes: tuple[str, ...] = ()
    project_restricted: bool = False
    project_ids: tuple[str, ...] = ()
    memory_restricted: bool = False
    memory_space_ids: tuple[str, ...] = ()
    memory_scope_keys: tuple[str, ...] = ()

    @field_validator("rest_scopes", "project_ids", "memory_space_ids", "memory_scope_keys")
    @classmethod
    def canonical_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("archive ceiling values must be unique and nonempty")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def credential_identity(self) -> Self:
        if self.credential_kind == "api_key":
            if self.api_key_id is None or str(UUID(self.api_key_id)) != self.api_key_id:
                raise ValueError("a trusted canonical intake key identity is required")
        elif self.api_key_id is not None:
            raise ValueError("a session ceiling cannot claim a key identity")
        if not self.project_restricted and self.project_ids:
            raise ValueError("project IDs require an explicit restricted ceiling")
        if not self.memory_restricted and (self.memory_space_ids or self.memory_scope_keys):
            raise ValueError("memory identities require an explicit restricted ceiling")
        return self


class ArchivePreviewCounts(ArchiveContractModel):
    created: int = Field(default=0, ge=0, strict=True)
    skipped: int = Field(default=0, ge=0, strict=True)
    conflicted: int = Field(default=0, ge=0, strict=True)
    quarantined: int = Field(default=0, ge=0, strict=True)
    coalesced: int = Field(default=0, ge=0, strict=True)

    @property
    def normalized_rows(self) -> int:
        return self.created + self.skipped + self.conflicted + self.quarantined


class ArchiveStoreWitness(ArchiveContractModel):
    store: Literal["content", "graph"]
    identity: str = Field(min_length=1)
    row_sha256: str | None = None
    state_sha256: str | None = None
    associations_sha256: str | None = None


class PlannedArchiveRow(ArchiveContractModel):
    kind: ArchiveKind
    original_id: str = Field(min_length=1)
    destination_id: str | None = None
    audience: ArchiveAudience
    disposition: ArchiveDisposition
    reason: str = Field(min_length=1)
    semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection: Literal["ordinary", "protected", "retired", "inert"]
    witnesses: tuple[ArchiveStoreWitness, ...] = ()
    endpoint_ids: tuple[str, ...] = ()
    declarations: int = Field(default=1, ge=1, strict=True)

    @model_validator(mode="after")
    def preserve_protection(self) -> Self:
        if self.protection != "ordinary" and self.disposition == ArchiveDisposition.CREATED:
            raise ValueError("foreign protected, retired or inert rows cannot be created")
        if self.disposition == ArchiveDisposition.CREATED and self.destination_id is None:
            raise ValueError("a canonical create preview requires a destination identity")
        return self


class CheckedArchivePlan(ArchiveContractModel):
    contract_version: Literal[1] = 1
    # Only the new marker is optional. Existing nullable authority fields stay
    # serialized so historical checked bytes and digests remain unchanged.
    witness_scheme: Literal["graph-association-authority-v2"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    organization_id: str
    actor_id: str
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin: ArchiveSourceOrigin
    mappings: ArchiveMappings
    credential: ArchiveCredentialCeiling
    conflict_policy: Literal["additive"] = "additive"
    rows: tuple[PlannedArchiveRow, ...]
    counts: dict[str, ArchivePreviewCounts]

    @property
    def effective_witness_scheme(self) -> ArchiveWitnessScheme:
        """Read guard semantics from the saved plan, never the current default."""
        return self.witness_scheme or "native-full-v1"

    @field_validator("organization_id", "actor_id")
    @classmethod
    def canonical_destination(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("destination identity must be a canonical UUID")
        return value

    @model_validator(mode="after")
    def reconcile_rows(self) -> Self:
        identities = [(row.kind, row.original_id) for row in self.rows]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate logical plan identity")
        expected = preview_counts(self.rows)
        if self.counts != expected:
            raise ValueError("archive preview counts do not reconcile")
        return self


def canonical_json(value: object) -> str:
    """Encode explicit JSON values without lossy default coercions."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def archive_digest(domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + canonical_json(value).encode()
    ).hexdigest()


def checked_plan_bytes(plan: CheckedArchivePlan) -> str:
    payload = plan.model_dump(mode="json")
    payload["rows"] = sorted(payload["rows"], key=lambda row: (row["kind"], row["original_id"]))
    return canonical_json(payload)


def checked_plan_digest(plan: CheckedArchivePlan) -> str:
    return hashlib.sha256(
        b"sibyl-archive-plan-v1\0" + checked_plan_bytes(plan).encode("utf-8")
    ).hexdigest()


def verify_checked_plan(encoded: str, expected_sha256: str) -> CheckedArchivePlan:
    plan = CheckedArchivePlan.model_validate_json(encoded)
    if checked_plan_bytes(plan) != encoded or checked_plan_digest(plan) != expected_sha256:
        raise ValueError("checked archive plan digest mismatch")
    return plan


def destination_identity(
    *,
    organization_id: str,
    actor_id: str,
    origin: ArchiveSourceOrigin,
    kind: ArchiveKind,
    original_id: str,
    audience: ArchiveAudience,
) -> str:
    """Stable under unchanged declared labels, without claiming source authenticity."""
    identity: list[JsonValue] = [
        organization_id,
        actor_id,
        origin.model_dump(mode="json"),
        kind.value,
        original_id,
        audience.model_dump(mode="json"),
    ]
    return str(uuid5(NAMESPACE_URL, "sibyl-personal-import/v1:" + canonical_json(identity)))


def preview_counts(rows: tuple[PlannedArchiveRow, ...]) -> dict[str, ArchivePreviewCounts]:
    by_kind: dict[str, Counter[str]] = {}
    for row in rows:
        counter = by_kind.setdefault(row.kind.value, Counter())
        counter[row.disposition.value] += 1
        counter["coalesced"] += row.declarations - 1
    return {kind: ArchivePreviewCounts(**dict(counts)) for kind, counts in sorted(by_kind.items())}
