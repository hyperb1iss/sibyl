import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.retrieval.dedup import DuplicatePair


def _load_consolidation_module():
    consolidation_spec = importlib.util.spec_from_file_location(
        "test_jobs_consolidation_module",
        Path(__file__).resolve().parents[1] / "src" / "sibyl" / "jobs" / "consolidation.py",
    )
    assert consolidation_spec is not None
    assert consolidation_spec.loader is not None

    consolidation_module = importlib.util.module_from_spec(consolidation_spec)
    consolidation_spec.loader.exec_module(consolidation_module)
    return consolidation_module


consolidation_module = _load_consolidation_module()


def _pair(
    entity1_id: str,
    entity2_id: str,
    suggested_keep: str,
) -> DuplicatePair:
    return DuplicatePair(
        entity1_id=entity1_id,
        entity2_id=entity2_id,
        similarity=0.97,
        entity1_name=entity1_id,
        entity2_name=entity2_id,
        entity_type="pattern",
        suggested_keep=suggested_keep,
    )


@pytest.mark.asyncio
async def test_consolidate_org_wires_config_and_respects_merge_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sibyl_core.retrieval.dedup as dedup_module

    client = MagicMock()
    entity_manager = MagicMock()
    graph_runtime = SimpleNamespace(client=client, entity_manager=entity_manager)
    config = object()
    deduplicator = MagicMock()
    deduplicator.find_duplicates = AsyncMock(
        return_value=[
            _pair("keep-1", "remove-1", suggested_keep="keep-1"),
            _pair("remove-2", "keep-2", suggested_keep="keep-2"),
            _pair("keep-3", "remove-3", suggested_keep="keep-3"),
        ]
    )
    deduplicator.merge_entities = AsyncMock(side_effect=[True, False])

    get_graph_runtime = AsyncMock(return_value=graph_runtime)
    dedup_config_cls = MagicMock(return_value=config)
    deduplicator_cls = MagicMock(return_value=deduplicator)

    monkeypatch.setattr(consolidation_module, "_get_graph_runtime", get_graph_runtime)
    monkeypatch.setattr(dedup_module, "DedupConfig", dedup_config_cls)
    monkeypatch.setattr(dedup_module, "EntityDeduplicator", deduplicator_cls)

    result = await consolidation_module.consolidate_org(
        {},
        group_id="org-123",
        similarity_threshold=0.91,
        max_merges_per_run=2,
    )

    assert result == {
        "group_id": "org-123",
        "duplicates_found": 3,
        "merges_completed": 1,
        "merges_failed": 1,
        "merges_skipped": 1,
    }
    get_graph_runtime.assert_awaited_once_with("org-123")
    dedup_config_cls.assert_called_once_with(
        similarity_threshold=0.91,
        same_type_only=True,
        min_name_overlap=0.3,
    )
    deduplicator_cls.assert_called_once_with(
        client=client,
        entity_manager=entity_manager,
        config=config,
    )
    deduplicator.find_duplicates.assert_awaited_once_with()
    assert deduplicator.merge_entities.await_args_list == [
        call(keep_id="keep-1", remove_id="remove-1", merge_metadata=True),
        call(keep_id="keep-2", remove_id="remove-2", merge_metadata=True),
    ]


@pytest.mark.asyncio
async def test_consolidate_org_returns_zeroes_when_no_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sibyl_core.retrieval.dedup as dedup_module

    deduplicator = MagicMock()
    deduplicator.find_duplicates = AsyncMock(return_value=[])
    deduplicator.merge_entities = AsyncMock()

    monkeypatch.setattr(
        consolidation_module,
        "_get_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                client=MagicMock(),
                entity_manager=MagicMock(),
            )
        ),
    )
    monkeypatch.setattr(dedup_module, "DedupConfig", MagicMock(return_value=object()))
    monkeypatch.setattr(dedup_module, "EntityDeduplicator", MagicMock(return_value=deduplicator))

    result = await consolidation_module.consolidate_org({}, group_id="org-123")

    assert result == {
        "group_id": "org-123",
        "duplicates_found": 0,
        "merges_completed": 0,
        "merges_failed": 0,
        "merges_skipped": 0,
    }
    deduplicator.find_duplicates.assert_awaited_once_with()
    deduplicator.merge_entities.assert_not_awaited()


