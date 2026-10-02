from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from sibyl_core.migrate.personal_archive_candidates import normalize_archive_candidates
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    ArchiveKind,
    CheckedArchivePlan,
    canonical_json,
    preview_counts,
)
from sibyl_core.migrate.personal_archive_prepared import (
    destination_semantic_digest,
    prepare_archive_body,
    prepare_archive_records,
)
from tests import test_personal_archive_candidates as fixtures


def checked_fixture(tmp_path, *, protected=False):
    org, owner, actor, destination = (str(uuid4()) for _ in range(4))
    record = fixtures._raw(org, owner, protected=protected)
    parsed = fixtures._parsed(tmp_path, org, content=fixtures._content(org, record))
    mappings = fixtures._mapping(actor, owner)
    candidates = normalize_archive_candidates(parsed, mappings, actor_id=actor)
    rows = []
    for candidate in candidates:
        row = candidate.initial_preview(
            organization_id=destination, actor_id=actor, origin=parsed.origin
        )
        if row.disposition is ArchiveDisposition.CREATED:
            body = prepare_archive_body(candidate, row, actor_id=actor, node_ids={})
            row = row.model_copy(update={"semantic_sha256": destination_semantic_digest(row, body)})
        rows.append(row)
    plan = CheckedArchivePlan(
        organization_id=destination,
        actor_id=actor,
        origin=parsed.origin,
        archive_sha256=parsed.archive_sha256,
        artifact_sha256=parsed.artifact_sha256,
        mappings=mappings,
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=tuple(reversed(rows)),
        counts=preview_counts(tuple(rows)),
    )
    return parsed, plan


def test_prepared_records_keep_independent_canonical_snapshots(tmp_path):
    parsed, plan = checked_fixture(tmp_path)
    prepared = prepare_archive_records(parsed, plan, run_id=str(uuid4()), artifact_id=str(uuid4()))
    raw = next(item for item in prepared.rows if item.row.kind is ArchiveKind.RAW_CAPTURE)
    body = raw.body
    assert body is not None
    assert body["principal_id"] == plan.actor_id
    assert body["source_id"] == raw.row.destination_id
    body["metadata"]["user-label"] = "changed locally"
    prepared.plan.counts.clear()
    assert raw.body["metadata"]["user-label"] == "synthetic"
    assert prepared.plan.counts
    assert all(
        item.body is None for item in prepared.rows if item.row.kind is not ArchiveKind.RAW_CAPTURE
    )


def test_prepared_records_keep_foreign_protection_inert(tmp_path):
    parsed, plan = checked_fixture(tmp_path, protected=True)
    prepared = prepare_archive_records(parsed, plan, run_id=str(uuid4()), artifact_id=str(uuid4()))
    assert all(item.body is None for item in prepared.rows)
    raw = next(item for item in prepared.rows if item.row.kind is ArchiveKind.RAW_CAPTURE)
    assert raw.row.disposition is ArchiveDisposition.QUARANTINED
    assert raw.row.protection == "protected"


@pytest.mark.parametrize("field", ["archive_sha256", "artifact_sha256", "origin"])
def test_prepared_records_reject_another_artifact(tmp_path, field):
    parsed, plan = checked_fixture(tmp_path)
    value = (
        "0" * 64
        if field != "origin"
        else plan.origin.model_copy(update={"organization_id": str(uuid4())})
    )
    changed = plan.model_copy(update={field: value})
    with pytest.raises(ValueError, match="origin or artifact binding"):
        prepare_archive_records(parsed, changed, run_id=str(uuid4()), artifact_id=str(uuid4()))


@pytest.mark.parametrize(
    "change", ["identity", "audience", "protection", "declarations", "endpoints", "body"]
)
def test_prepared_records_reject_changed_checked_contract(tmp_path, change):
    parsed, plan = checked_fixture(tmp_path)
    changes = {
        "identity": {"destination_id": str(uuid4())},
        "audience": {"audience": ArchiveAudience(memory_scope="private", scope_key=str(uuid4()))},
        "protection": {"protection": "inert", "disposition": ArchiveDisposition.QUARANTINED},
        "declarations": {"declarations": 3},
        "endpoints": {"endpoint_ids": (str(uuid4()),)},
        "body": {"semantic_sha256": "0" * 64},
    }
    rows = tuple(
        row.model_copy(update=changes[change]) if row.kind is ArchiveKind.RAW_CAPTURE else row
        for row in plan.rows
    )
    changed = plan.model_copy(update={"rows": rows, "counts": preview_counts(rows)})
    with pytest.raises(ValueError, match="prepared archive"):
        prepare_archive_records(parsed, changed, run_id=str(uuid4()), artifact_id=str(uuid4()))


