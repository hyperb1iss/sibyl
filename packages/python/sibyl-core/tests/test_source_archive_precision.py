"""Archive snapshots retain native nanoseconds and reject sub-microsecond races."""

import json
from copy import deepcopy

import pytest

from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.source_integrity import ArchiveDatetime, decode_record, encode_record
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.source_archive_store import (
    export_source_integrity,
    read_source_archive_snapshot,
    restore_source_integrity,
)
from tests.test_source_integrity_archive import destination as destination
from tests.test_source_integrity_archive import runtime as runtime

FIRST = "2026-09-10T02:47:11.572211763Z"
SECOND = "2026-09-10T02:47:11.572211764Z"


def test_archive_codec_keeps_native_precision_through_copy_and_json():
    value = {"created_at": ArchiveDatetime.parse(FIRST), "literal": FIRST}
    restored = decode_record(json.loads(json.dumps(encode_record(deepcopy(value)))))
    assert encode_record(restored)["record"] == {"created_at": FIRST, "literal": FIRST}
    assert restored["created_at"].isoformat() == "2026-09-10T02:47:11.572211+00:00"


async def seed(runtime):
    await runtime.entity_manager.create_direct(
        Entity(id="precision", entity_type=EntityType.SESSION, name="Precision", content="Evidence")
    )
    await runtime.client.execute_query(
        """UPDATE entity SET created_at=d'2026-09-10T02:47:11.572211763Z',
        attributes.metadata = {literal: $literal, 'literal.key': [d'2026-09-10T02:47:11.572211764Z']}
        WHERE uuid='precision';""",
        literal=FIRST,
    )


async def exact_dates(client):
    return await client.execute_query(
        """SELECT type::string(created_at) AS created_at,
        type::string(attributes.metadata.`literal.key`[0]) AS nested,
        attributes.metadata.literal AS literal FROM entity WHERE uuid='precision';"""
    )


async def test_native_archive_dates_survive_same_store_and_fresh_restore(runtime, destination):
    await seed(runtime)
    before = await exact_dates(runtime.client)
    payload = json.loads(
        json.dumps(
            await export_source_integrity(
                runtime.client.execute_query,
                kind=SourceKind.GRAPH_ENTITY,
                organizations=[runtime.client.group_id],
            )
        )
    )
    for target in (runtime, destination):
        result = await restore_source_integrity(
            target.client.execute_query,
            payload,
            kind=SourceKind.GRAPH_ENTITY,
            organizations=[runtime.client.group_id],
        )
        assert not result["conflicts"]
        assert await exact_dates(target.client) == before
    row = next(row for row in payload["source_rows"] if row["record"]["uuid"] == "precision")
    assert row["record"]["created_at"] == FIRST
    assert row["record"]["attributes"]["metadata"]["literal.key"] == [SECOND]


async def test_native_one_nanosecond_capture_race_is_rejected(runtime):
    await seed(runtime)
    original = runtime.client.execute_query
    mutated = False

    async def change(statement, **params):
        nonlocal mutated
        if "paths" in params and not mutated:
            mutated = True
            await original(
                "UPDATE entity SET created_at=d'2026-09-10T02:47:11.572211764Z' WHERE uuid='precision';"
            )
        return await original(statement, **params)

    with pytest.raises(Exception, match="source changed during datetime capture"):
        await read_source_archive_snapshot(
            change,
            kind=SourceKind.GRAPH_ENTITY,
            organizations=[runtime.client.group_id],
        )
    assert mutated


