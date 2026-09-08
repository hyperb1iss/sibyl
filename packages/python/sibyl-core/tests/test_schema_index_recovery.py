"""Recovery distinguishes missing definitions from unfinished index builds."""

from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal import schema_index_recovery as recovery
from sibyl_core.backends.surreal.schema_ownership import SchemaOwnershipLost
from sibyl_core.backends.surreal.schema_version import (
    ConcurrentIndexDefinition,
    IndexBuildFailedError,
    IndexBuildStalledError,
    IndexBuildStatus,
)

DEFINITION = ConcurrentIndexDefinition(
    name="idx_value", table="sample", definition="DEFINE INDEX idx_value ON sample FIELDS value"
)


@pytest.mark.parametrize("existing_status", ["missing", "error"])
async def test_recovery_creates_missing_or_replaces_failed_build(monkeypatch, existing_status):
    owner = AsyncMock()
    owner.read.return_value = {
        "indexes": {} if existing_status == "missing" else {"idx_value": "ddl"}
    }
    monkeypatch.setattr(
        recovery,
        "get_index_build_status",
        AsyncMock(return_value=IndexBuildStatus(existing_status) if existing_status else None),
    )
    ready = IndexBuildStatus("ready", initial=1000)
    wait = AsyncMock(return_value=ready)
    monkeypatch.setattr(recovery, "wait_for_index_ready", wait)

    assert await recovery.ensure_owned_concurrent_index(owner, DEFINITION) == ready

    owner.mutate.assert_awaited_once()
    body = owner.mutate.await_args.args[0]
    assert ("REMOVE INDEX idx_value ON sample;" in body) == (existing_status != "missing")
    assert body.endswith("DEFINE INDEX idx_value ON sample FIELDS value CONCURRENTLY;")
    wait.assert_awaited_once_with(
        owner.read,
        name="idx_value",
        table="sample",
        require_status=True,
        ownership=owner,
        timeout_seconds=300.0,
        track_progress=True,
    )


async def test_ready_index_is_preserved_unless_explicitly_rebuilt(monkeypatch):
    owner = AsyncMock()
    owner.read.return_value = {"indexes": {"idx_value": "ddl"}}
    ready = IndexBuildStatus("ready", initial=1000)
    monkeypatch.setattr(recovery, "get_index_build_status", AsyncMock(return_value=ready))
    wait = AsyncMock(return_value=ready)
    monkeypatch.setattr(recovery, "wait_for_index_ready", wait)
    assert await recovery.ensure_owned_concurrent_index(owner, DEFINITION) == ready
    owner.mutate.assert_not_awaited()
    wait.assert_not_awaited()
    assert await recovery.ensure_owned_concurrent_index(owner, DEFINITION, rebuild=True) == ready
    owner.mutate.assert_awaited_once()
    wait.assert_awaited_once()


async def test_lost_owner_cannot_inspect_or_restart_index():
    owner = AsyncMock()
    owner.heartbeat.side_effect = SchemaOwnershipLost("expired")
    with pytest.raises(SchemaOwnershipLost):
        await recovery.ensure_owned_concurrent_index(owner, DEFINITION)
    owner.read.assert_not_awaited()
    owner.mutate.assert_not_awaited()


async def test_unavailable_table_metadata_is_not_treated_as_missing_index():
    owner = AsyncMock()
    owner.read.return_value = {}
    with pytest.raises(TypeError, match="table index definitions"):
        await recovery.ensure_owned_concurrent_index(owner, DEFINITION)
    owner.mutate.assert_not_awaited()


async def test_failed_rebuild_does_not_continue_to_readiness(monkeypatch):
    owner = AsyncMock()
    owner.read.return_value = {"indexes": {}}
    owner.mutate.side_effect = SchemaOwnershipLost("expired")
    wait = AsyncMock()
    monkeypatch.setattr(recovery, "wait_for_index_ready", wait)
    with pytest.raises(SchemaOwnershipLost):
        await recovery.ensure_owned_concurrent_index(owner, DEFINITION)
    wait.assert_not_awaited()


async def test_progressing_predecessor_build_finishes_without_rebuild(monkeypatch):
    owner = AsyncMock()
    owner.read.return_value = {"indexes": {"idx_value": "ddl"}}
    monkeypatch.setattr(
        recovery, "get_index_build_status", AsyncMock(return_value=IndexBuildStatus("indexing"))
    )
    ready = IndexBuildStatus("ready", initial=1000)
    monkeypatch.setattr(recovery, "wait_for_index_ready", AsyncMock(return_value=ready))
    assert await recovery.ensure_owned_concurrent_index(owner, DEFINITION) == ready
    owner.mutate.assert_not_awaited()


@pytest.mark.parametrize("failure", [IndexBuildStalledError, IndexBuildFailedError])
async def test_stalled_or_failed_predecessor_is_rebuilt_once(monkeypatch, failure):
    owner = AsyncMock()
    owner.read.return_value = {"indexes": {"idx_value": "ddl"}}
    monkeypatch.setattr(
        recovery, "get_index_build_status", AsyncMock(return_value=IndexBuildStatus("indexing"))
    )
    ready = IndexBuildStatus("ready", initial=1000)
    wait = AsyncMock(side_effect=[failure("stopped"), ready])
    monkeypatch.setattr(recovery, "wait_for_index_ready", wait)
    assert await recovery.ensure_owned_concurrent_index(owner, DEFINITION) == ready
    owner.mutate.assert_awaited_once()
    assert owner.mutate.await_args.args[0].startswith("REMOVE INDEX idx_value ON sample;")
    assert wait.await_count == 2


@pytest.mark.parametrize("failure", [TimeoutError, SchemaOwnershipLost, OSError])
async def test_read_or_ownership_failure_never_rebuilds(monkeypatch, failure):
    owner = AsyncMock()
    owner.read.return_value = {"indexes": {"idx_value": "ddl"}}
    monkeypatch.setattr(
        recovery, "get_index_build_status", AsyncMock(return_value=IndexBuildStatus("indexing"))
    )
    monkeypatch.setattr(recovery, "wait_for_index_ready", AsyncMock(side_effect=failure("failed")))
    with pytest.raises(failure):
        await recovery.ensure_owned_concurrent_index(owner, DEFINITION)
    owner.mutate.assert_not_awaited()


async def test_missing_build_metadata_does_not_destroy_existing_index(monkeypatch):
    owner = AsyncMock()
    owner.read.return_value = {"indexes": {"idx_value": "ddl"}}
    monkeypatch.setattr(recovery, "get_index_build_status", AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match="no build status"):
        await recovery.ensure_owned_concurrent_index(owner, DEFINITION)
    owner.mutate.assert_not_awaited()
