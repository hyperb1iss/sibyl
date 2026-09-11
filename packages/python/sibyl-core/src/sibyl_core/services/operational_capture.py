"""Internal replay identity for retained operational sources, never caller metadata."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.experience import OperationalExperience
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services import content_client, content_models

_NAMESPACE = UUID("0403e7da-5b40-40a8-90bf-7281d817ed77")
SURFACE = "operational_experience"


def canonical_experience(experience: OperationalExperience) -> str:
    """Server-normalized retained source bytes, not original HTTP transport bytes."""
    return json.dumps(
        experience.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True)
class OperationalSourceWrite:
    """An authorized service's identity intent; an update requires its read revision."""

    organization_id: str
    source_id: str
    principal_id: str
    project_id: str
    expected_revision: int | None = None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.organization_id, self.source_id, self.principal_id, self.project_id)
        ):
            raise ValueError("Operational source needs explicit identity and project")
        if self.expected_revision is not None and (
            type(self.expected_revision) is not int or self.expected_revision < 1
        ):
            raise ValueError("Operational update requires a positive revision")

    @property
    def id(self) -> str:
        return str(
            uuid5(
                _NAMESPACE,
                json.dumps([self.organization_id, self.source_id], separators=(",", ":")),
            )
        )

    def validate(self, memory: content_models.RawMemory, *, incoming: bool = False) -> None:
        if (
            memory.organization_id != self.organization_id
            or memory.source_id != self.source_id
            or memory.principal_id != self.principal_id
            or memory.project_id != self.project_id
            or memory.memory_scope != MemoryScope.PROJECT
            or memory.scope_key != self.project_id
            or memory.capture_surface != SURFACE
        ):
            raise ValueError("Operational source identity or project differs")
        if incoming and memory.metadata != {"project_id": self.project_id}:
            raise ValueError("Operational source row metadata is owned by the capture service")
        experience = OperationalExperience.model_validate_json(memory.raw_content)
        if experience.source_id != self.source_id or experience.project_id != self.project_id:
            raise ValueError("Operational payload source identity differs")
        if memory.raw_content != canonical_experience(experience):
            raise ValueError("Operational source requires canonical validated JSON")


_SNAPSHOT = """RETURN {
    LET $row = (SELECT * FROM raw_captures WHERE uuid=$uuid LIMIT 1)[0];
    LET $state = (SELECT * FROM source_states WHERE organization_id=$org
        AND source_kind='raw_capture' AND source_id=$uuid LIMIT 1)[0];
    RETURN {row:$row, state:$state, fingerprint:crypto::sha256(type::string([$row,$state]))};
};"""


async def write_operational_source(
    client: SurrealContentClient,
    record: dict[str, Any],
    intent: OperationalSourceWrite,
    *,
    prepare_record: Callable[[], Awaitable[dict[str, object]]],
) -> dict[str, Any]:
    """Fence an inspected source and atomically create, replay or update its row."""
    rows = content_client.normalize_records(
        await client.execute_query(_SNAPSHOT, uuid=intent.id, org=intent.organization_id)
    )
    if len(rows) != 1:
        raise ValueError("Operational source snapshot is not unique")
    snapshot = rows[0]
    old, state = snapshot.get("row"), snapshot.get("state")
    if old is None:
        if state is not None:
            raise ValueError("Operational source is retired or missing its retained row")
        if intent.expected_revision is not None:
            raise RevisionConflictError(intent.id, intent.expected_revision, 0)
        mode = "create"
    else:
        if not isinstance(old, dict):
            raise ValueError("Operational source row has invalid shape")
        existing = content_models.raw_memory_from_record(old)
        intent.validate(existing)
        if not content_models.raw_memory_currently_recallable(existing) or (
            not isinstance(state, dict)
            or state.get("deleted") is not False
            or state.get("revision") != existing.revision
        ):
            raise ValueError("Operational source is retired or unavailable")
        if intent.expected_revision is not None and intent.expected_revision != existing.revision:
            raise RevisionConflictError(intent.id, intent.expected_revision, existing.revision)
        # Retrieval counters and lifecycle are not reset by a capture replay.
        keys = ("raw_content", "title", "entity_type", "tags", "provenance")
        same = all(old.get(key) == record.get(key) for key in keys)
        if same:
            mode = "replay"
        elif intent.expected_revision is None:
            raise RevisionConflictError(intent.id, 0, existing.revision)
        else:
            mode = "update"
    if mode != "replay":
        record = await prepare_record()
    saved = content_client.normalize_records(
        await client.execute_query(
            """RETURN {
            LET $old = (SELECT * FROM raw_captures WHERE uuid=$uuid LIMIT 1)[0];
            LET $state = (SELECT * FROM source_states WHERE organization_id=$org
                AND source_kind='raw_capture' AND source_id=$uuid LIMIT 1)[0];
            IF crypto::sha256(type::string([$old,$state])) != $fingerprint {
                THROW 'Operational source changed during capture';
            };
            IF $mode = 'replay' { RETURN $old; };
            IF $mode = 'create' {
                CREATE raw_captures CONTENT $record;
            } ELSE {
                UPDATE raw_captures SET raw_content=$record.raw_content,
                    title=$record.title, entity_type=$record.entity_type,
                    tags=$record.tags, provenance=$record.provenance,
                    embedding=$record.embedding,
                    metadata.embedding_metadata=$record.metadata.embedding_metadata,
                    revision=$old.revision + 1
                    WHERE uuid=$uuid AND organization_id=$org;
            };
            RETURN (SELECT * FROM raw_captures WHERE uuid=$uuid LIMIT 1)[0];
        };""",
            uuid=intent.id,
            org=intent.organization_id,
            record=record,
            fingerprint=snapshot["fingerprint"],
            mode=mode,
        )
    )
    if len(saved) != 1:
        raise ValueError("Operational source write returned no unique row")
    return saved[0]
