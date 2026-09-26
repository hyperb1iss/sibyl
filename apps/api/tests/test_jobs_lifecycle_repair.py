"""Shared scheduler registration and organization failure isolation."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import lifecycle_repair
from sibyl.jobs.worker import WorkerSettings, get_schedule_specs
from sibyl_core.projection.repair import LifecycleRepairResult
from sibyl_core.services.embedding_sweep import (
    LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE,
    SWEEP_CURRENT,
    EmbeddingSweepResult,
)
from sibyl_core.services.embedding_verdicts import LegacyVerdicts

_NO_EMBEDDING_WORK = {
    "embedding_checked": 0,
    "embedding_reembedded": 0,
    "embedding_adopted": 0,
    "embedding_pending": 0,
    "embedding_skipped": 0,
    "embedding_rejected": 0,
    "embedding_failed": 0,
    "embedding_unverified": 0,
    "embedding_deferred": 0,
}


class _Runtime:
    client = object()


@pytest.fixture(autouse=True)
def quiet_embedding_sweeps(monkeypatch):
    """Keep the sweeps out of tests about the other repairs."""
    monkeypatch.setattr(
        lifecycle_repair,
        "document_chunk_sweep_inputs",
        AsyncMock(return_value=({"provider": "fake"}, True, AsyncMock())),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "settle_legacy_verdicts",
        AsyncMock(return_value=LegacyVerdicts(graph={}, document_chunks={})),
    )
    monkeypatch.setattr(lifecycle_repair, "configured_embedding_provider", lambda: None)
    monkeypatch.setattr(lifecycle_repair, "record_configured_embedding_models", AsyncMock())
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
    runtime = _Runtime()

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
    # One runtime serves each organization's verdicts, graph repairs and sweeps.
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


def _record(order: list[str], name: str, result=None):
    async def run(*_args, **_kwargs):
        order.append(name)
        return result

    return run


def _quiet_repairs(monkeypatch, order: list[str]) -> None:
    @asynccontextmanager
    async def background(_group_id):
        yield _Runtime()

    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=["org"]))
    monkeypatch.setattr(lifecycle_repair, "background_graph_runtime", background)
    for name in (
        "repair_graph_lifecycle",
        "repair_promoted_embeddings",
        "repair_raw_source_lifecycle",
        "repair_raw_capture_embeddings",
    ):
        monkeypatch.setattr(lifecycle_repair, name, _record(order, name, LifecycleRepairResult()))


async def test_both_verdicts_settle_together_before_either_sweep(monkeypatch):
    order: list[str] = []
    _quiet_repairs(monkeypatch, order)
    provider = object()
    monkeypatch.setattr(lifecycle_repair, "configured_embedding_provider", lambda: provider)
    embed_chunks = AsyncMock()
    monkeypatch.setattr(
        lifecycle_repair,
        "document_chunk_sweep_inputs",
        AsyncMock(return_value=({"provider": "content"}, True, embed_chunks)),
    )
    settle = AsyncMock(
        side_effect=lambda *_a, **_k: (
            order.append("verdicts") or LegacyVerdicts(graph={}, document_chunks={})
        )
    )
    monkeypatch.setattr(lifecycle_repair, "settle_legacy_verdicts", settle)
    monkeypatch.setattr(
        lifecycle_repair,
        "sweep_graph_embeddings",
        _record(
            order,
            "graph_sweep",
            EmbeddingSweepResult(
                plane="graph", status="partial", checked=5, recovered=3, pending=2, skipped=1
            ),
        ),
    )
    monkeypatch.setattr(
        lifecycle_repair,
        "sweep_document_chunk_embeddings",
        _record(
            order,
            "chunk_sweep",
            EmbeddingSweepResult(
                plane="document_chunks",
                status="completed",
                adopted=4,
                warning=LEGACY_WARNING_ADOPTED_WITHOUT_EVIDENCE,
            ),
        ),
    )

    result = await lifecycle_repair.repair_lifecycle_all_orgs({})

    settle.assert_awaited_once()
    assert settle.await_args.kwargs["graph_client"] is _Runtime.client
    assert settle.await_args.kwargs["graph_provider"] is provider
    assert settle.await_args.kwargs["chunk_stamp"] == {"provider": "content"}
    assert settle.await_args.kwargs["embed_chunks"] is embed_chunks
    assert order.index("verdicts") < order.index("graph_sweep")
    assert order.index("verdicts") < order.index("chunk_sweep")
    assert {key: value for key, value in result.items() if key.startswith("embedding_")} == {
        "embedding_checked": 5,
        "embedding_reembedded": 3,
        "embedding_adopted": 4,
        "embedding_pending": 2,
        "embedding_skipped": 1,
        "embedding_rejected": 0,
        "embedding_failed": 0,
        "embedding_unverified": 1,
        "embedding_deferred": 0,
    }


async def test_unproven_verdicts_settle_only_after_every_organization_published(monkeypatch):
    order: list[str] = []
    _quiet_repairs(monkeypatch, order)
    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=["c", "d"]))
    calls: list[tuple[str, bool]] = []

    async def settle(organization_id, **kwargs):
        calls.append((organization_id, kwargs["allow_unproven"]))
        deferred = organization_id == "c" and not kwargs["allow_unproven"]
        state = {"legacy_deferred": True} if deferred else {"legacy_decision": "reembed"}
        return LegacyVerdicts(graph=state, document_chunks={"legacy_decision": "none"})

    graph_sweep = AsyncMock(return_value=EmbeddingSweepResult(plane="graph", status=SWEEP_CURRENT))
    monkeypatch.setattr(lifecycle_repair, "settle_legacy_verdicts", settle)
    monkeypatch.setattr(lifecycle_repair, "sweep_graph_embeddings", graph_sweep)

    result = await lifecycle_repair.repair_lifecycle_all_orgs({})

    # "c" had nothing of its own; it is weighed again only after "d" spoke.
    assert calls == [("c", False), ("d", False), ("c", True)]
    assert graph_sweep.await_count == 1
    assert result["embedding_deferred"] == 1
    assert result["failed_organizations"] == 0


async def test_unproven_verdicts_wait_when_an_organization_could_not_publish(monkeypatch):
    order: list[str] = []
    _quiet_repairs(monkeypatch, order)
    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=["c", "down"]))
    calls: list[tuple[str, bool]] = []

    async def settle(organization_id, **kwargs):
        calls.append((organization_id, kwargs["allow_unproven"]))
        if organization_id == "down":
            raise ConnectionError("graph namespace unavailable")
        return LegacyVerdicts(graph={"legacy_deferred": True}, document_chunks=None)

    monkeypatch.setattr(lifecycle_repair, "settle_legacy_verdicts", settle)

    result = await lifecycle_repair.repair_lifecycle_all_orgs({})

    assert calls == [("c", False), ("down", False)]
    assert result["embedding_deferred"] == 1


async def test_the_deployment_model_record_is_refreshed_every_pass(monkeypatch):
    record = AsyncMock()
    monkeypatch.setattr(lifecycle_repair, "record_configured_embedding_models", record)
    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=[]))

    await lifecycle_repair.repair_lifecycle_all_orgs({})

    record.assert_awaited_once()


async def test_an_unsettled_chunk_verdict_skips_only_the_chunk_sweep(monkeypatch):
    order: list[str] = []
    _quiet_repairs(monkeypatch, order)
    monkeypatch.setattr(
        lifecycle_repair,
        "settle_legacy_verdicts",
        AsyncMock(
            return_value=LegacyVerdicts(
                graph={}, document_chunks=ConnectionError("content store unavailable")
            )
        ),
    )
    graph_sweep = AsyncMock(return_value=EmbeddingSweepResult(plane="graph", status=SWEEP_CURRENT))
    chunk_sweep = AsyncMock()
    monkeypatch.setattr(lifecycle_repair, "sweep_graph_embeddings", graph_sweep)
    monkeypatch.setattr(lifecycle_repair, "sweep_document_chunk_embeddings", chunk_sweep)

    result = await lifecycle_repair.repair_lifecycle_all_orgs({})

    graph_sweep.assert_awaited_once()
    chunk_sweep.assert_not_awaited()
    # Raw repair no longer waits on any verdict: the evidence is a photograph.
    assert "repair_raw_capture_embeddings" in order
    assert "repair_promoted_embeddings" in order
    assert result["failed_organizations"] == 1


async def test_an_unsettled_graph_verdict_skips_only_the_graph_sweep(monkeypatch):
    order: list[str] = []
    _quiet_repairs(monkeypatch, order)
    monkeypatch.setattr(
        lifecycle_repair,
        "settle_legacy_verdicts",
        AsyncMock(side_effect=ConnectionError("content store unavailable")),
    )
    graph_sweep = AsyncMock()
    chunk_sweep = AsyncMock(
        return_value=EmbeddingSweepResult(plane="document_chunks", status=SWEEP_CURRENT)
    )
    monkeypatch.setattr(lifecycle_repair, "sweep_graph_embeddings", graph_sweep)
    monkeypatch.setattr(lifecycle_repair, "sweep_document_chunk_embeddings", chunk_sweep)

    result = await lifecycle_repair.repair_lifecycle_all_orgs({})

    graph_sweep.assert_not_awaited()
    chunk_sweep.assert_not_awaited()
    assert "repair_graph_lifecycle" in order
    assert result["failed_organizations"] == 1

    monkeypatch.setattr(
        lifecycle_repair,
        "settle_legacy_verdicts",
        AsyncMock(return_value=LegacyVerdicts(graph=RuntimeError("graph"), document_chunks={})),
    )
    await lifecycle_repair.repair_lifecycle_all_orgs({})
    graph_sweep.assert_not_awaited()
    chunk_sweep.assert_awaited_once()


async def test_a_failed_graph_sweep_keeps_the_graph_lifecycle_counts(monkeypatch):
    @asynccontextmanager
    async def background(_group_id):
        yield _Runtime()

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
