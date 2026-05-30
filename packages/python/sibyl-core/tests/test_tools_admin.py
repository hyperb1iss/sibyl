"""Tests for sibyl_core.tools.admin."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, call, patch

import pytest

from sibyl_core.migrate.legacy_graph_archive import parse_backup_datetime
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.tasks import Epic, Project, ProjectStatus, Task, TaskPriority, TaskStatus
from sibyl_core.services.graph import (
    EntityManager,
    RelationshipManager,
    SurrealGraphClient,
    normalize_records,
    prepare_graph_schema,
)
from sibyl_core.tools import admin as admin_module
from sibyl_core.tools.admin import (
    BackupData,
    backfill_episode_task_relationships,
    backfill_project_id_from_relationships,
    backfill_shared_project,
    backfill_task_project_relationships,
    create_backup,
    get_stats,
    health_check,
    migrate_fix_name_embedding_types,
    rebuild_indices,
    restore_backup,
)


class TestRebuildIndices:
    """Admin index rebuilds should report real behavior, not placeholder success."""

    @pytest.mark.asyncio
    async def test_rebuild_indices_reports_not_implemented(self) -> None:
        """The current runtime should fail honestly until rebuild support exists."""
        result = await rebuild_indices("search")

        assert result.success is False
        assert result.indices_rebuilt == []
        assert "not implemented" in result.message.lower()
        assert "search" in result.message

    @pytest.mark.asyncio
    async def test_rebuild_indices_rejects_unknown_target(self) -> None:
        """Unknown targets should return a clear validation error."""
        result = await rebuild_indices("mystery")

        assert result.success is False
        assert result.indices_rebuilt == []
        assert "unknown index type" in result.message.lower()

    @pytest.mark.asyncio
    async def test_rebuild_indices_normalizes_target_values(self) -> None:
        """Whitespace and casing should normalize before reporting."""
        result = await rebuild_indices(" ALL ")

        assert result.success is False
        assert result.indices_rebuilt == []
        assert "requested target: all" in result.message.lower()


class TestEmbeddingMaintenance:
    """Embedding maintenance should not run Falkor-only Cypher in Surreal mode."""

    @pytest.mark.asyncio
    async def test_name_embedding_migration_noops_in_surreal_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = AsyncMock()
        graph_client = SimpleNamespace(driver=driver)

        monkeypatch.setattr(admin_module.settings, "store", "surreal")
        monkeypatch.setattr(
            admin_module,
            "get_graph_client",
            AsyncMock(return_value=graph_client),
        )

        result = await migrate_fix_name_embedding_types()

        assert result.success is True
        assert result.entities_updated == 0
        driver.execute_query.assert_not_called()


class TestBackupInventory:
    """Migration backups should export the full entity inventory."""

    def test_backup_entity_types_cover_every_entity_type(self) -> None:
        from sibyl_core.tools.admin import BACKUP_ENTITY_TYPES

        assert set(BACKUP_ENTITY_TYPES) == set(EntityType)

    @pytest.mark.asyncio
    async def test_create_backup_separates_mentions_from_entity_relationships(self) -> None:
        org_id = "00000000-0000-0000-0000-000000000111"
        client = SurrealGraphClient(group_id=org_id, url="memory://")
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=org_id)
        relationship_manager = RelationshipManager(client, group_id=org_id)
        seed = BackupData(
            version="2.0",
            created_at="2026-04-19T00:00:00Z",
            organization_id=org_id,
            entity_count=2,
            relationship_count=1,
            entities=[
                Entity(
                    id="entity-1",
                    entity_type=EntityType.PATTERN,
                    name="Pattern",
                    organization_id=org_id,
                ).model_dump(mode="json"),
                Entity(
                    id="entity-2",
                    entity_type=EntityType.TOPIC,
                    name="Untyped",
                    organization_id=org_id,
                ).model_dump(mode="json"),
            ],
            relationships=[
                Relationship(
                    id="rel-1",
                    relationship_type=RelationshipType.MENTIONS,
                    source_id="entity-1",
                    target_id="entity-2",
                    weight=0.75,
                    metadata={"source_ids": ["source:doc"]},
                ).model_dump(mode="json")
            ],
            episode_count=1,
            mention_count=1,
            episodes=[
                {
                    "uuid": "episode-1",
                    "name": "Conversation",
                    "source": "message",
                    "source_description": "chat",
                    "content": "hi",
                    "created_at": "2026-04-19T00:00:00Z",
                    "valid_at": "2026-04-19T00:00:00Z",
                    "entity_edges": [],
                }
            ],
            mentions=[
                {
                    "uuid": "mention-1",
                    "source_id": "episode-1",
                    "target_id": "entity-1",
                    "created_at": "2026-04-19T00:00:00Z",
                }
            ],
        )

        try:
            with patch(
                "sibyl_core.tools.admin.get_graph_runtime",
                AsyncMock(
                    return_value=SimpleNamespace(
                        client=client,
                        entity_manager=entity_manager,
                        relationship_manager=relationship_manager,
                    )
                ),
            ):
                restored = await restore_backup(seed, organization_id=org_id, skip_existing=False)
                assert restored.success is True, restored.errors

                result = await create_backup(organization_id=org_id)
        finally:
            await client.close()

        assert result.success is True
        assert result.entity_count == 2
        assert result.relationship_count == 1
        assert result.episode_count == 1
        assert result.mention_count == 1
        assert result.backup_data is not None
        assert {entity["id"] for entity in result.backup_data.entities} == {"entity-1", "entity-2"}
        # MENTIONS relationships live in relates_to; episode mentions live in the
        # mentions table. Backup must keep them in their separate buckets.
        assert {rel["id"] for rel in result.backup_data.relationships} == {"rel-1"}
        assert result.backup_data.relationships[0]["relationship_type"] == "MENTIONS"
        assert {episode["uuid"] for episode in result.backup_data.episodes} == {"episode-1"}
        assert {mention["uuid"] for mention in result.backup_data.mentions} == {"mention-1"}

    def test_parse_backup_datetime_rejects_invalid_values(self) -> None:
        with pytest.raises(ValueError):
            parse_backup_datetime("not-a-date")


class TestHealthAndStats:
    """Admin health/stat helpers should use paged entity seams."""

    @pytest.mark.asyncio
    async def test_health_check_uses_paged_entity_counts(self) -> None:
        """health_check should count through entity-manager pagination."""
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = SimpleNamespace()
        entity_manager._driver = None
        entity_manager._group_id = None
        entity_manager.list_all = AsyncMock(
            side_effect=[
                [
                    SimpleNamespace(entity_type=EntityType.PATTERN),
                    SimpleNamespace(entity_type=EntityType.PATTERN),
                    SimpleNamespace(entity_type=EntityType.TASK),
                ],
                [
                    SimpleNamespace(entity_type=EntityType.PATTERN),
                    SimpleNamespace(entity_type=EntityType.TASK),
                ],
                [],
            ]
        )
        entity_manager.search = AsyncMock(return_value=[])

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    client=AsyncMock(),
                    entity_manager=entity_manager,
                    relationship_manager=AsyncMock(),
                )
            ),
        ):
            result = await health_check(organization_id=org_id)

        assert result.graph_connected is True
        assert result.entity_counts["pattern"] == 3
        assert result.entity_counts["task"] == 2
        assert result.entity_counts["episode"] == 0
        entity_manager.list_all.assert_has_awaits(
            [
                call(limit=1000, offset=0, include_archived=False),
                call(limit=1000, offset=3, include_archived=False),
                call(limit=1000, offset=5, include_archived=False),
            ]
        )
        entity_manager.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_stats_uses_paged_entity_counts(self) -> None:
        """get_stats should sum counts from paged entity listings."""
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = SimpleNamespace()
        entity_manager._driver = None
        entity_manager._group_id = None
        entity_manager.list_all = AsyncMock(
            side_effect=[
                [
                    SimpleNamespace(entity_type=EntityType.PATTERN),
                    SimpleNamespace(entity_type=EntityType.PATTERN),
                ],
                [
                    SimpleNamespace(entity_type=EntityType.PATTERN),
                    SimpleNamespace(entity_type=EntityType.TASK),
                    SimpleNamespace(entity_type=EntityType.TASK),
                ],
                [],
            ]
        )

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    client=AsyncMock(),
                    entity_manager=entity_manager,
                    relationship_manager=AsyncMock(),
                )
            ),
        ):
            stats = await get_stats(organization_id=org_id)

        assert stats["entities"]["pattern"] == 3
        assert stats["entities"]["task"] == 2
        assert stats["entities"]["episode"] == 0
        assert stats["total_entities"] == 5
        entity_manager.list_all.assert_has_awaits(
            [
                call(limit=1000, offset=0, include_archived=False),
                call(limit=1000, offset=2, include_archived=False),
                call(limit=1000, offset=5, include_archived=False),
            ]
        )


class TestRestoreBackup:
    """Graph restores should use the fast direct entity seams."""

    @pytest.mark.asyncio
    async def test_clean_restore_uses_bulk_direct_entity_insert(self) -> None:
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()
        driver = SimpleNamespace()
        entity_manager.bulk_create_direct = AsyncMock(return_value=(2, 0))
        entity_manager.create_direct = AsyncMock()
        relationship_manager.create_bulk = AsyncMock(return_value=(1, 0))
        relationship_manager.create = AsyncMock()
        backup_data = BackupData(
            version="2.0",
            created_at="2026-04-19T00:00:00Z",
            organization_id=org_id,
            entity_count=2,
            relationship_count=1,
            entities=[
                Entity(id="entity-1", entity_type=EntityType.PATTERN, name="One").model_dump(
                    mode="json"
                ),
                Entity(id="entity-2", entity_type=EntityType.TASK, name="Two").model_dump(
                    mode="json"
                ),
            ],
            relationships=[
                Relationship(
                    id="rel-1",
                    source_id="entity-1",
                    target_id="entity-2",
                    relationship_type=RelationshipType.RELATED_TO,
                ).model_dump(mode="json")
            ],
        )

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    client=SimpleNamespace(get_org_driver=lambda group_id: driver),
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            result = await restore_backup(
                backup_data,
                organization_id=org_id,
                skip_existing=False,
            )

        assert result.success is True
        assert result.entities_restored == 2
        assert result.entities_skipped == 0
        entity_manager.bulk_create_direct.assert_awaited_once()
        entity_manager.create_direct.assert_not_awaited()
        relationship_manager.create_bulk.assert_awaited_once()
        relationship_manager.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restore_rehydrates_typed_entities_losslessly(self) -> None:
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()
        entity_manager.bulk_create_direct = AsyncMock(return_value=(3, 0))
        relationship_manager.create_bulk = AsyncMock(return_value=(0, 0))
        task = Task(
            id="task-1",
            name="Ship packaging",
            title="Ship packaging",
            status=TaskStatus.DOING,
            priority=TaskPriority.CRITICAL,
            project_id="project-1",
            assignees=["nova"],
            technologies=["uv", "homebrew"],
        )
        project = Project(
            id="project-1",
            name="Sibyl",
            title="Sibyl",
            status=ProjectStatus.ACTIVE,
            repository_url="https://github.com/hyperb1iss/sibyl",
            tech_stack=["python", "surrealdb"],
        )
        epic = Epic(
            id="epic-1",
            name="Launch",
            title="Launch",
            project_id="project-1",
            priority=TaskPriority.HIGH,
        )
        backup_data = BackupData(
            version="2.0",
            created_at="2026-04-19T00:00:00Z",
            organization_id=org_id,
            entity_count=3,
            relationship_count=0,
            entities=[
                task.model_dump(mode="json"),
                project.model_dump(mode="json"),
                epic.model_dump(mode="json"),
            ],
            relationships=[],
        )

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    client=SimpleNamespace(get_org_driver=lambda group_id: SimpleNamespace()),
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            result = await restore_backup(
                backup_data,
                organization_id=org_id,
                skip_existing=False,
            )

        assert result.success is True
        restored = entity_manager.bulk_create_direct.await_args.args[0]
        assert [type(entity) for entity in restored] == [Task, Project, Epic]
        assert restored[0].status is TaskStatus.DOING
        assert restored[0].priority is TaskPriority.CRITICAL
        assert restored[0].project_id == "project-1"
        assert restored[0].assignees == ["nova"]
        assert restored[1].repository_url == "https://github.com/hyperb1iss/sibyl"
        assert restored[1].tech_stack == ["python", "surrealdb"]
        assert restored[2].project_id == "project-1"

    @pytest.mark.asyncio
    async def test_skip_existing_restore_uses_direct_create_without_embeddings(self) -> None:
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()
        driver = SimpleNamespace()
        entity_manager.get = AsyncMock(
            side_effect=[SimpleNamespace(id="entity-1"), Exception("missing")]
        )
        entity_manager.create_direct = AsyncMock()
        entity_manager.bulk_create_direct = AsyncMock()
        relationship_manager.create_bulk = AsyncMock(return_value=(0, 0))
        relationship_manager.create = AsyncMock()
        backup_data = BackupData(
            version="2.0",
            created_at="2026-04-19T00:00:00Z",
            organization_id=org_id,
            entity_count=2,
            relationship_count=0,
            entities=[
                Entity(id="entity-1", entity_type=EntityType.PATTERN, name="Existing").model_dump(
                    mode="json"
                ),
                Entity(
                    id="entity-2",
                    entity_type=EntityType.TASK,
                    name="Missing",
                    embedding=[0.1, 0.2, 0.3],
                ).model_dump(mode="json"),
            ],
            relationships=[],
        )

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    client=SimpleNamespace(get_org_driver=lambda group_id: driver),
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            result = await restore_backup(
                backup_data,
                organization_id=org_id,
                skip_existing=True,
            )

        assert result.success is True
        assert result.entities_restored == 1
        assert result.entities_skipped == 1
        entity_manager.bulk_create_direct.assert_not_awaited()
        entity_manager.create_direct.assert_awaited_once()
        relationship_manager.create_bulk.assert_not_awaited()
        create_args = entity_manager.create_direct.await_args
        assert create_args.args[0].id == "entity-2"
        assert create_args.kwargs == {"generate_embedding": False}

    @pytest.mark.asyncio
    async def test_restore_rehydrates_episodes_and_mentions(self) -> None:
        org_id = "00000000-0000-0000-0000-000000000111"
        client = SurrealGraphClient(group_id=org_id, url="memory://")
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=org_id)
        relationship_manager = RelationshipManager(client, group_id=org_id)
        backup_data = BackupData(
            version="2.0",
            created_at="2026-04-19T00:00:00Z",
            organization_id=org_id,
            entity_count=1,
            relationship_count=0,
            entities=[
                Entity(
                    id="entity-1",
                    entity_type=EntityType.TOPIC,
                    name="Mentioned entity",
                    organization_id=org_id,
                ).model_dump(mode="json")
            ],
            relationships=[],
            episode_count=1,
            mention_count=1,
            episodes=[
                {
                    "uuid": "episode-1",
                    "name": "Conversation",
                    "source": "message",
                    "source_description": "chat",
                    "content": "hi",
                    "created_at": "2026-04-19T00:00:00Z",
                    "valid_at": "2026-04-19T00:00:00Z",
                    "entity_edges": [],
                }
            ],
            mentions=[
                {
                    "uuid": "mention-1",
                    "source_id": "episode-1",
                    "target_id": "entity-1",
                    "created_at": "2026-04-19T00:00:00Z",
                }
            ],
        )

        try:
            with patch(
                "sibyl_core.tools.admin.get_graph_runtime",
                AsyncMock(
                    return_value=SimpleNamespace(
                        client=client,
                        entity_manager=entity_manager,
                        relationship_manager=relationship_manager,
                    )
                ),
            ):
                result = await restore_backup(
                    backup_data,
                    organization_id=org_id,
                    skip_existing=False,
                )

            episode_rows = normalize_records(
                await client.execute_query(
                    """
                    SELECT uuid, source, source_description, content, entity_edges
                    FROM episode
                    WHERE uuid = "episode-1";
                    """
                )
            )
            mention_rows = normalize_records(
                await client.execute_query(
                    """
                    SELECT uuid, in.uuid AS source_id, out.uuid AS target_id
                    FROM mentions
                    WHERE uuid = "mention-1";
                    """
                )
            )
        finally:
            await client.close()

        assert result.success is True
        assert result.episodes_restored == 1
        assert result.mentions_restored == 1
        assert episode_rows == [
            {
                "uuid": "episode-1",
                "source": "message",
                "source_description": "chat",
                "content": "hi",
                "entity_edges": [],
            }
        ]
        assert mention_rows == [
            {"uuid": "mention-1", "source_id": "episode-1", "target_id": "entity-1"}
        ]

    @pytest.mark.asyncio
    async def test_restore_round_trips_legacy_episode_mentions(self) -> None:
        org_id = "00000000-0000-0000-0000-000000000111"
        client = SurrealGraphClient(group_id=org_id, url="memory://")
        await prepare_graph_schema(client)
        entity_manager = EntityManager(client, group_id=org_id)
        relationship_manager = RelationshipManager(client, group_id=org_id)
        backup_data = BackupData(
            version="2.0",
            created_at="2026-04-19T00:00:00Z",
            organization_id=org_id,
            entity_count=1,
            relationship_count=0,
            entities=[
                Entity(
                    id="entity-legacy",
                    entity_type=EntityType.TOPIC,
                    name="Legacy entity",
                    organization_id=org_id,
                ).model_dump(mode="json")
            ],
            relationships=[],
            episode_count=1,
            mention_count=1,
            episodes=[
                {
                    "uuid": "episode-legacy",
                    "name": "Legacy conversation",
                    "source": "message",
                    "source_description": "chat",
                    "content": "legacy archive payload",
                    "created_at": "2026-04-19T00:00:00Z",
                    "valid_at": "2026-04-19T00:00:00Z",
                    "entity_edges": [],
                }
            ],
            mentions=[
                {
                    "uuid": "mention-legacy",
                    "source_node_uuid": "episode-legacy",
                    "target_node_uuid": "entity-legacy",
                    "created_at": "2026-04-19T00:00:00Z",
                }
            ],
        )

        try:
            with patch(
                "sibyl_core.tools.admin.get_graph_runtime",
                AsyncMock(
                    return_value=SimpleNamespace(
                        client=client,
                        entity_manager=entity_manager,
                        relationship_manager=relationship_manager,
                    )
                ),
            ):
                first_result = await restore_backup(
                    backup_data,
                    organization_id=org_id,
                    skip_existing=False,
                )
                second_result = await restore_backup(
                    backup_data,
                    organization_id=org_id,
                    skip_existing=True,
                )
                backup_result = await create_backup(organization_id=org_id)

            episode_rows = normalize_records(
                await client.execute_query(
                    """
                    SELECT uuid, source, source_description, content, group_id, entity_edges
                    FROM episode
                    WHERE uuid = "episode-legacy";
                    """
                )
            )
            mention_rows = normalize_records(
                await client.execute_query(
                    """
                    SELECT uuid, in.uuid AS source_id, out.uuid AS target_id, group_id
                    FROM mentions
                    WHERE uuid = "mention-legacy";
                    """
                )
            )
        finally:
            await client.close()

        assert first_result.success is True
        assert first_result.episodes_restored == 1
        assert first_result.mentions_restored == 1
        assert second_result.success is True
        assert second_result.episodes_restored == 0
        assert second_result.mentions_restored == 0
        assert second_result.episodes_skipped == 1
        assert second_result.mentions_skipped == 1
        assert episode_rows == [
            {
                "uuid": "episode-legacy",
                "source": "message",
                "source_description": "chat",
                "content": "legacy archive payload",
                "group_id": org_id,
                "entity_edges": [],
            }
        ]
        assert mention_rows == [
            {
                "uuid": "mention-legacy",
                "source_id": "episode-legacy",
                "target_id": "entity-legacy",
                "group_id": org_id,
            }
        ]
        assert backup_result.success is True
        assert backup_result.episode_count == 1
        assert backup_result.mention_count == 1
        assert backup_result.backup_data is not None
        assert backup_result.backup_data.mentions == [
            {
                "uuid": "mention-legacy",
                "source_id": "episode-legacy",
                "target_id": "entity-legacy",
                "group_id": org_id,
                "created_at": "2026-04-19T00:00:00+00:00",
            }
        ]

    @pytest.mark.asyncio
    async def test_restore_preserves_task_link_source_metadata(self) -> None:
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()
        entity_manager.bulk_create_direct = AsyncMock(return_value=(2, 0))
        relationship_manager.create_bulk = AsyncMock(return_value=(1, 0))
        relationship_manager.create = AsyncMock()
        backup_data = BackupData(
            version="2.0",
            created_at="2026-04-19T00:00:00Z",
            organization_id=org_id,
            entity_count=2,
            relationship_count=1,
            entities=[
                Entity(
                    id="project-1",
                    entity_type=EntityType.PROJECT,
                    name="Project",
                    metadata={"source_ids": ["source:project"]},
                ).model_dump(mode="json"),
                Entity(
                    id="task-1",
                    entity_type=EntityType.TASK,
                    name="Task",
                    metadata={"source_ids": ["source:task"]},
                ).model_dump(mode="json"),
            ],
            relationships=[
                Relationship(
                    id="task-project",
                    source_id="task-1",
                    target_id="project-1",
                    relationship_type=RelationshipType.BELONGS_TO,
                    metadata={"source_ids": ["source:task"]},
                ).model_dump(mode="json")
            ],
        )

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    client=SimpleNamespace(get_org_driver=lambda group_id: SimpleNamespace()),
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            result = await restore_backup(
                backup_data,
                organization_id=org_id,
                skip_existing=False,
            )

        assert result.success is True
        restored_entities = entity_manager.bulk_create_direct.await_args.args[0]
        restored_relationships = relationship_manager.create_bulk.await_args.args[0]
        assert [entity.id for entity in restored_entities] == ["project-1", "task-1"]
        assert restored_entities[1].metadata["source_ids"] == ["source:task"]
        assert restored_relationships[0].source_id == "task-1"
        assert restored_relationships[0].target_id == "project-1"
        assert restored_relationships[0].relationship_type is RelationshipType.BELONGS_TO
        assert restored_relationships[0].metadata["source_ids"] == ["source:task"]


class TestBackfillTaskProjectRelationships:
    """Project validation should page through the full project set."""

    @pytest.mark.asyncio
    async def test_backfill_task_project_relationships_pages_task_batches(self) -> None:
        """Tasks beyond the first page should still be processed."""
        org_id = "00000000-0000-0000-0000-000000000111"
        client = AsyncMock()
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        page_size = 2
        task_page_one = [
            SimpleNamespace(id="task-1", metadata={"project_id": "project-1"}),
            SimpleNamespace(id="task-2", metadata={"project_id": "project-1"}),
        ]
        task_page_two = [SimpleNamespace(id="task-3", metadata={"project_id": "project-1"})]
        project_page_one = [SimpleNamespace(id="project-1")]

        async def list_by_type(
            entity_type: EntityType, limit: int = 50, offset: int = 0, **_: object
        ) -> list[SimpleNamespace]:
            assert limit == page_size
            if entity_type == EntityType.TASK:
                if offset == 0:
                    return task_page_one
                if offset == page_size:
                    return task_page_two
                return []
            if entity_type == EntityType.PROJECT:
                if offset == 0:
                    return project_page_one
                if offset == page_size:
                    return []
            raise AssertionError(f"Unexpected page request: {entity_type=} {offset=}")

        entity_manager.list_by_type = AsyncMock(side_effect=list_by_type)
        relationship_manager.get_for_entity = AsyncMock(return_value=[])

        with (
            patch(
                "sibyl_core.tools.admin.get_graph_runtime",
                AsyncMock(
                    return_value=SimpleNamespace(
                        client=client,
                        entity_manager=entity_manager,
                        relationship_manager=relationship_manager,
                    )
                ),
            ),
            patch("sibyl_core.tools.admin.BACKFILL_PAGE_SIZE", page_size),
        ):
            result = await backfill_task_project_relationships(organization_id=org_id, dry_run=True)

        assert result.relationships_created == 3
        assert result.errors == []
        assert result.tasks_without_project == 0
        assert result.tasks_already_linked == 0
        assert relationship_manager.get_for_entity.await_count == 3
        entity_manager.list_by_type.assert_has_awaits(
            [
                call(EntityType.TASK, limit=page_size, offset=0),
                call(EntityType.TASK, limit=page_size, offset=page_size),
                call(EntityType.PROJECT, limit=page_size, offset=0, include_archived=True),
            ],
            any_order=False,
        )

    @pytest.mark.asyncio
    async def test_backfill_task_project_relationships_pages_project_validation(self) -> None:
        """Projects beyond the first 1000 should still validate task metadata."""
        org_id = "00000000-0000-0000-0000-000000000111"
        client = AsyncMock()
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        task = SimpleNamespace(id="task-1", metadata={"project_id": "project-1001"})
        first_page = [SimpleNamespace(id=f"project-{i}") for i in range(1000)]
        second_page = [SimpleNamespace(id="project-1001")]
        entity_manager.list_by_type = AsyncMock(side_effect=[[task], first_page, second_page, []])
        relationship_manager.get_for_entity = AsyncMock(return_value=[])

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    client=client,
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            result = await backfill_task_project_relationships(organization_id=org_id, dry_run=True)

        assert result.relationships_created == 1
        assert result.errors == []
        assert result.tasks_without_project == 0
        assert result.tasks_already_linked == 0
        entity_manager.list_by_type.assert_any_await(
            ANY, limit=1000, offset=0, include_archived=True
        )
        entity_manager.list_by_type.assert_any_await(
            ANY, limit=1000, offset=1000, include_archived=True
        )


class TestBackfillProjectIdFromRelationships:
    """project_id property backfill should use runtime seams."""

    @pytest.mark.asyncio
    async def test_pages_entities_and_projects_via_runtime(self) -> None:
        """Missing project ids should resolve from BELONGS_TO relationships."""
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        page_size = 2
        project_page_one = [SimpleNamespace(id="project-1"), SimpleNamespace(id="project-2")]
        entity_page_one = [
            SimpleNamespace(id="task-1", name="Task 1", project_id="project-1", metadata={}),
            SimpleNamespace(id="task-2", name="Task 2", project_id=None, metadata={}),
        ]
        entity_page_two = [
            SimpleNamespace(id="task-3", name="Task 3", project_id=None, metadata={}),
        ]

        async def list_by_type(
            entity_type: EntityType, limit: int = 50, offset: int = 0, **_: object
        ) -> list[SimpleNamespace]:
            assert entity_type == EntityType.PROJECT
            assert limit == page_size
            if offset == 0:
                return project_page_one
            return []

        async def list_all(limit: int = 50, offset: int = 0, **_: object) -> list[SimpleNamespace]:
            assert limit == page_size
            if offset == 0:
                return entity_page_one
            if offset == page_size:
                return entity_page_two
            return []

        entity_manager.list_by_type = AsyncMock(side_effect=list_by_type)
        entity_manager.list_all = AsyncMock(side_effect=list_all)
        entity_manager.update = AsyncMock()
        relationship_manager.get_for_entity = AsyncMock(
            side_effect=[
                [SimpleNamespace(target_id="project-2")],
                [],
            ]
        )

        with (
            patch(
                "sibyl_core.tools.admin.get_graph_runtime",
                AsyncMock(
                    return_value=SimpleNamespace(
                        entity_manager=entity_manager,
                        relationship_manager=relationship_manager,
                    )
                ),
            ),
            patch("sibyl_core.tools.admin.BACKFILL_PAGE_SIZE", page_size),
        ):
            result = await backfill_project_id_from_relationships(
                organization_id=org_id,
                dry_run=True,
            )

        assert result.success is True
        assert result.nodes_updated == 1
        assert result.nodes_already_set == 1
        assert result.nodes_without_project_rel == 1
        entity_manager.list_by_type.assert_has_awaits(
            [
                call(
                    EntityType.PROJECT,
                    limit=page_size,
                    offset=0,
                    include_archived=True,
                ),
                call(
                    EntityType.PROJECT,
                    limit=page_size,
                    offset=len(project_page_one),
                    include_archived=True,
                ),
            ]
        )
        entity_manager.list_all.assert_has_awaits(
            [
                call(limit=page_size, offset=0, include_archived=True),
                call(limit=page_size, offset=page_size, include_archived=True),
                call(
                    limit=page_size,
                    offset=page_size + len(entity_page_two),
                    include_archived=True,
                ),
            ]
        )
        relationship_manager.get_for_entity.assert_has_awaits(
            [
                call(
                    "task-2",
                    relationship_types=[RelationshipType.BELONGS_TO],
                    direction="outgoing",
                ),
                call(
                    "task-3",
                    relationship_types=[RelationshipType.BELONGS_TO],
                    direction="outgoing",
                ),
            ]
        )
        entity_manager.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_updates_nodes_via_entity_manager(self) -> None:
        """Non-dry-run backfill should persist project ids through entity updates."""
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        entity_manager.list_by_type = AsyncMock(
            side_effect=[
                [SimpleNamespace(id="project-1")],
                [],
            ]
        )
        entity_manager.list_all = AsyncMock(
            side_effect=[
                [SimpleNamespace(id="episode-1", name="Episode 1", project_id=None, metadata={})],
                [],
            ]
        )
        entity_manager.update = AsyncMock()
        relationship_manager.get_for_entity = AsyncMock(
            return_value=[SimpleNamespace(target_id="project-1")]
        )

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            result = await backfill_project_id_from_relationships(
                organization_id=org_id,
                dry_run=False,
            )

        assert result.success is True
        assert result.nodes_updated == 1
        assert result.nodes_already_set == 1
        entity_manager.update.assert_awaited_once_with(
            "episode-1",
            {"project_id": "project-1"},
        )


class TestBackfillSharedProjectRelationships:
    """Shared project backfill should use runtime seams."""

    @pytest.mark.asyncio
    async def test_pages_orphans_and_counts_dry_run(self) -> None:
        """Dry-run shared project backfill should classify orphans through entity seams."""
        org_id = "00000000-0000-0000-0000-000000000111"
        shared_project_id = "project_shared_00000000"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        page_size = 2
        entity_manager.get = AsyncMock(return_value=SimpleNamespace(id=shared_project_id))
        entity_manager.list_all = AsyncMock(
            side_effect=[
                [
                    SimpleNamespace(
                        id="task-1", entity_type=EntityType.TASK, project_id=None, metadata={}
                    ),
                    SimpleNamespace(
                        id="topic-1",
                        entity_type=EntityType.TOPIC,
                        project_id="project-1",
                        metadata={},
                    ),
                ],
                [
                    SimpleNamespace(
                        id="episode-1", entity_type=EntityType.EPISODE, project_id=None, metadata={}
                    ),
                ],
                [],
            ]
        )
        entity_manager.update = AsyncMock()
        relationship_manager.create = AsyncMock()

        with (
            patch(
                "sibyl_core.tools.admin.get_graph_runtime",
                AsyncMock(
                    return_value=SimpleNamespace(
                        entity_manager=entity_manager,
                        relationship_manager=relationship_manager,
                    )
                ),
            ),
            patch("sibyl_core.tools.admin.BACKFILL_PAGE_SIZE", page_size),
        ):
            result = await backfill_shared_project(
                organization_id=org_id,
                shared_project_graph_id=shared_project_id,
                dry_run=True,
            )

        assert result.success is True
        assert result.graph_entity_created is False
        assert result.entities_updated == 2
        assert result.entities_already_set == 1
        entity_manager.list_all.assert_has_awaits(
            [
                call(limit=page_size, offset=0, include_archived=True),
                call(limit=page_size, offset=page_size, include_archived=True),
                call(limit=page_size, offset=page_size + 1, include_archived=True),
            ]
        )
        entity_manager.update.assert_not_awaited()
        relationship_manager.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_updates_entities_and_creates_belongs_to_links(self) -> None:
        """Real shared-project backfill should persist project ids and link task-like entities."""
        org_id = "00000000-0000-0000-0000-000000000111"
        shared_project_id = "project_shared_00000000"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        entity_manager.get = AsyncMock(return_value=None)
        entity_manager.create_direct = AsyncMock()
        entity_manager.list_all = AsyncMock(
            side_effect=[
                [
                    SimpleNamespace(
                        id="task-1", entity_type=EntityType.TASK, project_id=None, metadata={}
                    ),
                    SimpleNamespace(
                        id="pattern-1", entity_type=EntityType.PATTERN, project_id=None, metadata={}
                    ),
                ],
                [],
            ]
        )
        entity_manager.update = AsyncMock()
        relationship_manager.create = AsyncMock()

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            result = await backfill_shared_project(
                organization_id=org_id,
                shared_project_graph_id=shared_project_id,
                dry_run=False,
            )
        assert result.success is True
        assert result.graph_entity_created is True
        assert result.graph_entity_id == shared_project_id
        assert result.entities_updated == 2
        assert result.entities_already_set == 2
        entity_manager.create_direct.assert_awaited_once()
        entity_manager.update.assert_has_awaits(
            [
                call("task-1", {"project_id": shared_project_id}),
                call("pattern-1", {"project_id": shared_project_id}),
            ]
        )
        relationship_manager.create.assert_awaited_once()
        relationship = relationship_manager.create.await_args.args[0]
        assert relationship.relationship_type == RelationshipType.BELONGS_TO
        assert relationship.source_id == "task-1"
        assert relationship.target_id == shared_project_id


class TestBackfillEpisodeTaskRelationships:
    """Episode/task relationship backfill should use runtime seams."""

    @pytest.mark.asyncio
    async def test_pages_episode_batches_and_counts_states(self) -> None:
        """Episodes should be paged and classified through entity/relationship seams."""
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        page_size = 2
        episode_page_one = [
            SimpleNamespace(id="episode-1", metadata={"task_id": "task-1"}),
            SimpleNamespace(id="episode-2", metadata={"task_id": "task-2"}),
        ]
        episode_page_two = [SimpleNamespace(id="episode-3", metadata={"task_id": "task-3"})]

        async def list_by_type(
            entity_type: EntityType, limit: int = 50, offset: int = 0, **_: object
        ) -> list[SimpleNamespace]:
            assert entity_type == EntityType.EPISODE
            assert limit == page_size
            if offset == 0:
                return episode_page_one
            if offset == page_size:
                return episode_page_two
            return []

        entity_manager.list_by_type = AsyncMock(side_effect=list_by_type)
        entity_manager.get = AsyncMock(
            side_effect=lambda task_id: None if task_id == "task-3" else SimpleNamespace(id=task_id)
        )
        relationship_manager.get_for_entity = AsyncMock(
            side_effect=[
                [],
                [SimpleNamespace(source_id="episode-2", target_id="task-2")],
                [],
            ]
        )
        relationship_manager.create = AsyncMock()

        with (
            patch(
                "sibyl_core.tools.admin.get_graph_runtime",
                AsyncMock(
                    return_value=SimpleNamespace(
                        entity_manager=entity_manager,
                        relationship_manager=relationship_manager,
                    )
                ),
            ),
            patch("sibyl_core.tools.admin.BACKFILL_PAGE_SIZE", page_size),
        ):
            result = await backfill_episode_task_relationships(
                organization_id=org_id,
                dry_run=True,
            )

        assert result.relationships_created == 1
        assert result.episodes_already_linked == 1
        assert result.episodes_without_task == 1
        entity_manager.list_by_type.assert_has_awaits(
            [
                call(
                    EntityType.EPISODE,
                    limit=page_size,
                    offset=0,
                    include_archived=True,
                ),
                call(
                    EntityType.EPISODE,
                    limit=page_size,
                    offset=page_size,
                    include_archived=True,
                ),
                call(
                    EntityType.EPISODE,
                    limit=page_size,
                    offset=page_size + len(episode_page_two),
                    include_archived=True,
                ),
            ]
        )
        relationship_manager.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_related_to_relationships_via_manager(self) -> None:
        """Backfill should create RELATED_TO edges through the relationship manager."""
        org_id = "00000000-0000-0000-0000-000000000111"
        entity_manager = AsyncMock()
        relationship_manager = AsyncMock()

        entity_manager.list_by_type = AsyncMock(
            side_effect=[
                [SimpleNamespace(id="episode-1", metadata={"task_id": "task-1"})],
                [],
            ]
        )
        entity_manager.get = AsyncMock(return_value=SimpleNamespace(id="task-1"))
        relationship_manager.get_for_entity = AsyncMock(return_value=[])
        relationship_manager.create = AsyncMock()

        with patch(
            "sibyl_core.tools.admin.get_graph_runtime",
            AsyncMock(
                return_value=SimpleNamespace(
                    entity_manager=entity_manager,
                    relationship_manager=relationship_manager,
                )
            ),
        ):
            result = await backfill_episode_task_relationships(
                organization_id=org_id,
                dry_run=False,
            )

        assert result.relationships_created == 1
        relationship_manager.create.assert_awaited_once()
        relationship = relationship_manager.create.await_args.args[0]
        assert relationship.relationship_type == RelationshipType.RELATED_TO
        assert relationship.source_id == "episode-1"
        assert relationship.target_id == "task-1"
        assert relationship.metadata == {"backfilled": True}
