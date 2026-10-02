"""Pure compilation of a checked, dependency-free canonical raw archive batch.

The caller owns verified run/artifact loading and fresh authorization. This
compiler binds immutable prepared bytes to native store-local guards; it does
not establish either authority from UUID shape or execute the transaction.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sibyl_core.auth.memory_policy import MEMORY_PROVENANCE_METADATA_KEYS
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.migrate.archive_phase_receipts import ArchiveCreatedIdentity, ArchivePhaseKey
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveDisposition,
    ArchiveKind,
    PlannedArchiveRow,
)
from sibyl_core.migrate.personal_archive_prepared import (
    PreparedArchiveRecords,
    raw_archive_body,
)
from sibyl_core.services.archive_phase_store import (
    PreparedArchivePhaseTransaction,
    prepare_archive_phase_transaction,
)
from sibyl_core.services.content_models import raw_memory_from_record, raw_memory_record

_DEPENDENCY_METADATA = MEMORY_PROVENANCE_METADATA_KEYS | {
    "raw_source_ids",
    "reflection_identity",
    "origin_execution_id",
    "operational_source_binding",
    "epic_id",
    "parent_task_id",
    "task_id",
    "milestone_id",
}
_DIGEST = re.compile(r"[0-9a-f]{64}")

# Full native rows preserve physical IDs and nanosecond clocks. Only the
# established SourceState bookkeeping field is outside checked evidence.
_WRITER = """
FOR $archive_raw_candidate IN $archive_raw_candidates {
    IF $archive_raw_candidate.disposition != 'quarantined' {
        LET $archive_raw_rows = SELECT * FROM raw_captures
            WHERE uuid = $archive_raw_candidate.destination_id;
        LET $archive_raw_states = SELECT * OMIT validation_write_witness FROM source_states
            WHERE organization_id = $sibyl_archive_phase_org AND source_kind = 'raw_capture'
                AND source_id = $archive_raw_candidate.destination_id;
        LET $archive_raw_associations = SELECT * FROM memory_derivations
            WHERE organization_id = $sibyl_archive_phase_org AND target_kind = 'raw_capture'
                AND target_id = $archive_raw_candidate.destination_id;
        IF array::len($archive_raw_rows) > 1 OR array::len($archive_raw_states) > 1
            OR array::len($archive_raw_associations) > 1 {
            THROW 'Archive raw witness inventory is ambiguous';
        };
        LET $archive_raw_row = $archive_raw_rows[0];
        LET $archive_raw_state = $archive_raw_states[0];
        LET $archive_raw_association = $archive_raw_associations[0];
        LET $archive_raw_actual = [
            IF $archive_raw_row = NONE THEN NULL ELSE crypto::sha256(type::string($archive_raw_row)) END,
            IF $archive_raw_state = NONE THEN NULL ELSE crypto::sha256(type::string($archive_raw_state)) END,
            IF $archive_raw_association = NONE THEN NULL ELSE crypto::sha256(type::string($archive_raw_association)) END
        ];
        IF ($archive_raw_row != NONE AND $archive_raw_row.organization_id != $sibyl_archive_phase_org)
            OR $archive_raw_actual != [
                $archive_raw_candidate.witness[0] ?? NULL,
                $archive_raw_candidate.witness[1] ?? NULL,
                $archive_raw_candidate.witness[2] ?? NULL
            ] {
            THROW 'Archive raw checked witness changed';
        };
        IF $archive_raw_candidate.disposition = 'created' {
            LET $archive_raw_record = object::from_entries(array::concat(
                object::entries($archive_raw_candidate.record),
                [['metadata', $archive_raw_metadata[$archive_raw_candidate.metadata_index]]]
            ));
            CREATE raw_captures CONTENT $archive_raw_record;
        } ELSE {
            IF $archive_raw_state = NONE {
                THROW 'Archive raw checked decision requires retained source state';
            };
            IF $archive_raw_candidate.disposition = 'skipped' {
                IF $archive_raw_row = NONE OR $archive_raw_state.deleted
                    OR $archive_raw_state.revision != $archive_raw_row.revision
                    OR $archive_raw_row.derivation_required = true OR $archive_raw_association != NONE
                    OR $archive_raw_row.deleted_at != NONE {
                    THROW 'Archive raw checked skip is no longer ordinary';
                };
                FOR $archive_raw_field IN $archive_raw_dependency_fields {
                    IF ($archive_raw_row.metadata[$archive_raw_field] ?? NULL) NOT IN [NULL, '', false] {
                        THROW 'Archive raw checked skip requires dependency guards';
                    };
                };
            };
            LET $source_states_to_fence = [$archive_raw_state];
            __SOURCE_WRITE_WITNESS__
        };
    };
};
LET $sibyl_archive_phase_outcomes = $archive_raw_candidates.map(|$candidate| {
    kind: 'raw_capture', disposition: $candidate.disposition,
    destination_id: $candidate.destination_id
});
""".replace("__SOURCE_WRITE_WITNESS__", SOURCE_STATE_WRITE_WITNESS)


def _witness(row: PlannedArchiveRow) -> list[str | None] | None:
    if row.endpoint_ids or any(witness.store != "content" for witness in row.witnesses):
        raise ValueError("archive raw dependency guards are not implemented")
    if len(row.witnesses) > 1:
        raise ValueError("archive raw witnesses repeat or name unsupported sources")
    if row.disposition is not ArchiveDisposition.QUARANTINED and (
        row.destination_id is None or str(UUID(row.destination_id)) != row.destination_id
    ):
        raise ValueError("archive raw destination must be a canonical UUID")
    if not row.witnesses:
        if row.disposition is ArchiveDisposition.QUARANTINED:
            return None
        raise ValueError("archive raw checked decision requires its exact content witness")
    witness = row.witnesses[0]
    if row.destination_id is None or witness.identity != "raw_captures:" + row.destination_id:
        raise ValueError("archive raw witness identity differs from its destination")
    hashes = [witness.row_sha256, witness.state_sha256, witness.associations_sha256]
    if any(value is not None and _DIGEST.fullmatch(value) is None for value in hashes):
        raise ValueError("archive raw witness digest is malformed")
    if row.disposition is ArchiveDisposition.CREATED and any(value is not None for value in hashes):
        raise ValueError("archive raw create must have a checked absence witness")
    if row.disposition in {ArchiveDisposition.SKIPPED, ArchiveDisposition.CONFLICTED}:
        if witness.state_sha256 is None:
            raise ValueError("archive raw checked decision requires retained source state")
        if row.disposition is ArchiveDisposition.SKIPPED and (
            witness.row_sha256 is None or witness.associations_sha256 is not None
        ):
            raise ValueError("archive raw skip requires an ordinary retained row")
    return hashes


def _record(
    row: PlannedArchiveRow, body: dict[str, Any], *, organization_id: str, actor_id: str
) -> dict[str, object]:
    if row.protection != "ordinary" or any(
        body.get("metadata", {}).get(field) not in (None, "", False)
        for field in _DEPENDENCY_METADATA
    ):
        raise ValueError("archive raw dependency or provenance guards are not implemented")
    memory = raw_memory_from_record(
        {**body, "uuid": row.destination_id, "organization_id": organization_id}
    )
    if (
        raw_archive_body(memory) != body
        or memory.source_id != row.destination_id
        or memory.principal_id != actor_id
        or memory.memory_scope.value != row.audience.memory_scope
        or memory.scope_key
        != (None if row.audience.memory_scope == "private" else row.audience.scope_key)
        or memory.project_id
        != (row.audience.scope_key if row.audience.memory_scope == "project" else None)
        or memory.agent_id is not None
    ):
        raise ValueError("archive raw canonical body or audience differs from its checked binding")
    return raw_memory_record(memory)


def _metadata_expression(value: object, parameters: dict[str, object]) -> str:
    """Bind every user key/scalar while retaining literal JSON NULL structure.

    The SDK maps Python None to native NONE, which deletes object keys and is
    unequal to NULL in arrays. Only structure and literal NULL enter the SQL;
    user strings, keys and numbers remain parameters, never query fragments.
    """
    if value is None:
        return "NULL"
    if isinstance(value, dict):
        entries = [
            "["
            + _metadata_expression(key, parameters)
            + ", "
            + _metadata_expression(item, parameters)
            + "]"
            for key, item in value.items()
        ]
        return "object::from_entries([" + ", ".join(entries) + "])"
    if isinstance(value, list):
        return "[" + ", ".join(_metadata_expression(item, parameters) for item in value) + "]"
    if type(value) not in (str, int, float, bool):
        raise ValueError("archive raw metadata must retain its canonical JSON shape")
    name = "archive_raw_metadata_value_" + str(len(parameters))
    parameters[name] = value
    return "$" + name


def prepare_archive_raw_apply(
    *,
    prepared: PreparedArchiveRecords,
    key: ArchivePhaseKey,
    url: str,
    expected_revision: int,
    expected_token: str,
) -> PreparedArchivePhaseTransaction:
    """Compile every checked raw row as the sole immutable content APPLY batch.

    Unsupported dependencies reject compilation before any executor can run.
    Existing decisions retain exact native witnesses and receive a real State
    write fence. Native phase composition supplies admission and receipt proof.
    """
    plan = prepared.plan
    key = ArchivePhaseKey.model_validate(key.model_dump(mode="python"))
    if key.binding != prepared.binding or (key.store, key.action, key.batch_sequence) != (
        "content",
        "apply",
        0,
    ):
        raise ValueError("archive raw phase key differs from the prepared content batch")
    candidates: list[dict[str, object]] = []
    creates = []
    metadata_parameters: dict[str, object] = {}
    metadata_expressions: list[str] = []
    for item in prepared.rows:
        row = item.row
        if row.kind is not ArchiveKind.RAW_CAPTURE:
            continue
        witness = _witness(row)
        record = None
        if row.disposition in {ArchiveDisposition.CREATED, ArchiveDisposition.SKIPPED}:
            body = item.body
            if body is None:
                raise ValueError("archive raw writable decision requires its canonical body")
            record = _record(
                row, body, organization_id=plan.organization_id, actor_id=plan.actor_id
            )
        destination = (
            None if row.disposition is ArchiveDisposition.QUARANTINED else row.destination_id
        )
        metadata_index = None
        if row.disposition is ArchiveDisposition.CREATED:
            assert record is not None
            metadata_index = len(metadata_expressions)
            metadata_expressions.append(
                _metadata_expression(record["metadata"], metadata_parameters)
            )
            assert row.destination_id is not None
            creates.append(
                ArchiveCreatedIdentity(kind="raw_capture", destination_id=row.destination_id)
            )
        candidates.append(
            {
                "destination_id": destination,
                "disposition": row.disposition.value,
                "witness": witness,
                "record": record,
                "metadata_index": metadata_index,
            }
        )
    active = [row["destination_id"] for row in candidates if row["disposition"] != "quarantined"]
    if len(set(active)) != len(active):
        raise ValueError("archive raw checked destinations repeat")
    if not candidates:
        raise ValueError("archive raw selection is empty")
    return prepare_archive_phase_transaction(
        key=key,
        url=url,
        expected_revision=expected_revision,
        expected_token=expected_token,
        writer_statements="LET $archive_raw_metadata = ["
        + ", ".join(metadata_expressions)
        + "];\n"
        + _WRITER,
        writer_parameters={
            **metadata_parameters,
            "archive_raw_candidates": candidates,
            "archive_raw_dependency_fields": sorted(_DEPENDENCY_METADATA),
        },
        planned_creates=tuple(creates),
    )
