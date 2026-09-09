"""Current target checks preserve authorized ordinary capture retrieval."""

from dataclasses import replace
from uuid import uuid4

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.retrieval._search_plan import RetrievalPlan, ScopeSpec
from sibyl_core.retrieval._search_sources import _recall_raw_candidates
from sibyl_core.services import content_client
from sibyl_core.services.content_models import MemoryScope
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.memory_derivations import unavailable_raw_derivation_ids
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.source_observations import SourceUnavailableError, observe_raw_capture
from sibyl_core.services.surreal_content import recall_raw_memory, remember_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime
from tests.test_synthesis_source_observations import remember_observed_synthesis


async def test_native_private_recall_preserves_principal_grant(content_store):
    org = str(uuid4())
    memory = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="capture",
        raw_content="Electric telescope capture",
        embedding_provider=None,
    )
    plan = RetrievalPlan(
        query="telescope",
        organization_id=org,
        facets=(),
        facet_types={},
        scopes=(ScopeSpec(MemoryScope.PRIVATE, None, "private_principal_bound", "owner"),),
        denied_scopes=(),
    )
    result = await _recall_raw_candidates(
        plan=plan, facet=None, requested_types=set(), limit=10, recall_fn=recall_raw_memory
    )
    assert [candidate.id for candidate in result.candidates] == [f"raw_memory:{memory.id}"]
    denied = await recall_raw_memory(
        organization_id=org,
        principal_id="owner",
        query="telescope",
        source_authority=SourceReadAuthority(
            "owner", scope_keys=frozenset({memory_scope_policy_key(MemoryScope.PRIVATE, "other")})
        ),
    )
    assert denied == []


@pytest.mark.parametrize("scope_restricted", [False, True])
async def test_imported_org_target_remains_recallable_with_current_grant(
    content_store, scope_restricted
):
    org = str(uuid4())
    memory = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="import",
        raw_content="Electric telescope import",
        memory_scope=MemoryScope.ORGANIZATION,
        capture_surface="source_import",
        embedding_provider=None,
    )
    authority = SourceReadAuthority(
        "owner",
        scope_keys=frozenset({memory_scope_policy_key(MemoryScope.ORGANIZATION, None)})
        if scope_restricted
        else None,
    )
    found = await recall_raw_memory(
        organization_id=org,
        principal_id="owner",
        query="telescope",
        memory_scope=MemoryScope.ORGANIZATION,
        source_authority=authority,
    )
    assert [item.id for item in found] == [memory.id]
    with pytest.raises(SourceUnavailableError):
        observe_raw_capture(memory, authority)
    assert await unavailable_raw_derivation_ids(
        org, [memory], replace(authority, scope_keys=frozenset())
    ) == {memory.id}
    assert await unavailable_raw_derivation_ids(
        org, [replace(memory, memory_scope=MemoryScope.PRIVATE)], authority
    ) == {memory.id}


async def test_org_scope_and_removed_marker_cannot_bypass_protected_ancestry(
    runtime, content_store
):
    org = runtime.client.group_id
    source = await remember_raw_memory(
        organization_id=org,
        principal_id="user_a",
        source_id="rule",
        raw_content="Electric telescope requires approval.",
        embedding_provider=None,
    )
    artifact = await remember_observed_synthesis(
        runtime, source.id, "raw_memory", source.revision, source.raw_content
    )
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET memory_scope='organization', capture_surface='source_import' "
            "WHERE organization_id=$org AND uuid=$id",
            org=org,
            id=artifact.remembered_memory_id,
        )
    await apply_memory_correction(
        organization_id=org, principal_id="user_a", source_id=source.id, action="hide"
    )
    recalled = await recall_raw_memory(
        organization_id=org,
        principal_id="user_a",
        query="approval",
        memory_scope=MemoryScope.ORGANIZATION,
    )
    assert artifact.remembered_memory_id not in {memory.id for memory in recalled}
