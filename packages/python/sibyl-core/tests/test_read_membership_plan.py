"""Persisted membership facts must reach native raw retrieval and graph policy."""

import os
from uuid import uuid4

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.config import settings
from sibyl_core.models.context import ContextFacet
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.retrieval._search_sources import _recall_raw_candidates
from sibyl_core.retrieval.search import build_context_retrieval_plan
from sibyl_core.services import content_client
from sibyl_core.services.content_raw_recall import recall_raw_memory_with_sources
from sibyl_core.services.surreal_content import remember_raw_memory


@pytest.fixture
async def membership_store(monkeypatch):
    client = SurrealContentClient(
        url=os.environ.get("SIBYL_SHARED_RETIREMENT_TEST_URL", "memory://"),
        username="root",
        password="root",
        namespace=f"membership_{uuid4().hex}",
    )
    monkeypatch.setattr(settings, "surreal_url", client._url)
    await bootstrap_content_schema(client)

    async def shared():
        return client

    monkeypatch.setattr(content_client, "get_shared_surreal_content_client", shared)
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [MemoryScope.TEAM, MemoryScope.DELEGATED])
@pytest.mark.parametrize(
    "grant", ["member", "missing_membership", "ceiling_excludes", "ceiling_admits"]
)
async def test_native_recall_reads_only_verified_memberships_with_key_ceiling(
    membership_store, scope, grant
):
    org_id, writer, reader, key = [str(uuid4()) for _ in range(4)]
    memory = await remember_raw_memory(
        organization_id=org_id,
        principal_id=writer,
        source_id="retained:scope-proof",
        raw_content="spectrochemical membership evidence",
        memory_scope=scope,
        scope_key=key,
        embedding_provider=None,
    )
    memberships = {key} if grant != "missing_membership" else set()
    ceiling = (
        {memory_scope_policy_key(scope, key)}
        if grant == "ceiling_admits"
        else {memory_scope_policy_key(MemoryScope.PRIVATE, reader)}
        if grant == "ceiling_excludes"
        else None
    )
    plan = build_context_retrieval_plan(
        query="spectrochemical",
        organization_id=org_id,
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["raw_memory"]},
        principal_id=reader,
        project=None,
        accessible_projects=set(),
        accessible_teams=memberships if scope is MemoryScope.TEAM else set(),
        accessible_delegations=memberships if scope is MemoryScope.DELEGATED else set(),
        allowed_memory_scope_keys=ceiling,
        limit=8,
    )
    recalled = await _recall_raw_candidates(
        plan=plan,
        facet=None,
        requested_types={"raw_memory"},
        limit=8,
        recall_fn=recall_raw_memory_with_sources,
    )
    ids = {candidate.id for candidate in recalled.candidates}
    assert (f"raw_memory:{memory.id}" in ids) is (grant in {"member", "ceiling_admits"})
    if grant == "ceiling_excludes":
        assert plan.accessible_teams == frozenset()
        assert plan.accessible_delegations == frozenset()
        assert any(denial.reason == "api_key_scope_excluded" for denial in plan.denied_scopes)