@pytest.mark.asyncio
async def test_consolidate_org_counts_merge_exceptions_as_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sibyl_core.retrieval.dedup as dedup_module

    deduplicator = MagicMock()
    deduplicator.find_duplicates = AsyncMock(
        return_value=[_pair("keep-1", "remove-1", suggested_keep="keep-1")]
    )
    deduplicator.merge_entities = AsyncMock(side_effect=RuntimeError("boom"))

    monkeypatch.setattr(
        consolidation_module,
        "_get_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                client=MagicMock(),
                entity_manager=MagicMock(),
            )
        ),
    )
    monkeypatch.setattr(dedup_module, "DedupConfig", MagicMock(return_value=object()))
    monkeypatch.setattr(dedup_module, "EntityDeduplicator", MagicMock(return_value=deduplicator))

    result = await consolidation_module.consolidate_org({}, group_id="org-123")

    assert result == {
        "group_id": "org-123",
        "duplicates_found": 1,
        "merges_completed": 0,
        "merges_failed": 1,
        "merges_skipped": 0,
    }
    deduplicator.find_duplicates.assert_awaited_once_with()
    deduplicator.merge_entities.assert_awaited_once_with(
        keep_id="keep-1",
        remove_id="remove-1",
        merge_metadata=True,
    )


@pytest.mark.asyncio
async def test_priority_decay_archives_only_old_unarchived_episodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)

    async def list_by_type(
        entity_type: EntityType,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[Entity]:
        del entity_type, limit, include_archived
        if offset != 0:
            return []
        return [
            Entity(
                id="episode-old",
                entity_type=EntityType.EPISODE,
                name="Old episode",
                created_at=now - timedelta(days=200),
            ),
            Entity(
                id="episode-archived",
                entity_type=EntityType.EPISODE,
                name="Archived episode",
                created_at=now - timedelta(days=220),
                metadata={"status": "archived"},
            ),
            Entity(
                id="episode-fresh",
                entity_type=EntityType.EPISODE,
                name="Fresh episode",
                created_at=now - timedelta(days=20),
            ),
        ]

    entity_manager = AsyncMock()
    entity_manager.list_by_type = AsyncMock(side_effect=list_by_type)
    entity_manager.update = AsyncMock(return_value=object())

    monkeypatch.setattr(
        consolidation_module,
        "_get_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                client=MagicMock(),
                entity_manager=entity_manager,
                relationship_manager=AsyncMock(),
            )
        ),
    )

    result = await consolidation_module.priority_decay(
        {},
        group_id="org-123",
        max_archives_per_run=10,
        entity_types=(EntityType.EPISODE,),
    )

    assert result == {
        "group_id": "org-123",
        "candidates_found": 1,
        "archived": 1,
        "min_age_days": 180,
    }
    assert entity_manager.list_by_type.await_args_list == [
        call(EntityType.EPISODE, limit=200, offset=0, include_archived=False),
        call(EntityType.EPISODE, limit=200, offset=3, include_archived=False),
    ]
    entity_manager.update.assert_awaited_once()
    assert entity_manager.update.await_args.args[0] == "episode-old"
    assert entity_manager.update.await_args.args[1]["status"] == "archived"
    assert "archived_at" in entity_manager.update.await_args.args[1]


