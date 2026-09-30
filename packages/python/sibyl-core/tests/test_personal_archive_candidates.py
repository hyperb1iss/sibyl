from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive import build_manifest, write_archive
from sibyl_core.migrate.personal_archive_candidates import normalize_archive_candidates
from sibyl_core.migrate.personal_archive_intake import (
    ArchiveIntakeBudget,
    ArchiveIntakeError,
    parse_personal_archive,
)
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveAudience,
    ArchiveDisposition,
    ArchiveKind,
    ArchiveMappings,
    preview_counts,
)
from sibyl_core.migrate.source_integrity import build_integrity_archive
from sibyl_core.models.entities import Relationship, RelationshipType
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services.content_models import RawMemory, raw_memory_record
from sibyl_core.services.graph_records import entity_from_surreal_row


def _budget():
    return ArchiveIntakeBudget(
        compressed_bytes=1_000_000,
        inflated_bytes=1_000_000,
        member_bytes=500_000,
        members=16,
        json_depth=32,
        json_scalar_bytes=100_000,
        json_nodes=50_000,
        parsed_rows=10_000,
        encoded_artifact_bytes=1_000_000,
        encoded_plan_bytes=1_000_000,
        metadata_transaction_bytes=2_000_000,
    )


def _mapping(actor, owner):
    return ArchiveMappings(
        source_private_owner_id=owner,
        quarantine=ArchiveAudience(memory_scope="private", scope_key=actor),
    )


def _state(org, identity, kind, *, deleted=False):
    return {
        "organization_id": org,
        "source_kind": kind.value,
        "source_id": identity,
        "generation": 1,
        "revision": 1,
        "deleted": deleted,
        "incarnation": str(uuid4()),
    }


def _raw(org, owner, *, protected=False, deleted=False):
    now = datetime(2026, 9, 30, tzinfo=UTC)
    memory = RawMemory(
        id=str(uuid4()),
        organization_id=org,
        source_id="declared-foreign-capture",
        principal_id=owner,
        memory_scope=MemoryScope.PRIVATE,
        title="Synthetic capture",
        raw_content="Synthetic raw evidence",
        captured_at=now,
        created_at=now,
        deleted_at=now if deleted else None,
        metadata={"user-label": "synthetic"},
    )
    record = raw_memory_record(memory)
    record["derivation_required"] = protected
    return record


def _content(org, record, *, current=True, deleted=False):
    section = (
        build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            source_rows=[record],
            source_states=[_state(org, record["uuid"], SourceKind.RAW_CAPTURE, deleted=deleted)],
            derivations=[],
        )
        if current
        else None
    )
    payload = {
        "version": "2.0" if current else "1.0",
        "organization_id": org,
        "tables": {"raw_captures": [record]},
        "row_counts": {"raw_captures": 1},
        "total_rows": 1,
    }
    if section is not None:
        payload["source_integrity"] = section
    return payload


def _entity(org, owner, *, protected=False, entity_type="topic"):
    return {
        "uuid": str(uuid4()),
        "group_id": org,
        "entity_type": entity_type,
        "name": "Synthetic entity",
        "content": "Synthetic graph evidence",
        "revision": 1,
        "derivation_required": protected,
        "attributes": {"memory_scope": "private", "principal_id": owner},
    }


def _graph(org, records, *, current=True, relationships=(), deleted=()):
    payload = {
        "version": "3.0" if current else "2.0",
        "organization_id": org,
        "entities": [entity_from_surreal_row(row).model_dump(mode="json") for row in records],
        "entity_count": len(records),
        "relationships": list(relationships),
        "relationship_count": len(relationships),
    }
    if current:
        payload["source_integrity"] = build_integrity_archive(
            kind=SourceKind.GRAPH_ENTITY,
            organizations=[org],
            source_rows=records,
            source_states=[
                _state(org, row["uuid"], SourceKind.GRAPH_ENTITY, deleted=row["uuid"] in deleted)
                for row in records
            ],
            derivations=[],
        )
    return payload


