import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

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
    import sibyl_core.graph.client as graph_client_module
    import sibyl_core.graph.entities as entities_module
    import sibyl_core.retrieval.dedup as dedup_module

    client = MagicMock()
    entity_manager = MagicMock()
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

    get_graph_client = AsyncMock(return_value=client)
    entity_manager_cls = MagicMock(return_value=entity_manager)
    dedup_config_cls = MagicMock(return_value=config)
    deduplicator_cls = MagicMock(return_value=deduplicator)

    monkeypatch.setattr(graph_client_module, "get_graph_client", get_graph_client)
    monkeypatch.setattr(entities_module, "EntityManager", entity_manager_cls)
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
    get_graph_client.assert_awaited_once_with()
    entity_manager_cls.assert_called_once_with(client, group_id="org-123")
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
    import sibyl_core.graph.client as graph_client_module
    import sibyl_core.graph.entities as entities_module
    import sibyl_core.retrieval.dedup as dedup_module

    deduplicator = MagicMock()
    deduplicator.find_duplicates = AsyncMock(return_value=[])
    deduplicator.merge_entities = AsyncMock()

    monkeypatch.setattr(
        graph_client_module, "get_graph_client", AsyncMock(return_value=MagicMock())
    )
    monkeypatch.setattr(entities_module, "EntityManager", MagicMock(return_value=MagicMock()))
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
    import sibyl_core.graph.client as graph_client_module
    import sibyl_core.graph.entities as entities_module
    import sibyl_core.retrieval.dedup as dedup_module

    deduplicator = MagicMock()
    deduplicator.find_duplicates = AsyncMock(
        return_value=[_pair("keep-1", "remove-1", suggested_keep="keep-1")]
    )
    deduplicator.merge_entities = AsyncMock(side_effect=RuntimeError("boom"))

    monkeypatch.setattr(
        graph_client_module, "get_graph_client", AsyncMock(return_value=MagicMock())
    )
    monkeypatch.setattr(entities_module, "EntityManager", MagicMock(return_value=MagicMock()))
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
