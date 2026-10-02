from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sibyl_core.migrate.archive import build_manifest, write_archive
from sibyl_core.migrate.personal_archive_candidates import normalize_archive_candidates
from sibyl_core.migrate.personal_archive_intake import parse_personal_archive
from sibyl_core.migrate.personal_archive_plan import ArchiveDisposition, ArchiveKind
from sibyl_core.services.content_models import RawMemory, raw_memory_record
from tests import test_archive_import_preview as fixtures

destination = fixtures.destination


async def test_native_raw_defaults_remain_identical_after_canonical_write(destination, tmp_path):
    context, content, _, _ = destination
    variants = (
        (),
        ("tags",),
        ("title", "entity_type", "review_state"),
        ("metadata",),
        ("tags", "metadata", "title", "entity_type", "review_state"),
        ("agent_id", "project_id"),
    )
    for index, omitted_fields in enumerate(variants):
        case_path = tmp_path / str(index)
        case_path.mkdir()
        parsed, mappings = fixtures._archive(case_path, actor_id=context.user_id)
        payload = parsed.content
        for field in omitted_fields:
            payload["tables"]["raw_captures"][0].pop(field)
            payload["source_integrity"]["source_rows"][0]["record"].pop(field)
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
            "content.json": json.dumps(payload).encode(),
            "graph.json": json.dumps(parsed.graph).encode(),
        }
        path = case_path / "canonical-defaults.tgz"
        write_archive(
            path,
            manifest=build_manifest(
                organization_id=parsed.origin.organization_id, source_store="surreal", files=files
            ),
            files=files,
        )
        parsed = parse_personal_archive(path, fixtures._budget())
        row = fixtures._row(
            await fixtures._build(parsed, mappings, context), ArchiveKind.RAW_CAPTURE
        )
        assert row.disposition is ArchiveDisposition.CREATED
        candidate = next(
            candidate
            for candidate in normalize_archive_candidates(
                parsed, mappings, actor_id=context.user_id
            )
            if candidate.kind is ArchiveKind.RAW_CAPTURE
        )
        body = fixtures.preview._prepared_body(
            candidate, row, actor_id=context.user_id, node_ids={}
        )
        now = datetime.now(UTC)
        memory = RawMemory(
            id=row.destination_id,
            organization_id=context.organization_id,
            source_id=body["source_id"],
            principal_id=context.user_id,
            title=body["title"],
            raw_content=body["raw_content"],
            entity_type=body["entity_type"],
            tags=body["tags"],
            metadata=body["metadata"],
            review_state=body["review_state"],
            created_at=now,
            captured_at=now,
        )
        await content.execute_query(
            "CREATE raw_captures CONTENT $record;", record=raw_memory_record(memory)
        )
        native_before = await content.execute_query(
            "RETURN {rows: (SELECT * FROM raw_captures ORDER BY uuid), states: (SELECT * FROM source_states ORDER BY source_id)};"
        )
        after = fixtures._row(
            await fixtures._build(parsed, mappings, context), ArchiveKind.RAW_CAPTURE
        )
        assert after.disposition is ArchiveDisposition.SKIPPED
        assert after.reason == "destination_canonical_identical"
        assert after.semantic_sha256 == row.semantic_sha256
        assert native_before == await content.execute_query(
            "RETURN {rows: (SELECT * FROM raw_captures ORDER BY uuid), states: (SELECT * FROM source_states ORDER BY source_id)};"
        )