def _parsed(tmp_path: Path, org, *, graph=None, content=None):
    payloads = {
        name: payload
        for name, payload in (
            ("graph.json", graph),
            ("content.json", content),
        )
        if payload is not None
    }
    files = {
        name: json.dumps(payload, default=lambda value: value.isoformat()).encode()
        for name, payload in payloads.items()
    }
    path = tmp_path / "candidate.spool"
    write_archive(
        path,
        manifest=build_manifest(
            organization_id=org,
            source_store="surreal",
            files=files,
        ),
        files=files,
    )
    return parse_personal_archive(path, _budget())


def test_personal_archive_candidates_reconcile_canonical_raw_and_public_mirror(tmp_path):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    record = _raw(org, owner)
    parsed = _parsed(tmp_path, org, content=_content(org, record))
    candidates = normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)
    raw = next(row for row in candidates if row.kind == ArchiveKind.RAW_CAPTURE)
    assert raw.protection == "ordinary"
    assert raw.declarations == 2
    preview = raw.initial_preview(
        organization_id=str(uuid4()), actor_id=actor, origin=parsed.origin
    )
    assert preview.disposition == ArchiveDisposition.CREATED
    assert preview.destination_id != record["uuid"]
    assert preview.audience.scope_key == actor
    counts = preview_counts((preview,))["raw_capture"]
    assert counts.created == counts.coalesced == 1
    assert (
        next(row for row in candidates if row.kind == ArchiveKind.SOURCE_STATE).protection
        == "inert"
    )


@pytest.mark.parametrize("category", ["protected", "retired", "legacy", "metadata-binding"])
def test_personal_archive_candidates_keep_nonordinary_raw_content_quarantined(tmp_path, category):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    record = _raw(org, owner, protected=category == "protected", deleted=category == "retired")
    if category == "metadata-binding":
        record["metadata"]["source_bindings"] = {"untrusted-source": 1}
    parsed = _parsed(
        tmp_path,
        org,
        content=_content(
            org,
            record,
            current=category != "legacy",
            deleted=category == "retired",
        ),
    )
    raw = next(
        row
        for row in normalize_archive_candidates(
            parsed,
            _mapping(actor, owner),
            actor_id=actor,
        )
        if row.kind == ArchiveKind.RAW_CAPTURE
    )
    assert raw.protection == ("retired" if category == "retired" else "protected")
    assert (
        raw.initial_preview(
            organization_id=str(uuid4()),
            actor_id=actor,
            origin=parsed.origin,
        ).disposition
        == ArchiveDisposition.QUARANTINED
    )


def test_personal_archive_candidates_coalesce_graph_body_and_reference_mirrors(tmp_path):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    record = _entity(org, owner)
    content = {
        "version": "1.0",
        "organization_id": org,
        "tables": {"entity": [{"uuid": record["uuid"], "organization_id": org}]},
    }
    parsed = _parsed(tmp_path, org, graph=_graph(org, [record]), content=content)
    graph = next(
        row
        for row in normalize_archive_candidates(
            parsed,
            _mapping(actor, owner),
            actor_id=actor,
        )
        if row.kind == ArchiveKind.GRAPH_ENTITY
    )
    assert graph.declarations == 3
    assert graph.protection == "ordinary"


@pytest.mark.parametrize("kind", ["raw", "graph"])
def test_personal_archive_candidates_reject_differing_body_mirrors(tmp_path, kind):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    if kind == "raw":
        content = _content(org, _raw(org, owner))
        content["tables"]["raw_captures"] = copy.deepcopy(content["tables"]["raw_captures"])
        content["tables"]["raw_captures"][0]["raw_content"] = "Differing mirror evidence"
        parsed = _parsed(tmp_path, org, content=content)
    else:
        graph = _graph(org, [_entity(org, owner)])
        graph["entities"][0]["name"] = "Differing mirror evidence"
        parsed = _parsed(tmp_path, org, graph=graph)
    with pytest.raises(ArchiveIntakeError, match=r"differing .*mirror body"):
        normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)


