"""Immutable store-local evidence for future additive archive writers.

These contracts do not authorize an import or equate checked preview decisions
with committed mutations. Native row fingerprints use Surreal's explicit value
encoding and are distinct from the checked plan's Python semantic digest.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from sibyl_core.migrate.personal_archive_plan import (
    ArchiveCredentialCeiling,
    ArchiveKind,
    archive_digest,
    canonical_json,
)

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, StringConstraints(min_length=1)]
Counter = Annotated[int, Field(ge=0)]
Store = Literal["content", "graph"]
Action = Literal["apply", "rollback"]
CanonicalKind = Literal["raw_capture", "graph_entity", "graph_relationship"]


class ArchivePhaseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )


class ArchivePhaseCredential(ArchiveCredentialCeiling):
    model_config = ArchivePhaseModel.model_config


class ArchiveRunBinding(ArchivePhaseModel):
    contract_version: Literal[1] = 1
    organization_id: str
    actor_id: str
    run_id: str
    artifact_id: str
    archive_sha256: Digest
    artifact_sha256: Digest
    mappings_sha256: Digest
    checked_plan_sha256: Digest
    credential: ArchivePhaseCredential

    @field_validator("contract_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("archive phase version must be an integer")
        return value

    @field_validator("organization_id", "actor_id", "run_id", "artifact_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("archive binding identities must be canonical UUIDs")
        return value

    @property
    def sha256(self) -> str:
        return archive_digest("sibyl-archive-run-binding-v1", self)


class ArchivePhaseKey(ArchivePhaseModel):
    binding: ArchiveRunBinding
    store: Store
    action: Action
    batch_sequence: Counter

    @property
    def phase(self) -> str:
        return f"{self.store}_{self.action}"


class ArchivePhaseControl(ArchivePhaseModel):
    binding: ArchiveRunBinding
    store: Store
    revision: Counter
    token: str
    state: Literal["open", "rolling_back", "rolled_back"]

    _token_uuid = field_validator("token")(ArchiveRunBinding.canonical_uuid.__func__)


class ArchiveCreatedIdentity(ArchivePhaseModel):
    kind: CanonicalKind
    destination_id: Identity

    _destination_uuid = field_validator("destination_id")(ArchiveRunBinding.canonical_uuid.__func__)


class IntroducedArchiveRow(ArchivePhaseModel):
    kind: CanonicalKind
    destination_id: Identity
    physical_id: Identity
    row_sha256: Digest
    body_sha256: Digest
    audience_sha256: Digest
    revision: Annotated[int, Field(ge=1)] | None
    source_incarnation: Identity | None
    source_generation: Annotated[int, Field(ge=1)] | None
    source_state_revision: Counter | None
    endpoint_ids: tuple[Identity, ...] = ()
    endpoint_state_sha256: tuple[Digest, ...] = ()
    binding_sha256: Digest | None = None

    _destination_uuid = field_validator("destination_id")(ArchiveRunBinding.canonical_uuid.__func__)

    @model_validator(mode="after")
    def evidence_shape(self) -> Self:
        table = {
            "raw_capture": "raw_captures",
            "graph_entity": "entity",
            "graph_relationship": "relates_to",
        }[self.kind]
        if not self.physical_id.startswith(table + ":"):
            raise ValueError("introduced physical identity does not match its canonical kind")
        if self.kind == "graph_relationship":
            if any(
                value is not None
                for value in (
                    self.revision,
                    self.source_incarnation,
                    self.source_generation,
                    self.source_state_revision,
                )
            ):
                raise ValueError("relationships cannot invent revisions or source states")
            if (
                len(self.endpoint_ids) != 2
                or len(self.endpoint_state_sha256) != 2
                or self.binding_sha256 is None
            ):
                raise ValueError("relationships require both actual endpoint and binding witnesses")
        elif any(
            value is None
            for value in (
                self.revision,
                self.source_incarnation,
                self.source_generation,
                self.source_state_revision,
            )
        ):
            raise ValueError("introduced canonical sources require actual source-state evidence")
        elif self.source_state_revision != self.revision:
            raise ValueError("introduced source-state revision must match its actual row")
        elif self.endpoint_ids or self.endpoint_state_sha256 or self.binding_sha256 is not None:
            raise ValueError("canonical sources cannot claim relationship evidence")
        return self


class ArchiveRetirementEvidence(ArchivePhaseModel):
    introduced: IntroducedArchiveRow
    absent: bool
    row_sha256: Digest | None
    source_incarnation: Identity | None
    source_generation: Annotated[int, Field(ge=1)] | None
    source_state_sha256: Digest | None

    @model_validator(mode="after")
    def retained_history(self) -> Self:
        if self.absent != (self.row_sha256 is None):
            raise ValueError("retirement row fingerprint must match its actual absence")
        if self.introduced.kind == "graph_relationship":
            if not self.absent or any(
                value is not None
                for value in (
                    self.row_sha256,
                    self.source_incarnation,
                    self.source_generation,
                    self.source_state_sha256,
                )
            ):
                raise ValueError(
                    "relationship retirement retains absence in the receipt, not an invented source state"
                )
        elif (
            self.source_incarnation != self.introduced.source_incarnation
            or self.source_generation is None
            or self.source_generation <= (self.introduced.source_generation or 0)
            or self.source_state_sha256 is None
        ):
            raise ValueError(
                "canonical retirement must retain the incarnation and advance its high-water"
            )
        return self


class ArchivePhaseCounts(ArchivePhaseModel):
    kind: Identity

    @field_validator("kind")
    @classmethod
    def supported_kind(cls, value: str) -> str:
        ArchiveKind(value)
        return value

    created: Counter = 0
    skipped: Counter = 0
    conflicted: Counter = 0
    quarantined: Counter = 0
    retired: Counter = 0
    preserved: Counter = 0


class ArchivePhaseReceipt(ArchivePhaseModel):
    key: ArchivePhaseKey
    token: str
    previous_token: str
    previous_revision: Counter
    committed_revision: Annotated[int, Field(ge=1)]
    counts: tuple[ArchivePhaseCounts, ...]
    introduced: tuple[IntroducedArchiveRow, ...] = ()
    retired: tuple[ArchiveRetirementEvidence, ...] = ()
    terminal: bool = False

    _token_uuid = field_validator("token", "previous_token")(
        ArchiveRunBinding.canonical_uuid.__func__
    )

    @model_validator(mode="after")
    def actual_counts(self) -> Self:
        if self.committed_revision != self.previous_revision + 1:
            raise ValueError("phase receipt must advance exactly one control revision")
        kinds = [count.kind for count in self.counts]
        if len(set(kinds)) != len(kinds):
            raise ValueError("phase counts require one entry per kind")
        identities = [(row.kind, row.destination_id) for row in self.introduced]
        retired_identities = [
            (row.introduced.kind, row.introduced.destination_id) for row in self.retired
        ]
        if len(set(identities)) != len(identities) or len(set(retired_identities)) != len(
            retired_identities
        ):
            raise ValueError("phase receipts cannot repeat introduced or retired identities")
        for count in self.counts:
            if count.created != sum(
                row.kind == count.kind for row in self.introduced
            ) or count.retired != sum(row.introduced.kind == count.kind for row in self.retired):
                raise ValueError("phase counts do not reconcile with actual evidence")
        if not (
            set(row.kind for row in self.introduced)
            | set(row.introduced.kind for row in self.retired)
        ).issubset(kinds):
            raise ValueError("phase counts omit actual evidence")
        if self.key.action == "apply":
            if (
                self.token != self.previous_token
                or self.terminal
                or self.retired
                or any(count.retired or count.preserved for count in self.counts)
            ):
                raise ValueError("apply receipts cannot claim rollback completion")
        elif self.introduced or any(
            count.created or count.skipped or count.quarantined for count in self.counts
        ):
            raise ValueError("rollback receipts cannot claim new imports")
        if any(
            self.key.store != ("content" if row.kind == "raw_capture" else "graph")
            for row in self.introduced
        ) or any(
            self.key.store != ("content" if row.introduced.kind == "raw_capture" else "graph")
            for row in self.retired
        ):
            raise ValueError("introduced evidence belongs to another store")
        return self


def strict_phase_json(value: object) -> str:
    """Snapshot explicit JSON, rejecting non-string keys and implicit objects."""

    def validate(item: object) -> None:
        if item is None or type(item) in (str, int, float, bool):
            return
        if isinstance(item, list | tuple):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict) and all(type(key) is str for key in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("phase parameters require explicit JSON values")

    validate(value)
    return canonical_json(value)


def phase_binding_json(binding: ArchiveRunBinding) -> str:
    validated = ArchiveRunBinding.model_validate(binding.model_dump(mode="python"))
    return strict_phase_json(validated.model_dump(mode="json"))


def phase_receipt_json(receipt: ArchivePhaseReceipt) -> str:
    validated = ArchivePhaseReceipt.model_validate(receipt.model_dump(mode="python"))
    return strict_phase_json(validated.model_dump(mode="json"))


def verify_phase_receipt(encoded: str) -> ArchivePhaseReceipt:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate phase receipt JSON key")
            result[key] = value
        return result

    json.loads(
        encoded,
        object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError("nonfinite phase receipt JSON")
        ),
    )
    receipt = ArchivePhaseReceipt.model_validate_json(encoded)
    if phase_receipt_json(receipt) != encoded:
        raise ValueError("phase receipt JSON must be canonical")
    return receipt