@pytest.mark.asyncio
async def test_priority_decay_defaults_to_derived_memory_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    called_types: list[EntityType] = []

    def entity(entity_id: str, entity_type: EntityType) -> Entity:
        return Entity(
            id=entity_id,
            entity_type=entity_type,
            name=entity_id,
            created_at=now - timedelta(days=240),
            metadata={"importance": 0.10},
        )

    async def list_by_type(
        entity_type: EntityType,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[Entity]:
        del limit, include_archived
        called_types.append(entity_type)
        if offset != 0:
            return []
        if entity_type is EntityType.CLAIM:
            return [entity("claim-derived", EntityType.CLAIM)]
        if entity_type is EntityType.IDEA:
            return [entity("idea-derived", EntityType.IDEA)]
        return []

    entity_manager = AsyncMock()
    entity_manager.list_by_type = AsyncMock(side_effect=list_by_type)
    entity_manager.update = AsyncMock(return_value=object())

    monkeypatch.setattr(
        consolidation_module,
        "_get_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                client=MagicMock(),
                entity_manager=entity_manager,
                relationship_manager=AsyncMock(),
            )
        ),
    )

    result = await consolidation_module.priority_decay(
        {},
        group_id="org-123",
        max_archives_per_run=10,
    )

    assert result["candidates_found"] == 2
    assert result["archived"] == 2
    assert set(consolidation_module._PRIORITY_DECAY_ENTITY_TYPES) <= set(called_types)
    assert EntityType.SOURCE not in called_types
    assert EntityType.DOCUMENT not in called_types
    assert EntityType.SESSION not in called_types
    assert EntityType.TASK not in called_types
    assert EntityType.PROJECT not in called_types
    assert sorted(await_call.args[0] for await_call in entity_manager.update.await_args_list) == [
        "claim-derived",
        "idea-derived",
    ]


@pytest.mark.asyncio
async def test_priority_decay_respects_archive_cap_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)

    def episode(entity_id: str, age_days: int) -> Entity:
        return Entity(
            id=entity_id,
            entity_type=EntityType.EPISODE,
            name=entity_id,
            created_at=now - timedelta(days=age_days),
        )

    entity_manager = AsyncMock()
    entity_manager.list_by_type = AsyncMock(
        side_effect=[
            [episode("episode-1", 240), episode("episode-2", 220)],
            [episode("episode-3", 200)],
            [],
        ]
    )
    entity_manager.update = AsyncMock(return_value=object())

    monkeypatch.setattr(
        consolidation_module,
        "_get_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                client=MagicMock(),
                entity_manager=entity_manager,
                relationship_manager=AsyncMock(),
            )
        ),
    )

    result = await consolidation_module.priority_decay(
        {},
        group_id="org-123",
        max_archives_per_run=3,
        entity_types=(EntityType.EPISODE,),
    )

    assert result["candidates_found"] == 3
    assert result["archived"] == 3
    assert entity_manager.list_by_type.await_args_list == [
        call(EntityType.EPISODE, limit=200, offset=0, include_archived=False),
        call(EntityType.EPISODE, limit=200, offset=2, include_archived=False),
        call(EntityType.EPISODE, limit=200, offset=3, include_archived=False),
    ]
    assert [await_call.args[0] for await_call in entity_manager.update.await_args_list] == [
        "episode-1",
        "episode-2",
        "episode-3",
    ]