def test_personal_archive_candidates_reject_differing_duplicates_and_coalesce_identical(tmp_path):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    graph = _graph(org, [_entity(org, owner)], current=False)
    graph["entities"] *= 2
    graph["entity_count"] = 2
    parsed = _parsed(tmp_path, org, graph=graph)
    (candidate,) = normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)
    assert candidate.declarations == 2 and candidate.protection == "protected"
    graph["entities"][1] = {**graph["entities"][1], "content": "Different body"}
    parsed = _parsed(tmp_path, org, graph=graph)
    with pytest.raises(ArchiveIntakeError, match="differing duplicate"):
        normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)


@pytest.mark.parametrize("kind", ["raw", "graph"])
def test_personal_archive_candidates_reject_missing_current_mirror_inventory(tmp_path, kind):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    if kind == "raw":
        content = _content(org, _raw(org, owner))
        content["tables"]["raw_captures"] = []
        content["row_counts"]["raw_captures"] = content["total_rows"] = 0
        parsed = _parsed(tmp_path, org, content=content)
    else:
        graph = _graph(org, [_entity(org, owner)])
        graph["entities"], graph["entity_count"] = [], 0
        parsed = _parsed(tmp_path, org, graph=graph)
    with pytest.raises(ArchiveIntakeError, match="mirrors differ"):
        normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)


@pytest.mark.parametrize("mutation", ["wrong-owner", "missing-project", "wrong-source-org"])
def test_personal_archive_candidates_require_explicit_audience_and_origin_consistency(
    tmp_path, mutation
):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    record = _raw(org, owner)
    if mutation == "wrong-owner":
        record["principal_id"] = str(uuid4())
    elif mutation == "missing-project":
        record["memory_scope"], record["scope_key"] = "project", "foreign-project"
    else:
        record["organization_id"] = str(uuid4())
    # Legacy bytes remain untrusted labels, so this path cannot adopt a ledger.
    parsed = _parsed(tmp_path, org, content=_content(org, record, current=False))
    with pytest.raises(ArchiveIntakeError):
        normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)


@pytest.mark.parametrize(
    "category", ["ordinary", "protected", "retired", "foreign-binding", "missing"]
)
def test_personal_archive_candidates_keep_dependent_edges_quarantined(tmp_path, category):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    source = _entity(org, owner)
    target = _entity(org, owner, protected=category == "protected")
    relationship = Relationship(
        id=str(uuid4()),
        source_id=source["uuid"],
        target_id=str(uuid4()) if category == "missing" else target["uuid"],
        relationship_type=RelationshipType.RELATED_TO,
    ).model_dump(mode="json")
    if category == "foreign-binding":
        relationship["operational_source_binding"] = {"foreign": "untrusted binding"}
    graph = _graph(
        org,
        [source, target],
        relationships=[relationship],
        deleted=[target["uuid"]] if category == "retired" else (),
    )
    parsed = _parsed(tmp_path, org, graph=graph)
    edge = next(
        row
        for row in normalize_archive_candidates(
            parsed,
            _mapping(actor, owner),
            actor_id=actor,
        )
        if row.kind == ArchiveKind.GRAPH_RELATIONSHIP
    )
    assert edge.protection == ("ordinary" if category == "ordinary" else "protected")
    preview = edge.initial_preview(
        organization_id=str(uuid4()), actor_id=actor, origin=parsed.origin
    )
    assert preview.disposition == (
        ArchiveDisposition.CREATED if category == "ordinary" else ArchiveDisposition.QUARANTINED
    )


