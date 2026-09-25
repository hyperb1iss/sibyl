"""Shared scheduler registration and organization failure isolation."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import lifecycle_repair
from sibyl.jobs.worker import WorkerSettings, get_schedule_specs
from sibyl_core.projection.repair import LifecycleRepairResult
from sibyl_core.services.embedding_sweep import SWEEP_CURRENT, EmbeddingSweepResult

_NO_EMBEDDING_WORK = {
    "embedding_checked": 0,
    "embedding_reembedded": 0,
    "embedding_adopted": 0,
    "embedding_pending": 0,
    "embedding_skipped": 0,
    "embedding_rejected": 0,
    "embedding_failed": 0,
}


@pytest.fixture(autouse=True)
def quiet_embedding_sweeps(monkeypatch):
    """Keep the sweeps out of tests about the other repairs."""
    monkeypatch.setattr(
        lifecycle_repair,
        "document_chunk_sweep_inputs",
        AsyncMock(return_value=({"provider": "fake"}, True, AsyncMock())),
    )
    monkeypatch.setattr(lifecycle_repair, "decide_document_chunk_legacy_vectors", AsyncMock())
    monkeypatch.setattr(lifecycle_repair, "decide_graph_legacy_vectors", AsyncMock())
    monkeypatch.setattr(
        lifecycle_repair,
        "sweep_document_chunk_embeddings",
        AsyncMock(return_value=EmbeddingSweepResult(plane="document_chunks", status=SWEEP_CURRENT)),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "sweep_graph_embeddings",
        AsyncMock(return_value=EmbeddingSweepResult(plane="graph", status=SWEEP_CURRENT)),
    )


async def test_scheduled_repair_continues_after_org_failure(monkeypatch):
    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=["a", "b"]))
    entered = []
    closed = []
    runtime = object()

    @asynccontextmanager
    async def background(group_id):
        entered.append(group_id)
        if group_id == "a":
            raise ConnectionError("unavailable")
        try:
            yield runtime
        finally:
            closed.append(group_id)

    monkeypatch.setattr(lifecycle_repair, "background_graph_runtime", background)
    repair = AsyncMock(return_value=LifecycleRepairResult(checked=3, recovered=2, pending=1))
    monkeypatch.setattr(lifecycle_repair, "repair_graph_lifecycle", repair)
    raw_repair = AsyncMock(return_value=LifecycleRepairResult(checked=1, recovered=1))
    monkeypatch.setattr(lifecycle_repair, "repair_raw_source_lifecycle", raw_repair)
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_promoted_embeddings",
        AsyncMock(return_value=LifecycleRepairResult()),
    )
    embedding_repair = AsyncMock(
        return_value=LifecycleRepairResult(checked=2, recovered=1, failed=1)
    )
    monkeypatch.setattr(lifecycle_repair, "repair_raw_capture_embeddings", embedding_repair)
    result = await lifecycle_repair.repair_lifecycle_all_orgs({})
    assert result == {
        "organizations": 2,
        "failed_organizations": 1,
        "checked": 9,
        "recovered": 6,
        "pending": 1,
        "failed": 2,
        **_NO_EMBEDDING_WORK,
    }
    repair.assert_awaited_once_with(runtime)
    assert entered == ["a", "b"]
    assert closed == ["b"]
    assert raw_repair.await_count == 2
    assert embedding_repair.await_count == 2
    assert {call.args for call in embedding_repair.await_args_list} == {("a",), ("b",)}


def test_repair_uses_shared_local_and_redis_schedule():
    specs = [spec for spec in get_schedule_specs() if spec.name == "repair_lifecycle_all_orgs"]
    assert len(specs) == 1
    assert specs[0].function is lifecycle_repair.repair_lifecycle_all_orgs
    assert specs[0].schedule_label == "* * * * *"
    assert lifecycle_repair.repair_lifecycle_all_orgs in WorkerSettings.functions


async def test_repair_resolves_current_memberships_with_one_auth_context(monkeypatch):
    from types import SimpleNamespace

    context = SimpleNamespace(user_id="owner", organization_id="org", org_role="member")
    resolve = AsyncMock(return_value=context)
    projects = AsyncMock(return_value={"project"})
    teams = AsyncMock(return_value={"team"})
    delegated = AsyncMock(return_value={"delegated"})
    monkeypatch.setattr(lifecycle_repair, "resolve_auth_context", resolve)
    monkeypatch.setattr(lifecycle_repair, "list_accessible_project_graph_ids", projects)
    monkeypatch.setattr(lifecycle_repair, "list_accessible_team_scope_keys", teams)
    monkeypatch.setattr(lifecycle_repair, "list_accessible_delegated_scope_keys", delegated)
    authority = await lifecycle_repair.resolve_source_authority("org", "owner")
    assert authority.projects == {"project"}
    assert authority.teams == {"team"}
    assert authority.delegations == {"delegated"}
    assert authority.scope_keys is None
    resolve.assert_awaited_once_with(claims={"sub": "owner", "org": "org"})
    for reader in (projects, teams, delegated):
        reader.assert_awaited_once_with(context)


async def test_repair_rejects_revoked_org_membership_before_reading_grants(monkeypatch):
    from types import SimpleNamespace

    context = SimpleNamespace(user_id="owner", organization_id="org", org_role=None)
    projects = AsyncMock()
    monkeypatch.setattr(lifecycle_repair, "resolve_auth_context", AsyncMock(return_value=context))
    monkeypatch.setattr(lifecycle_repair, "list_accessible_project_graph_ids", projects)
    assert await lifecycle_repair.resolve_source_authority("org", "owner") is None
    projects.assert_not_awaited()


async def test_repair_rejects_deleted_user(monkeypatch):
    from sibyl.persistence.auth_common import UserNotFoundError

    monkeypatch.setattr(
        lifecycle_repair, "resolve_auth_context", AsyncMock(side_effect=UserNotFoundError("gone"))
    )
    assert await lifecycle_repair.resolve_source_authority("org", "owner") is None


async def test_embedding_verdicts_settle_before_the_repairs_that_restamp_evidence(monkeypatch):
    order: list[str] = []

    def record(name, result=None):
        async def run(*_args, **_kwargs):
            order.append(name)
            return result

        return run

    @asynccontextmanager
    async def background(_group_id):
        yield object()

    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=["org"]))
    monkeypatch.setattr(lifecycle_repair, "background_graph_runtime", background)
    monkeypatch.setattr(
        lifecycle_repair, "decide_document_chunk_legacy_vectors", record("chunk_verdict")
    )
    monkeypatch.setattr(lifecycle_repair, "decide_graph_legacy_vectors", record("graph_verdict"))
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_graph_lifecycle",
        record("graph_lifecycle", LifecycleRepairResult()),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_promoted_embeddings",
        record("promoted", LifecycleRepairResult()),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_raw_source_lifecycle",
        record("raw_lifecycle", LifecycleRepairResult()),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_raw_capture_embeddings",
        record("raw_embeddings", LifecycleRepairResult()),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "sweep_graph_embeddings",
        record(
            "graph_sweep",
            EmbeddingSweepResult(
                plane="graph", status="partial", checked=5, recovered=3, pending=2, skipped=1
            ),
        ),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "sweep_document_chunk_embeddings",
        record(
            "chunk_sweep",
            EmbeddingSweepResult(plane="document_chunks", status="completed", adopted=4),
        ),
    )

    result = await lifecycle_repair.repair_lifecycle_all_orgs({})

    assert order.index("chunk_verdict") < order.index("raw_embeddings")
    assert order.index("graph_verdict") < order.index("promoted")
    assert {key: value for key, value in result.items() if key.startswith("embedding_")} == {
        "embedding_checked": 5,
        "embedding_reembedded": 3,
        "embedding_adopted": 4,
        "embedding_pending": 2,
        "embedding_skipped": 1,
        "embedding_rejected": 0,
        "embedding_failed": 0,
    }


async def test_an_unsettled_chunk_verdict_holds_back_raw_restamping(monkeypatch):
    @asynccontextmanager
    async def background(_group_id):
        yield object()

    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=["org"]))
    monkeypatch.setattr(lifecycle_repair, "background_graph_runtime", background)
    monkeypatch.setattr(
        lifecycle_repair,
        "decide_document_chunk_legacy_vectors",
        AsyncMock(side_effect=ConnectionError("content store unavailable")),
    )
    for name in ("repair_graph_lifecycle", "repair_promoted_embeddings"):
        monkeypatch.setattr(lifecycle_repair, name, AsyncMock(return_value=LifecycleRepairResult()))
    raw_lifecycle = AsyncMock(return_value=LifecycleRepairResult())
    raw_embeddings = AsyncMock(return_value=LifecycleRepairResult())
    chunk_sweep = AsyncMock()
    monkeypatch.setattr(lifecycle_repair, "repair_raw_source_lifecycle", raw_lifecycle)
    monkeypatch.setattr(lifecycle_repair, "repair_raw_capture_embeddings", raw_embeddings)
    monkeypatch.setattr(lifecycle_repair, "sweep_document_chunk_embeddings", chunk_sweep)

    await lifecycle_repair.repair_lifecycle_all_orgs({})

    raw_lifecycle.assert_awaited_once()
    raw_embeddings.assert_not_awaited()
    chunk_sweep.assert_not_awaited()


async def test_a_failed_graph_sweep_keeps_the_graph_lifecycle_counts(monkeypatch):
    @asynccontextmanager
    async def background(_group_id):
        yield object()

    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=["org"]))
    monkeypatch.setattr(lifecycle_repair, "background_graph_runtime", background)
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_graph_lifecycle",
        AsyncMock(return_value=LifecycleRepairResult(checked=2, recovered=2)),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_promoted_embeddings",
        AsyncMock(return_value=LifecycleRepairResult()),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_raw_source_lifecycle",
        AsyncMock(return_value=LifecycleRepairResult()),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "repair_raw_capture_embeddings",
        AsyncMock(return_value=LifecycleRepairResult()),
    )
    monkeypatch.setattr(
        lifecycle_repair, "sweep_graph_embeddings", AsyncMock(side_effect=TimeoutError())
    )

    result = await lifecycle_repair.repair_lifecycle_all_orgs({})

    assert (result["checked"], result["recovered"], result["failed_organizations"]) == (2, 2, 1)