@pytest.mark.asyncio
async def test_priority_decay_scores_importance_recency_and_supersession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)

    def episode(entity_id: str, metadata: dict[str, object]) -> Entity:
        return Entity(
            id=entity_id,
            entity_type=EntityType.EPISODE,
            name=entity_id,
            created_at=now - timedelta(days=240),
            updated_at=now - timedelta(days=240),
            metadata=metadata,
        )

    entity_manager = AsyncMock()
    entity_manager.list_by_type = AsyncMock(
        side_effect=[
            [
                episode("low-importance", {"importance": 0.15}),
                episode("important", {"importance": 0.98}),
                episode(
                    "recently-used",
                    {
                        "importance": 0.40,
                        "last_recalled_at": (now - timedelta(days=3)).isoformat(),
                        "retrieval_count": 1,
                    },
                ),
                episode("superseded", {"importance": 0.95, "lifecycle_state": "superseded"}),
                episode("pinned", {"importance": 0.05, "retention": "pinned"}),
            ],
            [],
        ]
    )
    entity_manager.update = AsyncMock(return_value=object())

    monkeypatch.setattr(
        consolidation_module,
        "_get_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                client=MagicMock(),
                entity_manager=entity_manager,
                relationship_manager=AsyncMock(),
            )
        ),
    )

    result = await consolidation_module.priority_decay(
        {},
        group_id="org-123",
        max_archives_per_run=10,
        decay_threshold=0.35,
        recency_half_life_days=180,
        entity_types=(EntityType.EPISODE,),
    )

    assert result["candidates_found"] == 2
    assert result["archived"] == 2
    assert [await_call.args[0] for await_call in entity_manager.update.await_args_list] == [
        "low-importance",
        "superseded",
    ]
    low_updates = entity_manager.update.await_args_list[0].args[1]
    superseded_updates = entity_manager.update.await_args_list[1].args[1]
    assert low_updates["decay_reason"] == "low_priority_decay_score"
    assert superseded_updates["decay_reason"] == "superseded_or_stale"
    assert low_updates["decay_score"] < low_updates["decay_threshold"]
    assert superseded_updates["decay_score"] < superseded_updates["decay_threshold"]


@pytest.mark.asyncio
async def test_priority_decay_protects_cited_twin_before_age_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)

    def episode(entity_id: str, metadata: dict[str, object]) -> Entity:
        return Entity(
            id=entity_id,
            entity_type=EntityType.EPISODE,
            name=entity_id,
            created_at=now - timedelta(days=240),
            updated_at=now - timedelta(days=240),
            metadata={"importance": 0.10, **metadata},
        )

    entity_manager = AsyncMock()
    entity_manager.list_by_type = AsyncMock(
        side_effect=[
            [
                episode("uncited", {}),
                episode(
                    "cited",
                    {
                        "last_used_at": (now - timedelta(days=1)).isoformat(),
                        "citation_count": 1,
                    },
                ),
                episode(
                    "recalled",
                    {
                        "last_recalled_at": (now - timedelta(days=10)).isoformat(),
                        "retrieval_count": 3,
                    },
                ),
            ],
            [],
        ]
    )
    entity_manager.update = AsyncMock(return_value=object())

    monkeypatch.setattr(
        consolidation_module,
        "_get_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                client=MagicMock(),
                entity_manager=entity_manager,
                relationship_manager=AsyncMock(),
            )
        ),
    )

    result = await consolidation_module.priority_decay(
        {},
        group_id="org-123",
        max_archives_per_run=10,
        decay_threshold=0.35,
        recency_half_life_days=180,
        entity_types=(EntityType.EPISODE,),
    )

    assert result["candidates_found"] == 1
    assert result["archived"] == 1
    entity_manager.update.assert_awaited_once()
    assert entity_manager.update.await_args.args[0] == "uncited"


def test_priority_decay_scores_citation_above_exposure_above_untouched() -> None:
    now = datetime.now(UTC)

    def episode(entity_id: str, metadata: dict[str, object]) -> Entity:
        return Entity(
            id=entity_id,
            entity_type=EntityType.EPISODE,
            name=entity_id,
            created_at=now - timedelta(days=420),
            metadata={"importance": 0.50, **metadata},
        )

    untouched = episode("untouched", {})
    exposed = episode(
        "exposed",
        {
            "last_recalled_at": (now - timedelta(days=2)).isoformat(),
            "retrieval_count": 1,
        },
    )
    cited = episode(
        "cited",
        {
            "citation_count": 1,
            "last_used_at": (now - timedelta(days=2)).isoformat(),
        },
    )
    legacy_only = episode(
        "legacy-only",
        {"last_accessed_at": (now - timedelta(days=2)).isoformat()},
    )
    legacy_capped = episode(
        "legacy-capped",
        {
            "citation_count": 1,
            "last_accessed_at": (now - timedelta(days=2)).isoformat(),
            "last_used_at": (now - timedelta(days=180)).isoformat(),
        },
    )

    untouched_score = consolidation_module._priority_decay_score(
        untouched,
        now=now,
        recency_half_life_days=180,
    )
    exposed_score = consolidation_module._priority_decay_score(
        exposed,
        now=now,
        recency_half_life_days=180,
    )
    cited_score = consolidation_module._priority_decay_score(
        cited,
        now=now,
        recency_half_life_days=180,
    )
    legacy_only_score = consolidation_module._priority_decay_score(
        legacy_only,
        now=now,
        recency_half_life_days=180,
    )
    legacy_capped_score = consolidation_module._priority_decay_score(
        legacy_capped,
        now=now,
        recency_half_life_days=180,
    )

    assert cited_score > exposed_score > legacy_only_score > untouched_score
    assert consolidation_module._entity_last_seen_at(legacy_capped) == now - timedelta(days=180)
    assert exposed_score > legacy_capped_score > untouched_score