def test_personal_archive_candidates_map_existing_anchors_without_creating_foreign_memberships(
    tmp_path,
):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    project, team = (
        _entity(org, owner, entity_type="project"),
        _entity(org, owner, entity_type="team"),
    )
    mappings = ArchiveMappings(
        source_private_owner_id=owner,
        projects={project["uuid"]: "existing-project"},
        teams={team["uuid"]: "existing-team"},
        quarantine=ArchiveAudience(memory_scope="private", scope_key=actor),
    )
    parsed = _parsed(tmp_path, org, graph=_graph(org, [project, team], current=False))
    rows = normalize_archive_candidates(parsed, mappings, actor_id=actor)
    assert {row.fixed_destination_id for row in rows} == {"existing-project", "existing-team"}
    assert all(
        row.initial_preview(
            organization_id=str(uuid4()),
            actor_id=actor,
            origin=parsed.origin,
        ).disposition
        == ArchiveDisposition.SKIPPED
        for row in rows
    )


def test_personal_archive_candidates_treat_unmatched_reference_and_runtime_history_as_inert(
    tmp_path,
):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    content = {
        "version": "1.0",
        "organization_id": org,
        "tables": {
            "entity": [{"uuid": str(uuid4()), "organization_id": org}],
            "source_imports": [
                {"uuid": str(uuid4()), "organization_id": org, "status": "completed"}
            ],
            "api_idempotency_records": [{"uuid": str(uuid4()), "organization_id": org}],
        },
    }
    parsed = _parsed(tmp_path, org, content=content)
    candidates = normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)
    assert len(candidates) == 3
    assert all(row.protection == "inert" for row in candidates)
    assert all(
        row.initial_preview(
            organization_id=str(uuid4()),
            actor_id=actor,
            origin=parsed.origin,
        ).disposition
        == ArchiveDisposition.QUARANTINED
        for row in candidates
    )


@pytest.mark.parametrize("marker", ["protected", "retired"])
def test_personal_archive_candidates_reject_contradictory_raw_protection(tmp_path, marker):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    content = _content(org, _raw(org, owner))
    content["tables"]["raw_captures"] = copy.deepcopy(content["tables"]["raw_captures"])
    mirror = content["tables"]["raw_captures"][0]
    if marker == "protected":
        mirror["derivation_required"] = True
    else:
        mirror["deleted_at"] = datetime(2026, 9, 30, tzinfo=UTC)
    parsed = _parsed(tmp_path, org, content=content)
    with pytest.raises(ArchiveIntakeError, match="differing raw capture mirror protection"):
        normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)


@pytest.mark.parametrize("table", ["crawl_sources", "crawled_documents", "document_chunks"])
def test_personal_archive_candidates_keep_foreign_crawl_configuration_and_provenance_inert(
    tmp_path, table
):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    content = {
        "version": "1.0",
        "organization_id": org,
        "tables": {
            table: [
                {
                    "uuid": str(uuid4()),
                    "organization_id": org,
                    "url": "https://synthetic.invalid/example",
                    "source_id": str(uuid4()),
                    "content": "Captured crawler provenance",
                }
            ]
        },
    }
    parsed = _parsed(tmp_path, org, content=content)
    (candidate,) = normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)
    assert candidate.protection == "inert"
    assert candidate.reason == "foreign_ingestion_authority_quarantined"
    preview = candidate.initial_preview(
        organization_id=str(uuid4()), actor_id=actor, origin=parsed.origin
    )
    assert preview.disposition == ArchiveDisposition.QUARANTINED
    assert preview.destination_id is None


def test_personal_archive_candidates_reject_relationship_origin_disagreement(tmp_path):
    org, owner, actor = str(uuid4()), str(uuid4()), str(uuid4())
    source, target = _entity(org, owner), _entity(org, owner)
    edge = Relationship(
        id=str(uuid4()),
        source_id=source["uuid"],
        target_id=target["uuid"],
        relationship_type=RelationshipType.RELATED_TO,
    ).model_dump(mode="json")
    edge["group_id"] = str(uuid4())
    parsed = _parsed(tmp_path, org, graph=_graph(org, [source, target], relationships=[edge]))
    with pytest.raises(ArchiveIntakeError, match="declared source organization"):
        normalize_archive_candidates(parsed, _mapping(actor, owner), actor_id=actor)
