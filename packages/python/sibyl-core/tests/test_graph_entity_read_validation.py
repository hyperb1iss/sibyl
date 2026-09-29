"""Standalone entity reads share proofs while keeping the next read fresh."""

from unittest.mock import AsyncMock

import pytest

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.models.experience import OperationalExperience
from sibyl_core.services import content_client, graph_derivations
from sibyl_core.services.content_raw_persistence import remember_raw_memory
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.operational_capture import OperationalSourceWrite, canonical_experience
from sibyl_core.services.operational_projection import load_operational_projection_source
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import content_store as content_store
from tests.test_operational_projection import runtime as runtime


async def published_entities(runtime, authority, observations):
    org = runtime.client.group_id
    payload = OperationalExperience.model_validate(
        {
            "source_id": "operation",
            "project_id": "project_a",
            "goal": "inspect",
            "observations": [
                {
                    "id": f"state_{index}",
                    "ordinal": index,
                    "evidence": [{"id": "text", "content": "The page is unchanged."}],
                }
                for index in range(observations)
            ],
        }
    )
    memory = await remember_raw_memory(
        organization_id=org,
        principal_id="user_a",
        source_id="operation",
        raw_content=canonical_experience(payload),
        memory_scope="project",
        scope_key="project_a",
        metadata={"project_id": "project_a"},
        capture_surface="operational_experience",
        embedding_provider=None,
        operational_write=OperationalSourceWrite(org, "operation", "user_a", "project_a", None),
    )
    source = await load_operational_projection_source(
        SourceIdentity(org, SourceKind.RAW_CAPTURE, memory.id), authority
    )
    projection = await runtime.entity_manager.publish_operational_entities(source)
    return memory, list(projection.manifest.entity_ids)


@pytest.mark.parametrize("observations", [1, 10, 100])
async def test_graph_entity_read_queries_depend_on_sources_not_entity_count(
    runtime, content_store, authority, monkeypatch, observations
):
    _, ids = await published_entities(runtime, authority, observations)
    resolver = AsyncMock(return_value=authority)
    monkeypatch.setattr(graph_derivations, "get_source_authority_resolver", lambda: resolver)
    async with content_client.surreal_content_client() as client:
        queries = AsyncMock(wraps=client.execute_query)
        monkeypatch.setattr(client, "execute_query", queries)
        current = await available_graph_entities(runtime.client.group_id, ids, runtime=runtime)
    assert set(current) == set(ids)
    assert len(current) >= observations
    resolver.assert_awaited_once_with(runtime.client.group_id, "user_a")
    assert queries.await_count <= 4


@pytest.mark.parametrize(
    "mutation",
    ["raw_content", "raw_incarnation", "association", "projects", "principal", "authority"],
)
async def test_graph_entity_read_next_call_rechecks_evidence_and_authority(
    runtime, content_store, authority, monkeypatch, mutation
):
    memory, ids = await published_entities(runtime, authority, 10)
    resolver = AsyncMock(return_value=authority)
    monkeypatch.setattr(graph_derivations, "get_source_authority_resolver", lambda: resolver)
    assert set(
        await available_graph_entities(runtime.client.group_id, ids, runtime=runtime)
    ) == set(ids)
    if mutation in {"projects", "principal", "authority"}:
        resolver.return_value = {
            "projects": SourceReadAuthority("user_a"),
            "principal": SourceReadAuthority("user_b", projects=frozenset({"project_a"})),
            "authority": None,
        }[mutation]
    elif mutation == "association":
        assert await runtime.client.execute_query("UPDATE memory_derivations SET active=false;")
    else:
        async with content_client.surreal_content_client() as client:
            sql = (
                "UPDATE raw_captures SET raw_content='changed evidence' WHERE uuid=$id;"
                if mutation == "raw_content"
                else "UPDATE source_states SET incarnation=type::string(rand::uuid()) "
                "WHERE source_kind='raw_capture' AND source_id=$id;"
            )
            assert await client.execute_query(sql, id=memory.id)
    assert not await available_graph_entities(runtime.client.group_id, ids, runtime=runtime)
    if mutation != "association":
        assert resolver.await_count == 2