def test_prepared_records_recheck_mutated_materialization(tmp_path):
    parsed, plan = checked_fixture(tmp_path)
    prepared = prepare_archive_records(parsed, plan, run_id=str(uuid4()), artifact_id=str(uuid4()))
    raw = next(item for item in prepared.rows if item.body is not None)
    body = raw.body
    body["raw_content"] = "different source body"
    tampered = replace(
        prepared,
        rows=tuple(
            replace(item, body_json=canonical_json(body)) if item is raw else item
            for item in prepared.rows
        ),
    )
    with pytest.raises(ValueError, match="body differs"):
        _ = tampered.plan
    shortened = replace(prepared, rows=prepared.rows[:-1])
    with pytest.raises(ValueError, match="rows differ"):
        _ = shortened.plan
    assert json.loads(prepared.checked_plan_json)["counts"]


@pytest.mark.parametrize("field", ["run_id", "artifact_id"])
@pytest.mark.parametrize("value", ["invalid", "D5ED5A41-7E36-414B-91F5-7D6102B8502D", 1])
def test_prepared_records_reject_noncanonical_run_binding(tmp_path, field, value):
    parsed, plan = checked_fixture(tmp_path)
    ids = {"run_id": str(uuid4()), "artifact_id": str(uuid4())}
    ids[field] = value
    with pytest.raises(ValueError):
        prepare_archive_records(parsed, plan, **ids)


def test_prepared_records_bind_exact_original_credential_and_mappings(tmp_path):
    from sibyl_core.migrate.personal_archive_plan import archive_digest, checked_plan_digest

    parsed, plan = checked_fixture(tmp_path)
    credential = ArchiveCredentialCeiling(
        credential_kind="api_key",
        api_key_id=str(uuid4()),
        rest_scopes=("read", "write"),
        project_restricted=True,
        project_ids=(),
        memory_restricted=True,
        memory_space_ids=(),
        memory_scope_keys=(),
    )
    plan = plan.model_copy(update={"credential": credential})
    run_id, artifact_id = str(uuid4()), str(uuid4())
    prepared = prepare_archive_records(parsed, plan, run_id=run_id, artifact_id=artifact_id)
    binding = prepared.binding
    assert (binding.organization_id, binding.actor_id, binding.run_id, binding.artifact_id) == (
        plan.organization_id,
        plan.actor_id,
        run_id,
        artifact_id,
    )
    assert binding.archive_sha256 == parsed.archive_sha256
    assert binding.artifact_sha256 == parsed.artifact_sha256
    assert binding.checked_plan_sha256 == checked_plan_digest(plan)
    assert binding.mappings_sha256 == archive_digest("sibyl-archive-mappings-v1", plan.mappings)
    assert binding.credential.model_dump(mode="python") == credential.model_dump(mode="python")
    assert binding.credential.project_restricted and not binding.credential.project_ids
    assert binding.credential.memory_restricted and not binding.credential.memory_scope_keys


def test_graph_projection_keeps_user_fields_and_ignores_storage_mirrors():
    from sibyl_core.migrate.personal_archive_prepared import graph_archive_body
    from sibyl_core.models import Entity

    org = str(uuid4())
    entity = Entity(
        id=str(uuid4()),
        entity_type="topic",
        name="empty body",
        source_file="notes/source.md",
        metadata={
            "user_label": {"nested": ["keep", 17]},
            "user_timestamp": "2026-09-30T01:02:03.123456789Z",
            "revision": 97,
            "_direct_insert": False,
            "description": "foreign mirror",
        },
    )
    body = graph_archive_body(entity, organization_id=org)
    assert body["description"] == body["content"] == "empty body"
    assert body["source_file"] == "notes/source.md"
    assert body["metadata"]["user_label"] == entity.metadata["user_label"]
    assert body["metadata"]["user_timestamp"] == entity.metadata["user_timestamp"]
    assert (
        not {
            "revision",
            "_direct_insert",
            "updated_at",
            "description",
            "entity_type",
            "source_file",
        }
        & body["metadata"].keys()
    )
    assert graph_archive_body(Entity.model_validate(body), organization_id=org) == body
    assert (
        graph_archive_body(entity.model_copy(update={"revision": 2}), organization_id=org) == body
    )


