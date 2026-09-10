"""Public graph companions preserve native dates across JSON and restore."""

import json
from dataclasses import asdict
from datetime import datetime
from unittest.mock import AsyncMock

from sibyl_core.migrate.graph_companions import DATETIME_PATHS, relationship_from_archive
from sibyl_core.migrate.legacy_graph_archive import episode_from_payload, save_native_episode
from sibyl_core.models.entities import Relationship, RelationshipType
from sibyl_core.tools.admin import BackupData, create_backup, restore_backup
from tests.test_source_integrity_archive import destination as destination
from tests.test_source_integrity_archive import runtime as runtime
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)

STAMP = "2026-09-10T02:47:11.572211763Z"


async def test_public_companions_keep_native_dates_through_json_restore(
    runtime, destination, monkeypatch
):
    await runtime.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id="precise-edge",
                source_id="project_a",
                target_id="project_b",
                relationship_type=RelationshipType.RELATED_TO,
            )
        ]
    )
    await runtime.client.execute_query(
        "UPDATE relates_to SET created_at=d'2026-09-10T02:47:11.572211763Z', "
        "valid_at=d'2026-09-10T02:47:11.000000001Z', "
        "attributes.nested={at: d'2026-09-10T02:47:11.572211763Z', "
        "literal: '2026-09-10T02:47:11.572211763Z'} WHERE uuid='precise-edge';"
    )
    episode = episode_from_payload(
        {
            "uuid": "precise-episode",
            "name": "Episode",
            "content": "Evidence",
            "created_at": STAMP,
            "valid_at": STAMP,
        },
        organization_id=runtime.client.group_id,
    )
    await save_native_episode(runtime.client, episode)
    await runtime.client.execute_query(
        "LET $src=(SELECT VALUE id FROM episode WHERE uuid='precise-episode')[0]; "
        "LET $tgt=(SELECT VALUE id FROM entity WHERE uuid='project_a')[0]; "
        "RELATE $src->mentions:precise->$tgt SET uuid='precise-mention', group_id=$org, "
        "created_at=d'2026-09-10T02:47:11.572211763Z';",
        org=runtime.client.group_id,
    )
    result = await create_backup(organization_id=runtime.client.group_id)
    assert result.success, result.message
    payload = json.loads(json.dumps(asdict(result.backup_data)))
    edge = next(row for row in payload["relationships"] if row["id"] == "precise-edge")
    assert edge["created_at"] == STAMP
    assert edge["metadata"]["nested"] == {"at": STAMP, "literal": STAMP}
    assert ["metadata", "nested", "at"] in edge[DATETIME_PATHS]
    assert ["metadata", "nested", "literal"] not in edge[DATETIME_PATHS]
    assert payload["episodes"][0]["created_at"] == STAMP
    assert payload["mentions"][0]["created_at"] == STAMP
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    restored = await restore_backup(BackupData(**payload), organization_id=runtime.client.group_id)
    assert restored.success, restored.message
    for table, identity in (
        ("relates_to", "precise-edge"),
        ("episode", "precise-episode"),
        ("mentions", "precise-mention"),
    ):
        rows = await destination.client.execute_query(
            f"SELECT type::string(created_at) AS stamp FROM {table} WHERE uuid=$uuid;",
            uuid=identity,
        )
        assert rows[0]["stamp"] == STAMP
    rows = await destination.client.execute_query(
        "SELECT type::string(valid_at) AS valid, type::string(attributes.nested.at) AS stamp, "
        "attributes.nested.at AS typed, "
        "attributes.nested.literal AS literal FROM relates_to WHERE uuid='precise-edge';"
    )
    assert isinstance(rows[0].pop("typed"), datetime)
    assert rows[0] == {
        "valid": "2026-09-10T02:47:11.000000001Z",
        "stamp": STAMP,
        "literal": STAMP,
    }


def test_relationship_legacy_offset_and_typed_paths_survive_merge():
    from sibyl_core.migrate.merge import _merge_relationships

    payload = {
        "id": "edge",
        "source_id": "old",
        "target_id": "other",
        "relationship_type": "RELATED_TO",
        "created_at": STAMP,
        "metadata": {"old": STAMP},
        DATETIME_PATHS: [["metadata", "old"]],
    }
    merged = _merge_relationships([{"relationships": [payload]}], replacements={"old": "new"})
    assert merged[0][DATETIME_PATHS] == [["metadata", "old"]]
    model = relationship_from_archive(merged[0])
    assert model.source_id == "new"
    assert model.created_at.native_text == STAMP
    assert model.metadata["old"].native_text == STAMP


async def test_invalid_companion_date_paths_reject_before_clean(runtime, monkeypatch):
    result = await create_backup(organization_id=runtime.client.group_id)
    assert result.success, result.message
    result.backup_data.relationships = [{"id": "bad", DATETIME_PATHS: [["missing"]]}]
    execute = AsyncMock(side_effect=AssertionError("must not touch destination"))
    monkeypatch.setattr(runtime.client, "execute_query", execute)
    restored = await restore_backup(
        result.backup_data, organization_id=runtime.client.group_id, clean=True
    )
    assert not restored.success
    execute.assert_not_called()


async def test_public_backup_rejects_companion_nanosecond_capture_race(runtime, monkeypatch):
    await runtime.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id="racing-edge",
                source_id="project_a",
                target_id="project_b",
                relationship_type=RelationshipType.RELATED_TO,
            )
        ]
    )
    await runtime.client.execute_query(
        "UPDATE relates_to SET created_at=d'2026-09-10T02:47:11.572211763Z' "
        "WHERE uuid='racing-edge';"
    )
    execute = runtime.client.execute_query
    changed = False

    async def mutate_before_date_capture(query, **parameters):
        nonlocal changed
        if "AS datetimes" in query and not changed:
            changed = True
            await execute(
                "UPDATE relates_to SET created_at=d'2026-09-10T02:47:11.572211764Z' "
                "WHERE uuid='racing-edge';"
            )
        return await execute(query, **parameters)

    monkeypatch.setattr(runtime.client, "execute_query", mutate_before_date_capture)
    result = await create_backup(organization_id=runtime.client.group_id)
    assert changed
    assert not result.success
    assert "changed during datetime capture" in result.message


async def test_legacy_relationship_date_string_keeps_metadata_type(runtime):
    from sibyl_core.migrate.graph_companions import relationship_from_archive

    value = "2026-09-10T02:47:11.572211763+05:45"
    relationship = relationship_from_archive(
        {
            "id": "legacy-precise",
            "source_id": "project_a",
            "target_id": "project_b",
            "relationship_type": "RELATED_TO",
            "created_at": value,
            "metadata": {"valid_at": value},
        }
    )
    assert relationship.metadata["valid_at"] == value
    await runtime.relationship_manager.create_direct_bulk([relationship])
    rows = await runtime.client.execute_query(
        "SELECT type::string(created_at) AS created, type::string(valid_at) AS valid, "
        "attributes.valid_at AS original FROM relates_to WHERE uuid='legacy-precise';"
    )
    assert rows[0] == {
        "created": "2026-09-09T21:02:11.572211763Z",
        "valid": "2026-09-09T21:02:11.572211763Z",
        "original": value,
    }