@pytest.mark.asyncio
async def test_priority_decay_orders_candidates_by_usage_before_age_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)

    def episode(
        entity_id: str,
        age_days: int,
        metadata: dict[str, object],
        *,
        aware_created_at: bool = True,
    ) -> Entity:
        created_at = now - timedelta(days=age_days)
        if not aware_created_at:
            created_at = created_at.replace(tzinfo=None)
        return Entity(
            id=entity_id,
            entity_type=EntityType.EPISODE,
            name=entity_id,
            created_at=created_at,
            updated_at=now - timedelta(days=age_days),
            metadata={"importance": 0.10, **metadata},
        )

    entity_manager = AsyncMock()
    entity_manager.list_by_type = AsyncMock(
        side_effect=[
            [
                episode(
                    "retrieved-oldest",
                    800,
                    {
                        "last_used_at": (now - timedelta(days=500)).isoformat(),
                        "retrieval_count": 5,
                    },
                ),
                episode("unused-younger", 240, {}),
                episode("unused-older", 420, {}, aware_created_at=False),
            ],
            [],
        ]
    )
    entity_manager.update = AsyncMock(return_value=object())

    monkeypatch.setattr(
        consolidation_module,
        "_get_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                client=MagicMock(),
                entity_manager=entity_manager,
                relationship_manager=AsyncMock(),
            )
        ),
    )

    result = await consolidation_module.priority_decay(
        {},
        group_id="org-123",
        max_archives_per_run=2,
        decay_threshold=0.80,
        recency_half_life_days=180,
        entity_types=(EntityType.EPISODE,),
    )

    assert result["candidates_found"] == 2
    assert result["archived"] == 2
    assert [await_call.args[0] for await_call in entity_manager.update.await_args_list] == [
        "unused-older",
        "unused-younger",
    ]


@pytest.mark.asyncio
async def test_list_organization_ids_uses_runtime_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl.persistence import organization_runtime

    dispatched = AsyncMock(return_value=["org-1", "org-2"])
    monkeypatch.setattr(organization_runtime, "list_org_ids", dispatched)

    result = await consolidation_module._list_organization_ids()

    dispatched.assert_awaited_once_with()
    assert result == ["org-1", "org-2"]


@pytest.mark.asyncio
async def test_consolidate_all_orgs_uses_surreal_org_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_org_ids = AsyncMock(return_value=["org-1", "org-2"])
    monkeypatch.setattr(consolidation_module, "_list_organization_ids", list_org_ids)
    monkeypatch.setattr(
        consolidation_module,
        "consolidate_org",
        AsyncMock(side_effect=lambda ctx, group_id: {"group_id": group_id, "ok": True}),
    )
    monkeypatch.setattr(
        consolidation_module,
        "priority_decay",
        AsyncMock(side_effect=lambda ctx, group_id: {"group_id": group_id, "ok": True}),
    )

    result = await consolidation_module.consolidate_all_orgs({})

    list_org_ids.assert_awaited_once_with()
    assert result == {
        "orgs_processed": 2,
        "orgs_succeeded": 2,
        "orgs_failed": 0,
    }