@pytest.mark.parametrize(
    ("kind", "metadata", "expected_fields", "changed_field", "changed_value"),
    [
        (
            "task",
            {},
            {
                "title": "typed archive",
                "status": "todo",
                "priority": "medium",
                "task_order": 0,
                "complexity": "medium",
            },
            "status",
            "done",
        ),
        (
            "task",
            {
                "title": "distinct title",
                "status": "done",
                "priority": "high",
                "task_order": 7,
                "complexity": "complex",
                "assignees": ["person"],
                "due_date": "2026-09-28T12:01:02Z",
            },
            {
                "title": "distinct title",
                "status": "done",
                "priority": "high",
                "task_order": 7,
                "complexity": "complex",
                "assignees": ["person"],
                "due_date": "2026-09-28T12:01:02Z",
            },
            "status",
            "todo",
        ),
        (
            "procedure",
            {},
            {"automation_level": "manual"},
            "automation_level",
            "automated",
        ),
        (
            "procedure",
            {
                "automation_level": "semi-automated",
                "required_tools": ["tool"],
                "estimated_minutes": 19,
            },
            {
                "automation_level": "semi-automated",
                "required_tools": ["tool"],
                "estimated_minutes": 19,
            },
            "automation_level",
            "automated",
        ),
    ],
)
def test_graph_projection_retains_typed_business_defaults(
    kind, metadata, expected_fields, changed_field, changed_value
):
    from sibyl_core.migrate.personal_archive_prepared import graph_archive_body
    from sibyl_core.models import Entity
    from sibyl_core.services.graph_entity_store import _entity_record
    from sibyl_core.services.graph_records import entity_from_surreal_row

    organization_id = str(uuid4())
    entity = Entity(
        id=str(uuid4()),
        entity_type=kind,
        name="typed archive",
        source_file="notes/typed.md",
        metadata={
            **metadata,
            "user_label": {"nested": ["keep", 17]},
            "user_timestamp": "2026-09-30T01:02:03.123456789Z",
        },
    )
    body = graph_archive_body(entity, organization_id=organization_id)
    native = entity_from_surreal_row(
        _entity_record(Entity.model_validate(body), group_id=organization_id)
    )
    assert graph_archive_body(native, organization_id=organization_id) == body
    assert body["source_file"] == entity.source_file
    assert body["metadata"]["user_label"] == entity.metadata["user_label"]
    assert body["metadata"]["user_timestamp"] == entity.metadata["user_timestamp"]
    assert {key: body["metadata"][key] for key in expected_fields} == expected_fields
    changed = Entity.model_validate(body)
    changed.metadata[changed_field] = changed_value
    assert graph_archive_body(changed, organization_id=organization_id) != body


@pytest.mark.parametrize("field", ["due_date", "started_at", "completed_at", "reviewed_at"])
def test_graph_projection_keeps_explicit_task_timestamp_precision(field):
    from sibyl_core.migrate.personal_archive_prepared import graph_archive_body
    from sibyl_core.models import Entity
    from sibyl_core.services.graph_entity_store import _entity_record
    from sibyl_core.services.graph_records import entity_from_surreal_row

    organization_id = str(uuid4())
    timestamp = "2026-09-30T01:02:03.123456789Z"
    entity = Entity(
        id=str(uuid4()), entity_type="task", name="typed timestamp", metadata={field: timestamp}
    )
    body = graph_archive_body(entity, organization_id=organization_id)
    native = entity_from_surreal_row(
        _entity_record(Entity.model_validate(body), group_id=organization_id)
    )
    assert body["metadata"][field] == timestamp
    assert graph_archive_body(native, organization_id=organization_id) == body
