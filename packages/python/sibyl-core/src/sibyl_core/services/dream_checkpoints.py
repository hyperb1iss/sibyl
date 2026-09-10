"""Durable observation-scoped stages for the ordinary reflection job."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.memory_pipeline.observations import evidence_hash
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services import content_client
from sibyl_core.services.reflection import (
    HeuristicReflectionExtractor,
    ReflectionExtractionRequest,
    validate_reflection_candidates,
)
from sibyl_core.services.source_state_store import RawSourceSnapshot

_CANDIDATES = TypeAdapter(list[ReflectionCandidate])


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


@dataclass(frozen=True)
class DreamSourceWork:
    snapshot: RawSourceSnapshot
    readable_projects: frozenset[str]
    writable_projects: frozenset[str]

    @property
    def request(self) -> dict[str, object]:
        source = self.snapshot.memory
        observation = self.snapshot.observation
        return {
            "version": "ordinary-reflection-dream-v1",
            "source": observation.source.key,
            "incarnation": observation.effective_incarnation,
            "generation": observation.generation,
            "evidence": observation.content_sha256,
            "principal_id": source.principal_id,
            "memory_scope": source.memory_scope.value,
            "scope_key": source.scope_key,
            "project_id": source.project_id,
            "source_id": source.source_id,
            "title": source.title,
            "inputs": {
                key: source.metadata.get(key)
                for key in (
                    "domain",
                    "related_to",
                    "suggested_memory_scope",
                    "suggested_scope_key",
                    "domain",
                    "reflection_source_title",
                    "reflection_intent",
                    "reflection_index",
                    "extractor_kind",
                    "project_id",
                    "contains_sensitive",
                )
            },
            "readable_projects": sorted(self.readable_projects),
            "writable_projects": sorted(self.writable_projects),
        }

    @property
    def key(self) -> str:
        return evidence_hash(self.request)


async def load_dream_stage(work: DreamSourceWork) -> dict[str, Any] | None:
    async with content_client.surreal_content_client() as client:
        row = await content_client.select_one(
            client,
            "SELECT * FROM dream_source_checkpoints WHERE uuid = $uuid LIMIT 1;",
            uuid=work.key,
        )
    if row is not None and row.get("request_json") != canonical(work.request):
        raise ValueError("Dream checkpoint request differs")
    return row


class CheckpointReflectionExtractor:
    """Persist a validated extraction before any downstream candidate write."""

    def __init__(self, work: DreamSourceWork) -> None:
        self.work = work

    async def extract(self, request: ReflectionExtractionRequest) -> list[ReflectionCandidate]:
        stage = await load_dream_stage(self.work)
        if stage is None:
            candidates = await HeuristicReflectionExtractor().extract(request)
            validate_reflection_candidates(candidates, require_source_ids=False)
            encoded = _CANDIDATES.dump_json(candidates).decode()
            async with content_client.surreal_content_client() as client:
                await client.execute_query(
                    """RETURN {
                        LET $source = (SELECT * FROM raw_captures WHERE organization_id = $org
                            AND uuid = $source_id LIMIT 1)[0];
                        LET $state = (SELECT * FROM source_states WHERE organization_id = $org
                            AND source_id = $source_id AND source_kind = 'raw_capture' LIMIT 1)[0];
                        IF $source = NONE OR $state = NONE OR $state.deleted != false
                            OR $source.revision != $revision OR $state.revision != $revision
                            OR $state.incarnation != $incarnation OR $state.generation != $generation {
                            THROW 'Dream extraction source changed';
                        };
                        INSERT IGNORE INTO dream_source_checkpoints $row;
                    };""",
                    org=self.work.snapshot.memory.organization_id,
                    source_id=self.work.snapshot.memory.id,
                    revision=self.work.snapshot.memory.revision,
                    incarnation=self.work.snapshot.observation.effective_incarnation,
                    generation=self.work.snapshot.observation.generation,
                    row={
                        "uuid": self.work.key,
                        "organization_id": self.work.snapshot.memory.organization_id,
                        "source_id": self.work.snapshot.memory.id,
                        "request_json": canonical(self.work.request),
                        "extraction_json": encoded,
                    },
                )
            stage = await load_dream_stage(self.work)
        if stage is None:
            raise ValueError("Dream extraction was not durably stored")
        candidates = _CANDIDATES.validate_json(stage["extraction_json"])
        validate_reflection_candidates(candidates, require_source_ids=False)
        return candidates


async def complete_dream_stage(work: DreamSourceWork, result: dict[str, object]) -> bool:
    """Acknowledge only the exact captured row revision and source incarnation."""
    source = work.snapshot.memory
    observation = work.snapshot.observation
    async with content_client.surreal_content_client() as client:
        rows = content_client.normalize_records(
            await client.execute_query(
                """RETURN {
                LET $source = (SELECT * FROM raw_captures
                    WHERE organization_id = $org AND uuid = $source_id LIMIT 1)[0];
                LET $state = (SELECT * FROM source_states WHERE organization_id = $org
                    AND source_kind = 'raw_capture' AND source_id = $source_id LIMIT 1)[0];
                IF $source = NONE OR $state = NONE OR $state.deleted != false
                    OR $source.revision != $revision OR $state.revision != $revision
                    OR $state.incarnation != $incarnation OR $state.generation != $generation {
                    RETURN {completed: false};
                };
                LET $stage = (SELECT * FROM dream_source_checkpoints WHERE uuid = $uuid LIMIT 1)[0];
                IF $stage = NONE OR $stage.request_json != $request_json {
                    THROW 'Dream checkpoint request differs';
                };
                IF $stage.completion_json = NONE {
                    UPDATE dream_source_checkpoints SET completion_json = $completion_json
                        WHERE uuid = $uuid;
                };
                RETURN {completed: true};
            };""",
                org=source.organization_id,
                source_id=source.id,
                revision=source.revision,
                incarnation=observation.effective_incarnation,
                generation=observation.generation,
                uuid=work.key,
                request_json=canonical(work.request),
                completion_json=canonical(result),
            )
        )
    return len(rows) == 1 and rows[0].get("completed") is True


@dataclass(frozen=True)
class DreamCandidateWrite:
    work: DreamSourceWork
    index: int

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("Dream candidate index must be nonnegative")

    @property
    def id(self) -> str:
        from uuid import NAMESPACE_URL, uuid5

        return str(uuid5(NAMESPACE_URL, f"sibyl:dream:{self.work.key}:{self.index}"))


_IMMUTABLE_CANDIDATE = """[
    $memory.organization_id, $memory.principal_id, $memory.source_id,
    $memory.memory_scope, $memory.scope_key, $memory.title, $memory.raw_content,
    $memory.entity_type, $memory.capture_surface, $memory.provenance, $memory.tags,
    $memory.metadata.raw_source_ids, $memory.metadata.reflection_reason,
    $memory.metadata.confidence, $memory.metadata.claim_records,
    $memory.metadata.reflection_findings, $memory.metadata.relationship_records,
    $memory.metadata.sensitivity_flags, $memory.metadata.extraction_prompt_metadata,
    $memory.metadata.source_bindings, $memory.metadata.source_ids,
    $memory.metadata.suggested_memory_scope, $memory.metadata.suggested_scope_key,
    $memory.metadata.domain, $memory.metadata.reflection_source_title,
    $memory.metadata.reflection_intent, $memory.metadata.reflection_index,
    $memory.metadata.extractor_kind, $memory.metadata.project_id, $memory.metadata.contains_sensitive
]"""


async def insert_dream_candidate(
    client: SurrealContentClient, row: dict[str, Any], write: DreamCandidateWrite
) -> dict[str, Any]:
    """Use the review writer's prepared row, preserving an existing candidate's lifecycle."""
    work = write.work
    source = work.snapshot.memory
    observation = work.snapshot.observation
    metadata = row.get("metadata", {})
    if (
        row.get("uuid") != write.id
        or row.get("organization_id") != source.organization_id
        or row.get("principal_id") != source.principal_id
        or row.get("source_id") != source.id
        or row.get("memory_scope") != source.memory_scope.value
        or row.get("scope_key") != source.scope_key
        or metadata.get("raw_source_ids") != [source.id]
    ):
        raise ValueError("Dream candidate source identity differs")
    request_fingerprint = evidence_hash(
        [
            [
                row.get(key)
                for key in (
                    "organization_id",
                    "principal_id",
                    "source_id",
                    "memory_scope",
                    "scope_key",
                    "title",
                    "raw_content",
                    "entity_type",
                    "capture_surface",
                    "provenance",
                    "tags",
                )
            ],
            [
                metadata.get(key)
                for key in (
                    "raw_source_ids",
                    "reflection_reason",
                    "confidence",
                    "claim_records",
                    "reflection_findings",
                    "relationship_records",
                    "sensitivity_flags",
                    "extraction_prompt_metadata",
                    "source_ids",
                    "suggested_memory_scope",
                    "suggested_scope_key",
                    "domain",
                    "reflection_source_title",
                    "reflection_intent",
                    "reflection_index",
                    "extractor_kind",
                    "project_id",
                    "contains_sensitive",
                )
            ],
        ]
    )
    fingerprint = f"crypto::sha256(type::string({_IMMUTABLE_CANDIDATE}))"
    rows = content_client.normalize_records(
        await client.execute_query(
            """RETURN {
            LET $stage = (SELECT * FROM dream_source_checkpoints WHERE uuid = $operation LIMIT 1)[0];
            LET $source = (SELECT * FROM raw_captures WHERE uuid = $source_id
                AND organization_id = $org LIMIT 1)[0];
            LET $state = (SELECT * FROM source_states WHERE source_id = $source_id
                AND organization_id = $org AND source_kind = 'raw_capture' LIMIT 1)[0];
            IF $stage = NONE OR $stage.request_json != $request_json
                OR $source = NONE OR $source.revision != $revision
                OR $state = NONE OR $state.revision != $revision OR $state.deleted != false
                OR $state.incarnation != $incarnation OR $state.generation != $generation {
                THROW 'Dream candidate source changed';
            };
            LET $existing = (SELECT * FROM raw_captures WHERE uuid = $candidate_id LIMIT 1)[0];
            LET $bindings = $stage.candidate_fingerprints ?? {};
            LET $expected = $bindings[$candidate_id];
            IF $expected != NONE {
                IF $existing = NONE { THROW 'Dream candidate was purged'; };
                LET $memory = $existing;
                IF """
            + fingerprint
            + """ != $expected.stored {
                    THROW 'Dream candidate immutable result changed';
                };
                IF $expected.request != $request_fingerprint {
                    THROW 'Dream candidate incoming result changed';
                };
                RETURN $existing;
            };
            IF $existing != NONE { THROW 'Dream candidate identity collision'; };
            CREATE raw_captures CONTENT $row;
            LET $memory = (SELECT * FROM raw_captures WHERE uuid = $candidate_id LIMIT 1)[0];
            UPDATE dream_source_checkpoints SET candidate_fingerprints = object::from_entries(array::concat(
                object::entries($bindings), [[$candidate_id, {stored: """
            + fingerprint
            + """, request: $request_fingerprint}]]))
                WHERE uuid = $operation;
            RETURN $memory;
        };""",
            operation=work.key,
            request_json=canonical(work.request),
            org=source.organization_id,
            source_id=source.id,
            revision=source.revision,
            incarnation=observation.effective_incarnation,
            generation=observation.generation,
            candidate_id=write.id,
            row=row,
            request_fingerprint=request_fingerprint,
        )
    )
    if len(rows) != 1:
        raise ValueError("Dream candidate write returned no row")
    return rows[0]


async def checkpoint_prepared_candidates(
    work: DreamSourceWork, candidates: list[ReflectionCandidate]
) -> list[ReflectionCandidate]:
    """Freeze grounded lifecycle decisions before their first candidate write."""
    encoded = _CANDIDATES.dump_json(candidates).decode()
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE dream_source_checkpoints SET prepared_json = $prepared "
            "WHERE uuid = $uuid AND request_json = $request AND prepared_json = NONE;",
            uuid=work.key,
            request=canonical(work.request),
            prepared=encoded,
        )
    stage = await load_dream_stage(work)
    if stage is None or not isinstance(stage.get("prepared_json"), str):
        raise ValueError("Dream candidate plan was not durably stored")
    frozen = _CANDIDATES.validate_json(stage["prepared_json"])
    validate_reflection_candidates(frozen, require_source_ids=True)
    return frozen


async def load_dream_cursor(organization_id: str) -> tuple[str, int]:
    """Read dispatch progress, never a successful-consumption acknowledgement."""
    async with content_client.surreal_content_client() as client:
        row = await content_client.select_one(
            client,
            "SELECT * FROM dream_source_cursors WHERE organization_id = $org LIMIT 1;",
            org=organization_id,
        )
    if row is None:
        return "", 0
    source_id, revision = row.get("source_id"), row.get("revision")
    if not isinstance(source_id, str) or type(revision) is not int or revision < 1:
        raise ValueError("Dream dispatch cursor is invalid")
    return source_id, revision


async def advance_dream_cursor(organization_id: str, source_id: str, revision: int) -> bool:
    """Advance before dispatch; late concurrent workers cannot rewind progress."""
    async with content_client.surreal_content_client() as client:
        rows = content_client.normalize_records(
            await client.execute_query(
                """RETURN {
                LET $current = (SELECT * FROM dream_source_cursors
                    WHERE organization_id = $org LIMIT 1)[0];
                IF ($current.revision ?? 0) != $revision { RETURN {advanced: false}; };
                IF $current = NONE {
                    CREATE dream_source_cursors CONTENT {
                        organization_id: $org, source_id: $source, revision: 1
                    };
                } ELSE {
                    UPDATE dream_source_cursors SET source_id = $source, revision += 1
                        WHERE organization_id = $org;
                };
                RETURN {advanced: true};
            };""",
                org=organization_id,
                source=source_id,
                revision=revision,
            )
        )
    return len(rows) == 1 and rows[0].get("advanced") is True
