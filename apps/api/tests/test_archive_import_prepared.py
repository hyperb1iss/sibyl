from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sibyl_core.migrate.personal_archive_plan import (
    ArchiveCredentialCeiling,
    ArchiveDisposition,
    ArchiveKind,
    CheckedArchivePlan,
    checked_plan_bytes,
    checked_plan_digest,
    preview_counts,
)
from sibyl_core.migrate.personal_archive_prepared import prepare_archive_records
from sibyl_core.models import Entity
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services.content_models import RawMemory, raw_memory_record
from sibyl_core.services.graph_entity_store import _entity_record
from tests import test_archive_import_preview as fixtures

destination = fixtures.destination
_RAW_SNAPSHOT = """
RETURN {rows:(SELECT * FROM raw_captures ORDER BY uuid),
    states:(SELECT * OMIT validation_write_witness FROM source_states ORDER BY source_id)};
"""
_GRAPH_SNAPSHOT = """
RETURN {rows:(SELECT * FROM entity ORDER BY uuid),
    states:(SELECT * OMIT validation_write_witness FROM source_states ORDER BY source_id)};
"""


@pytest.fixture
async def owned_destination(destination):
    try:
        yield destination
    finally:
        _, content, graph, _ = destination
        await content.execute_query("REMOVE NAMESPACE " + content.namespace + ";")
        await graph.execute_query("REMOVE NAMESPACE " + graph.namespace + ";")


def checked_plan(parsed, mappings, context, rows):
    return CheckedArchivePlan(
        organization_id=context.organization_id,
        actor_id=context.user_id,
        origin=parsed.origin,
        archive_sha256=parsed.archive_sha256,
        artifact_sha256=parsed.artifact_sha256,
        mappings=mappings,
        credential=ArchiveCredentialCeiling(credential_kind="session"),
        rows=rows,
        counts=preview_counts(rows),
    )


async def test_native_prepared_bodies_and_bookkeeping_preserve_checked_authority(
    owned_destination, tmp_path
):
    context, content, graph, _ = owned_destination
    parsed, mappings = fixtures._archive(tmp_path, actor_id=context.user_id)
    initial_rows = await fixtures._build(parsed, mappings, context)
    initial_plan = checked_plan(parsed, mappings, context, initial_rows)
    run_id, artifact_id = str(uuid4()), str(uuid4())
    prepared = prepare_archive_records(parsed, initial_plan, run_id=run_id, artifact_id=artifact_id)
    now = datetime.now(UTC)
    for item in prepared.rows:
        body, row = item.body, item.row
        if body is None:
            continue
        assert row.disposition is ArchiveDisposition.CREATED
        if row.kind is ArchiveKind.RAW_CAPTURE:
            body["memory_scope"] = MemoryScope(body["memory_scope"])
            memory = RawMemory(
                id=row.destination_id,
                organization_id=context.organization_id,
                created_at=now,
                captured_at=now,
                **body,
            )
            await content.execute_query(
                "CREATE raw_captures CONTENT $record;", record=raw_memory_record(memory)
            )
        elif row.kind is ArchiveKind.GRAPH_ENTITY:
            entity = Entity(
                **body,
                organization_id=context.organization_id,
                created_at=now,
                updated_at=now,
            )
            await graph.execute_query(
                "CREATE entity CONTENT $record;",
                record=_entity_record(entity, group_id=context.organization_id),
            )
    existing_rows = await fixtures._build(parsed, mappings, context)
    for row in existing_rows:
        if row.kind in {ArchiveKind.RAW_CAPTURE, ArchiveKind.GRAPH_ENTITY}:
            assert row.disposition is ArchiveDisposition.SKIPPED
            before = next(
                item.row
                for item in prepared.rows
                if item.row.original_id == row.original_id and item.row.kind is row.kind
            )
            assert row.semantic_sha256 == before.semantic_sha256
            assert row.witnesses[0].state_sha256 is not None
    existing_plan = checked_plan(parsed, mappings, context, existing_rows)
    existing_prepared = prepare_archive_records(
        parsed, existing_plan, run_id=run_id, artifact_id=artifact_id
    )
    assert existing_prepared.binding.run_id == run_id
    assert existing_prepared.binding.artifact_id == artifact_id

    snapshots = []
    full_hashes = []
    stores = (
        (content, _RAW_SNAPSHOT, "raw_capture", "UPDATE raw_captures SET revision+=1;"),
        (graph, _GRAPH_SNAPSHOT, "graph_entity", "UPDATE entity SET revision+=1;"),
    )
    for client, snapshot_query, kind, _ in stores:
        snapshots.append(await client.execute_query(snapshot_query))
        full_hashes.append(
            await client.execute_query(
                "SELECT source_id, crypto::sha256(type::string($this)) AS sha256 "
                "FROM source_states ORDER BY source_id;"
            )
        )
        await client.execute_query(
            "UPDATE source_states SET validation_write_witness=type::string(rand::uuid()) "
            "WHERE organization_id=$organization_id AND source_kind=$source_kind;",
            organization_id=context.organization_id,
            source_kind=kind,
        )
    fenced_rows = await fixtures._build(parsed, mappings, context)
    fenced_plan = checked_plan(parsed, mappings, context, fenced_rows)
    assert checked_plan_bytes(fenced_plan) == checked_plan_bytes(existing_plan)
    assert checked_plan_digest(fenced_plan) == checked_plan_digest(existing_plan)
    for index, (client, snapshot_query, _, revision_query) in enumerate(stores):
        assert await client.execute_query(snapshot_query) == snapshots[index]
        assert (
            await client.execute_query(
                "SELECT source_id, crypto::sha256(type::string($this)) AS sha256 "
                "FROM source_states ORDER BY source_id;"
            )
            != full_hashes[index]
        )
        await client.execute_query(revision_query)
    changed_rows = await fixtures._build(parsed, mappings, context)
    changed_plan = checked_plan(parsed, mappings, context, changed_rows)
    assert checked_plan_digest(changed_plan) != checked_plan_digest(existing_plan)
    for row in changed_rows:
        if row.kind in {ArchiveKind.RAW_CAPTURE, ArchiveKind.GRAPH_ENTITY}:
            old = next(
                item
                for item in existing_rows
                if item.kind is row.kind and item.original_id == row.original_id
            )
            assert row.disposition is ArchiveDisposition.SKIPPED
            assert row.semantic_sha256 == old.semantic_sha256
            assert row.witnesses[0].row_sha256 != old.witnesses[0].row_sha256
            assert row.witnesses[0].state_sha256 != old.witnesses[0].state_sha256


