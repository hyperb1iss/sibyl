"""Request-owned proofs retain source and authority freshness at return."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services.graph_read_availability import available_graph_relationships
from sibyl_core.services.graph_read_validation import GraphReadValidation
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.source_observations import SourceUnavailableError
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import capture
from tests.test_operational_projection import content_store as content_store
from tests.test_operational_projection import runtime as runtime
from tests.test_operational_relationships import relationship_authority as relationship_authority


@pytest.mark.parametrize(
    "mutation",
    [
        "raw_content",
        "raw_incarnation",
        "authority",
        "association_active",
        "association_ceiling",
        "endpoint_incarnation",
        "edge_body",
        "bookkeeping",
    ],
)
async def test_graph_read_batch_final_boundary(
    runtime, content_store, authority, relationship_authority, monkeypatch, mutation
):
    from sibyl_core.services import content_client, operational_relationships

    memory, source = await capture(runtime, authority)
    await runtime.entity_manager.publish_operational_entities(source)
    ids = await runtime.relationship_manager.publish_operational_relationships(source)
    original = await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    assert set(original) == set(ids)
    snapshot = operational_relationships._snapshot
    calls = 0

    async def boundary(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if mutation in {"raw_content", "raw_incarnation"}:
                async with content_client.surreal_content_client() as client:
                    sql = (
                        "UPDATE raw_captures SET raw_content='changed evidence' WHERE uuid=$id;"
                        if mutation == "raw_content"
                        else "UPDATE source_states SET incarnation=type::string(rand::uuid()) "
                        "WHERE source_kind='raw_capture' AND source_id=$id;"
                    )
                    changed = await client.execute_query(sql, id=memory.id)
                    assert changed
            elif mutation == "authority":
                for module in ("graph_derivations", "operational_relationships"):
                    monkeypatch.setattr(
                        f"sibyl_core.services.{module}.get_source_authority_resolver",
                        lambda: AsyncMock(return_value=SourceReadAuthority("user_a")),
                    )
            else:
                sql = {
                    "association_active": "UPDATE memory_derivations SET active=false;",
                    "association_ceiling": "UPDATE memory_derivations SET authority_ceiling.projects=['project_a','project_b'];",
                    "endpoint_incarnation": "UPDATE source_states SET incarnation=type::string(rand::uuid()) WHERE source_kind='graph_entity';",
                    "edge_body": "UPDATE relates_to SET fact='changed edge';",
                    "bookkeeping": "UPDATE relates_to SET attributes.operational_write_witness=type::string(rand::uuid()); UPDATE memory_derivations SET validation_write_witness=type::string(rand::uuid());",
                }[mutation]
                assert await runtime.client.execute_query(sql)
        return await snapshot(*args, **kwargs)

    monkeypatch.setattr(operational_relationships, "_snapshot", boundary)
    current = await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    assert calls == 2
    if mutation == "bookkeeping":
        assert set(current) == set(ids)
        # Witnesses are not semantic evidence; every other returned field stays exact.
        for identifier, row in current.items():
            expected = original[identifier].model_dump(mode="json")
            actual = row.model_dump(mode="json")
            for value in (expected, actual):
                value["metadata"].pop("operational_write_witness", None)
            assert actual == expected
    else:
        assert current == {}


async def test_graph_read_batch_keeps_authority_ceilings_and_ancestry_separate(
    runtime, content_store, authority
):
    from sibyl_core.services.memory_derivations import validate_observations

    memory, _ = await capture(runtime, authority)
    org = runtime.client.group_id
    source = SourceIdentity(org, SourceKind.RAW_CAPTURE, memory.id)
    read = GraphReadValidation(org)
    snapshot = await read.source_snapshot(source, authority)
    assert await validate_observations(
        [snapshot.observation], authority, organization_id=org, read=read
    )
    assert not await validate_observations(
        [snapshot.observation],
        authority,
        organization_id=org,
        ancestors=frozenset({source}),
        read=read,
    )
    for restricted in (
        replace(authority, projects=frozenset()),
        SourceReadAuthority("user_b"),
        replace(authority, scope_keys=frozenset()),
    ):
        with pytest.raises(SourceUnavailableError):
            await read.source_snapshot(source, restricted)
    with pytest.raises(SourceUnavailableError):
        await read.source_snapshot(replace(source, organization_id="another_org"), authority)
    assert not await validate_observations(
        [snapshot.observation], authority, organization_id="another_org", read=read
    )

    assert not await validate_observations(
        [replace(snapshot.observation, source=replace(source, organization_id="another_org"))],
        authority,
        organization_id=org,
        read=read,
    )


async def test_graph_read_batch_reverse_references_keep_concurrent_validation(
    runtime, content_store, authority, monkeypatch
):
    import asyncio
    import hashlib

    from sibyl_core.services import validation_execution, validation_promotion
    from sibyl_core.services.content_models import raw_memory_record

    memory, _ = await capture(runtime, authority)
    captures = [
        raw_memory_record(replace(memory, id=identifier)) for identifier in ("raw_a", "raw_b")
    ]
    associations = [
        {
            "target_id": raw_id,
            "validation_entity_id": entity_id,
            "validation_binding_json": "{}",
            "active": True,
            "body_sha256": hashlib.sha256(memory.raw_content.encode()).hexdigest(),
        }
        for raw_id, entity_id in (("raw_a", "entity_a"), ("raw_b", "entity_b"))
    ]
    query = AsyncMock(
        return_value=[
            {
                "direct": associations,
                "published": [],
                "captures": captures,
                "associations": associations,
            }
        ]
    )
    monkeypatch.setattr(validation_execution, "_query", query)
    arrived = set()
    both = asyncio.Event()

    async def binding(memory, association):
        arrived.add(memory.id)
        if len(arrived) == 2:
            both.set()
        await both.wait()
        return True

    monkeypatch.setattr(validation_promotion, "validation_binding_current", binding)
    result = await asyncio.wait_for(
        validation_promotion.validated_graph_currents(
            runtime.client.group_id, ["entity_a", "entity_b"]
        ),
        timeout=2,
    )
    assert result == {"entity_a": True, "entity_b": True}
    assert arrived == {"raw_a", "raw_b"}
    query.assert_awaited_once()
    assert "validation_entity_id IN $entities" in query.call_args.args[0]


async def test_graph_read_batch_final_phase_covers_all_storage_batches(
    runtime, content_store, authority, relationship_authority, monkeypatch
):
    from sibyl_core.services import (
        content_client,
        graph_read_availability,
        operational_relationships,
    )

    memory, source = await capture(runtime, authority)
    await runtime.entity_manager.publish_operational_entities(source)
    ids = await runtime.relationship_manager.publish_operational_relationships(source)
    assert len(ids) > 2
    batches = (len(ids) + 1) // 2
    monkeypatch.setattr(graph_read_availability, "_READ_BATCH_SIZE", 2)
    assert set(
        await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    ) == set(ids)
    snapshot = operational_relationships._snapshot
    calls = 0

    async def boundary(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == batches + 1:
            async with content_client.surreal_content_client() as client:
                assert await client.execute_query(
                    "UPDATE source_states SET incarnation=type::string(rand::uuid()) "
                    "WHERE source_kind='raw_capture' AND source_id=$id;",
                    id=memory.id,
                )
        return await snapshot(*args, **kwargs)

    monkeypatch.setattr(operational_relationships, "_snapshot", boundary)
    assert not await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    assert calls == 2 * batches