async def test_native_one_nanosecond_restore_race_is_rejected(runtime):
    await seed(runtime)
    payload = await export_source_integrity(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    original = runtime.client.execute_query
    mutated = False

    async def change(statement, **params):
        nonlocal mutated
        if statement.startswith("BEGIN TRANSACTION;") and not mutated:
            mutated = True
            await original(
                "UPDATE entity SET created_at=d'2026-09-10T02:47:11.572211764Z' WHERE uuid='precision';"
            )
        return await original(statement, **params)

    with pytest.raises(Exception, match="destination changed"):
        await restore_source_integrity(
            change,
            payload,
            kind=SourceKind.GRAPH_ENTITY,
            organizations=[runtime.client.group_id],
            clean=True,
        )
    assert mutated
    from sibyl_core.backends.surreal.records import normalize_records

    assert normalize_records(await exact_dates(runtime.client))[0]["created_at"] == SECOND


@pytest.mark.parametrize("key", ["literal.key", "back`tick", "back\\slash", "Ω", "[0]"])
async def test_native_datetime_paths_preserve_literal_metadata_keys(runtime, key):
    from surrealdb.data.types.datetime import Datetime

    await seed(runtime)
    await runtime.client.execute_query(
        "UPDATE entity SET attributes.metadata=$metadata WHERE uuid='precision';",
        metadata={key: [Datetime(FIRST)], "literal": FIRST},
    )
    payload = await export_source_integrity(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    row = next(row for row in payload["source_rows"] if row["record"]["uuid"] == "precision")
    assert row["record"]["attributes"]["metadata"][key] == [FIRST]
    assert row["record"]["attributes"]["metadata"]["literal"] == FIRST


async def test_native_raw_source_archive_retains_nanoseconds_on_empty_destination():
    from uuid import uuid4

    from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema

    source = SurrealContentClient(url="memory://", database="source_" + uuid4().hex)
    target = SurrealContentClient(url="memory://", database="target_" + uuid4().hex)
    try:
        for client in (source, target):
            await bootstrap_content_schema(client)
        await source.execute_query(
            """CREATE raw_captures CONTENT {
                uuid: 'precision', organization_id: 'org', source_id: 'source',
                raw_content: 'Evidence', principal_id: 'owner',
                created_at: d'2026-09-10T02:47:11.572211763Z',
                captured_at: d'2026-09-10T02:47:11.572211764Z',
                metadata: {date: d'2026-09-10T02:47:11.572211765Z', literal: $literal}
            };""",
            literal=FIRST,
        )
        payload = json.loads(
            json.dumps(
                await export_source_integrity(
                    source.execute_query,
                    kind=SourceKind.RAW_CAPTURE,
                    organizations=["org"],
                )
            )
        )
        result = await restore_source_integrity(
            target.execute_query,
            payload,
            kind=SourceKind.RAW_CAPTURE,
            organizations=["org"],
        )
        assert not result["conflicts"]
        query = """SELECT type::string(created_at) AS created_at,
            type::string(captured_at) AS captured_at, type::string(metadata.date) AS nested,
            metadata.literal AS literal FROM raw_captures WHERE uuid='precision';"""
        assert await target.execute_query(query) == await source.execute_query(query)
        assert payload["source_rows"][0]["record"]["captured_at"] == SECOND
    finally:
        await source.close()
        await target.close()


async def test_native_companion_one_nanosecond_change_prevents_graph_cleanup(runtime):
    from sibyl_core.models.entities import Relationship, RelationshipType

    await runtime.relationship_manager.create(
        Relationship(
            id="precise-link",
            source_id="project_a",
            target_id="project_b",
            relationship_type=RelationshipType.RELATED_TO,
        )
    )
    await runtime.client.execute_query(
        "UPDATE relates_to SET created_at=d'2026-09-10T02:47:11.572211763Z' WHERE uuid='precise-link';"
    )
    payload = await export_source_integrity(
        runtime.client.execute_query,
        kind=SourceKind.GRAPH_ENTITY,
        organizations=[runtime.client.group_id],
    )
    execute = runtime.client.execute_query
    mutated = False

    async def race(statement, **params):
        nonlocal mutated
        if statement.startswith("BEGIN TRANSACTION;") and not mutated:
            mutated = True
            await execute(
                "UPDATE relates_to SET created_at=d'2026-09-10T02:47:11.572211764Z' WHERE uuid='precise-link';"
            )
        return await execute(statement, **params)

    with pytest.raises(Exception, match="destination changed"):
        await restore_source_integrity(
            race,
            payload,
            kind=SourceKind.GRAPH_ENTITY,
            organizations=[runtime.client.group_id],
            clean=True,
            clean_graph_auxiliary=True,
        )
    assert mutated
    from sibyl_core.backends.surreal.records import normalize_records

    rows = normalize_records(
        await execute(
            "SELECT type::string(created_at) AS created_at FROM relates_to WHERE uuid='precise-link';"
        )
    )
    assert rows == [{"created_at": SECOND}]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2020-01-02T00:00:00+00:00", "2020-01-02T00:00:00Z"),
        ("2020-01-02T00:00:00", "2020-01-02T00:00:00Z"),
        ("2020-01-02T01:00:00.123456789+01:00", "2020-01-02T00:00:00.123456789Z"),
        ("2020-01-01T23:00:00.000000001-01:00", "2020-01-02T00:00:00.000000001Z"),
    ],
)
async def test_native_archive_parameters_keep_legacy_offsets_and_fraction(runtime, text, expected):
    from sibyl_core.migrate.source_integrity import native_archive_parameters

    record = {"date": ArchiveDatetime.parse(text)}
    result = await runtime.client.execute_query(
        "RETURN type::string($record.date);",
        record=native_archive_parameters(record),
    )
    assert result == expected
    assert encode_record(record)["record"]["date"] == text
