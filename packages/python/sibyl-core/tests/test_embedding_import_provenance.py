"""Restored and merged vectors keep their model, or arrive marked as unknown."""

from __future__ import annotations

from unittest.mock import AsyncMock

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.embeddings.provenance import (
    UNVERIFIED_EMBEDDING_PROVIDER,
    UNVERIFIED_ORIGIN_ARCHIVE,
)
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.migrate.merge import _merge_entity
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.embedding_sweep import SWEEP_COMPLETED, read_embedding_sweep_state
from sibyl_core.services.graph_embedding_sweep import (
    GRAPH_EMBEDDING_PLANE,
    sweep_graph_embeddings,
)
from sibyl_core.tools.admin import create_backup, restore_backup
from tests.test_reflection_identity import content_store as content_store
from tests.test_source_integrity_archive import destination as destination
from tests.test_source_integrity_archive import runtime as runtime
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)

PROVIDER = DeterministicEmbeddingProvider(
    EmbeddingMetadata(
        provider="deterministic",
        model="import-current",
        dimensions=EMBEDDING_DIM,
        cache_namespace="graph",
        tokenizer_estimate_method="utf8-byte-length",
    )
)
STAMP = PROVIDER.metadata.to_dict()


def _vector() -> list[float]:
    return [0.5, *([0.0] * (EMBEDDING_DIM - 1))]


async def _seed_source_graph(runtime) -> None:
    for entity_id, metadata, vector in (
        ("stamped", {"embedding_metadata": STAMP}, _vector()),
        ("legacy", {}, _vector()),
        ("lexical", {}, None),
    ):
        await runtime.entity_manager.create_direct(
            Entity(
                id=entity_id,
                entity_type=EntityType.TOPIC,
                name=f"Entity {entity_id}",
                metadata=metadata,
                embedding=vector,
            )
        )
    await runtime.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id=edge_id,
                relationship_type=RelationshipType.RELATED_TO,
                source_id="stamped",
                target_id=target,
                metadata={"fact": f"fact {edge_id}", "fact_embedding": _vector(), **metadata},
            )
            for edge_id, target, metadata in (
                ("edge-stamped", "legacy", {"embedding_metadata": STAMP}),
                ("edge-legacy", "lexical", {}),
            )
        ]
    )


async def _stamps(runtime, table: str) -> dict[str, object]:
    rows = normalize_records(
        await runtime.client.execute_query(
            f"SELECT uuid, attributes.embedding_metadata AS stamp FROM {table};"
        )
    )
    return {str(row["uuid"]): row.get("stamp") for row in rows}


async def test_restore_keeps_stamps_and_marks_unstamped_vectors_unverified(
    runtime, destination, monkeypatch
) -> None:
    await _seed_source_graph(runtime)
    # The destination already finished a pass for the configured model.
    await destination.entity_manager.create_direct(
        Entity(
            id="resident",
            entity_type=EntityType.TOPIC,
            name="Resident",
            metadata={"embedding_metadata": STAMP},
            embedding=_vector(),
        )
    )
    first = await sweep_graph_embeddings(destination, embedding_provider=PROVIDER)
    assert first.status == SWEEP_COMPLETED

    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.message
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    restored = await restore_backup(
        backup.backup_data, organization_id=runtime.client.group_id, clean=False
    )
    assert restored.success, restored.errors

    entities = await _stamps(destination, "entity")
    assert entities["stamped"] == STAMP
    assert entities["legacy"]["provider"] == UNVERIFIED_EMBEDDING_PROVIDER
    assert entities["legacy"]["origin"] == UNVERIFIED_ORIGIN_ARCHIVE
    assert entities["lexical"] is None
    edges = await _stamps(destination, "relates_to")
    assert edges["edge-stamped"] == STAMP
    assert edges["edge-legacy"]["provider"] == UNVERIFIED_EMBEDDING_PROVIDER
    state = await read_embedding_sweep_state(
        GRAPH_EMBEDDING_PLANE, destination.client.group_id, destination.client.execute_query
    )
    assert state.get("complete_metadata") is None

    swept = await sweep_graph_embeddings(destination, embedding_provider=PROVIDER)

    # Only the vectors that could not name their model are replaced.
    assert swept.status == SWEEP_COMPLETED
    assert (swept.recovered, swept.adopted) == (2, 0)
    entities = await _stamps(destination, "entity")
    assert all(entities[entity_id] == STAMP for entity_id in ("stamped", "legacy", "resident"))
    assert entities["lexical"] is None
    assert all(stamp == STAMP for stamp in (await _stamps(destination, "relates_to")).values())


