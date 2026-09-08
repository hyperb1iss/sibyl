"""Repair sweeps recover healthy sources without starving unresolved rows."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.projection import repair
from tests.test_reflection_identity import runtime as runtime


async def test_repair_sweeps_cross_pages_and_preserve_unknown_owners(runtime, monkeypatch):
    monkeypatch.setattr(repair, "_PAGE_SIZE", 2)
    for row_id, metadata in (
        ("parent", {}),
        ("a-legacy", {"source_validation_pending": True}),
        ("b-missing", {"lifecycle_reconciliation_pending": {"parent:absent": True}}),
        (
            "c-ready",
            {
                "lifecycle_reconciliation_pending": {"parent:parent": True},
                "excluded_from_recall": True,
            },
        ),
        ("d-ready", {"lifecycle_reconciliation_pending": {"parent:parent": True}}),
        ("e-ready", {"lifecycle_reconciliation_pending": {"parent:parent": True}}),
    ):
        await runtime.entity_manager.create_direct(
            Entity(id=row_id, name=row_id, entity_type=EntityType.EPISODE, metadata=metadata),
            generate_embedding=False,
        )
    result = await repair.repair_graph_lifecycle(runtime)
    assert (result.checked, result.recovered, result.pending, result.failed) == (5, 3, 2, 0)
    authored = await runtime.entity_manager.get("c-ready")
    assert authored.metadata["excluded_from_recall"] is True
    repeated = await repair.repair_graph_lifecycle(runtime)
    assert (repeated.checked, repeated.pending, repeated.recovered) == (2, 2, 0)
    await runtime.entity_manager.create_direct(
        Entity(id="absent", name="Available parent", entity_type=EntityType.EPISODE),
        generate_embedding=False,
    )
    await asyncio.gather(
        repair.repair_graph_lifecycle(runtime), repair.repair_graph_lifecycle(runtime)
    )
    pending = await repair.repair_graph_lifecycle(runtime)
    assert (pending.checked, pending.pending, pending.failed) == (1, 1, 0)
    legacy = await runtime.entity_manager.get("a-legacy")
    assert legacy.metadata["source_validation_pending"]


@pytest.mark.parametrize("row_id,key", [("z-row", "a-key"), ("a-row", "z-key")])
async def test_repair_cursor_uses_the_ordered_key(monkeypatch, row_id, key):
    monkeypatch.setattr(repair, "_PAGE_SIZE", 1)
    query = AsyncMock(side_effect=[[{"uuid": row_id, "lifecycle_repair_key": key}], []])
    repair_row = AsyncMock(return_value="pending")
    monkeypatch.setattr(repair, "_repair_row", repair_row)
    runtime = SimpleNamespace(client=SimpleNamespace(group_id="org", execute_query=query))

    result = await repair.repair_graph_lifecycle(runtime)

    assert result.checked == result.pending == 1
    assert query.await_args_list[0].kwargs["cursor"] == ""
    assert query.await_args_list[1].kwargs["cursor"] == key
    repair_row.assert_awaited_once_with(runtime, row_id)


@pytest.mark.parametrize("digest", ["", "short", "z" * 64])
async def test_malformed_signature_does_not_starve_other_repair_owners(runtime, digest):
    malformed = f"capture-signature-v1:source:{digest}"
    for row_id, metadata in (
        ("parent", {}),
        (
            "child",
            {"lifecycle_reconciliation_pending": {malformed: True, "parent:parent": True}},
        ),
    ):
        await runtime.entity_manager.create_direct(
            Entity(id=row_id, name=row_id, entity_type=EntityType.EPISODE, metadata=metadata),
            generate_embedding=False,
        )
    result = await repair.repair_graph_lifecycle(runtime)
    assert result.checked == result.pending == 1 and result.failed == 0
    row = await runtime.entity_manager.get("child")
    assert row.metadata["lifecycle_reconciliation_pending"] == {malformed: True}