async def test_native_graph_writer_defaults_preserve_semantic_user_fields(
    owned_destination, tmp_path
):
    import hashlib
    import json

    from sibyl_core.migrate.archive import build_manifest, write_archive
    from sibyl_core.migrate.personal_archive_intake import parse_personal_archive
    from sibyl_core.services.graph_records import entity_from_surreal_row

    context, _, graph, _ = owned_destination
    variants = (
        "empty-body",
        "source-file",
        "metadata-source-file",
        "writer-mirrors",
        "user-metadata",
    )
    for index, variant in enumerate(variants):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        parsed, mappings = fixtures._archive(case_path, actor_id=context.user_id)
        payload = parsed.graph
        record = payload["source_integrity"]["source_rows"][0]["record"]
        if variant == "empty-body":
            record["description"] = record["content"] = ""
        elif variant == "source-file":
            record["source_file"] = "notes/meaningful-source.md"
        elif variant == "metadata-source-file":
            record["attributes"]["source_file"] = "notes/metadata-source.md"
        elif variant == "writer-mirrors":
            record["attributes"].update(
                _direct_insert=False,
                revision=93,
                description="foreign mirror",
                entity_type="foreign mirror",
                updated_at="2026-09-01T01:02:03Z",
            )
        else:
            record["attributes"].update(
                user_label={"nested": ["keep", {"value": 17}]},
                user_timestamp="2026-09-30T01:02:03.123456789Z",
            )
        public = entity_from_surreal_row(record).model_dump(mode="json")
        payload["entities"][0] = public
        section = payload["source_integrity"]
        section["sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in section.items() if key != "sha256"},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        files = {
            "graph.json": json.dumps(payload).encode(),
            "content.json": json.dumps(parsed.content).encode(),
        }
        path = case_path / "canonical-graph.tgz"
        write_archive(
            path,
            manifest=build_manifest(
                organization_id=parsed.origin.organization_id,
                source_store="surreal",
                files=files,
            ),
            files=files,
        )
        parsed = parse_personal_archive(path, fixtures._budget())
        rows = await fixtures._build(parsed, mappings, context)
        plan = checked_plan(parsed, mappings, context, rows)
        prepared = prepare_archive_records(
            parsed, plan, run_id=str(uuid4()), artifact_id=str(uuid4())
        )
        item = next(
            item
            for item in prepared.rows
            if item.row.kind is ArchiveKind.GRAPH_ENTITY and item.row.original_id == public["id"]
        )
        body = item.body
        assert body is not None
        if variant == "empty-body":
            assert body["description"] == body["content"] == body["name"]
        elif variant in {"source-file", "metadata-source-file"}:
            assert body["source_file"] == public["source_file"]
        elif variant == "user-metadata":
            assert body["metadata"]["user_label"] == public["metadata"]["user_label"]
            assert body["metadata"]["user_timestamp"] == public["metadata"]["user_timestamp"]
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
        await graph.execute_query(
            "CREATE entity CONTENT $record;",
            record=_entity_record(
                Entity.model_validate(body),
                group_id=context.organization_id,
            ),
        )
        native_before = await graph.execute_query(_GRAPH_SNAPSHOT)
        checked = next(
            row
            for row in await fixtures._build(parsed, mappings, context)
            if row.kind is ArchiveKind.GRAPH_ENTITY and row.original_id == public["id"]
        )
        assert checked.disposition is ArchiveDisposition.SKIPPED
        assert checked.semantic_sha256 == item.row.semantic_sha256
        assert await graph.execute_query(_GRAPH_SNAPSHOT) == native_before
        if variant in {"source-file", "metadata-source-file"}:
            await graph.execute_query(
                "UPDATE entity SET attributes.source_file='notes/different.md', revision+=1 WHERE uuid=$identity;",
                identity=item.row.destination_id,
            )
            changed = next(
                row
                for row in await fixtures._build(parsed, mappings, context)
                if row.kind is ArchiveKind.GRAPH_ENTITY and row.original_id == public["id"]
            )
            assert changed.disposition is ArchiveDisposition.CONFLICTED