def test_merge_moves_a_borrowed_vector_with_its_provenance() -> None:
    target = {"id": "a", "metadata": {"embedding_metadata": {"model": "cleared"}}}
    source = {"id": "b", "embedding": [0.1], "metadata": {"embedding_metadata": STAMP}}

    _merge_entity(target, source, source_org_id="org", entity_id="b")

    assert target["embedding"] == [0.1]
    assert target["metadata"]["embedding_metadata"] == STAMP

    unstamped_target = {"id": "a", "metadata": {"embedding_metadata": {"model": "cleared"}}}
    _merge_entity(
        unstamped_target,
        {"id": "c", "embedding": [0.2], "metadata": {}},
        source_org_id="org",
        entity_id="c",
    )
    assert "embedding_metadata" not in unstamped_target["metadata"]


def test_merge_never_lends_a_stamp_to_a_vector_it_did_not_produce() -> None:
    target = {"id": "a", "embedding": [0.1, 0.2], "metadata": {}}
    source = {"id": "b", "embedding": [0.3, 0.4], "metadata": {"embedding_metadata": STAMP}}

    _merge_entity(target, source, source_org_id="org", entity_id="b")

    assert target["embedding"] == [0.1, 0.2]
    assert "embedding_metadata" not in target["metadata"]


def test_an_update_that_keeps_the_vector_cannot_relabel_it() -> None:
    from sibyl_core.services.graph_entity_store import _entity_update_metadata_patch

    forged = {"provider": "forged", "model": "m", "dimensions": 3}

    kept_vector = _entity_update_metadata_patch(
        {"metadata": {"embedding_metadata": forged, "note": "x"}, "embedding_metadata": forged}
    )
    with_vector = _entity_update_metadata_patch(
        {"metadata": {"embedding_metadata": forged}, "embedding": [0.1, 0.2, 0.3]}
    )

    assert "embedding_metadata" not in kept_vector
    assert kept_vector["note"] == "x"
    assert with_vector["embedding_metadata"] == forged


async def test_a_metadata_only_update_keeps_the_stored_stamp(runtime) -> None:
    from sibyl_core.models.entities import Entity, EntityType
    from tests.test_embedding_sweep import _rows, _vector

    stamp = {"provider": "deterministic", "model": "real", "dimensions": len(_vector(0.5))}
    await runtime.entity_manager.create_direct(
        Entity(
            id="kept",
            entity_type=EntityType.TOPIC,
            name="Kept",
            metadata={"embedding_metadata": stamp},
            embedding=_vector(0.5),
        ),
        generate_embedding=False,
    )

    await runtime.entity_manager.update(
        "kept", {"metadata": {"embedding_metadata": {"provider": "forged"}, "note": "x"}}
    )

    row = (await _rows(runtime, "entity"))["kept"]
    assert row["stamp"] == stamp
    assert row["vector"] == _vector(0.5)


async def test_a_raw_write_never_keeps_a_client_embedding_stamp(content_store) -> None:
    from uuid import uuid4

    from sibyl_core.services import content_client
    from sibyl_core.services.surreal_content import remember_raw_memory

    memory = await remember_raw_memory(
        organization_id=str(uuid4()),
        principal_id="tenant",
        source_id="planted",
        raw_content="planted",
        embedding_provider=None,
        metadata={"embedding_metadata": {"provider": "forged"}, "kept": "yes"},
    )

    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client, "SELECT metadata FROM raw_captures WHERE uuid = $id;", id=memory.id
        )
    assert "embedding_metadata" not in rows[0]["metadata"]
    assert rows[0]["metadata"]["kept"] == "yes"
