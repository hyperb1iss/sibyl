"""The closed predicate vocabulary, from declaration to the edge on disk."""

from __future__ import annotations

from typing import Any

import pytest

import sibyl_core.tools.add as add_module
from sibyl_core.models.entities import Entity, EntityType, RelationshipType
from sibyl_core.models.relations import (
    DECLARABLE_RELATIONSHIP_PREDICATES,
    declared_relation_targets,
    parse_relation_declaration,
    parse_relation_declarations,
)
from sibyl_core.retrieval.search import _GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS
from sibyl_core.services.graph import (
    EntityManager,
    GraphRuntime,
    RelationshipManager,
    SurrealGraphClient,
    prepare_graph_schema,
)
from sibyl_core.tools.add import add

VOCABULARY = ("supersedes", "contradicts", "requires", "supports", "decides")


class TestParsing:
    def test_bare_id_is_untyped(self) -> None:
        declaration = parse_relation_declaration("ep_0a1b2c3d4e5f")
        assert declaration.target_id == "ep_0a1b2c3d4e5f"
        assert declaration.relationship_type is RelationshipType.RELATED_TO
        assert declaration.declared is False

    @pytest.mark.parametrize("predicate", VOCABULARY)
    def test_vocabulary_predicate_is_parsed(self, predicate: str) -> None:
        declaration = parse_relation_declaration(f"{predicate}:ep_0a1b2c3d4e5f")
        assert declaration.target_id == "ep_0a1b2c3d4e5f"
        assert declaration.relationship_type.value == predicate.upper()
        assert declaration.declared is True

    def test_predicate_is_case_insensitive(self) -> None:
        declaration = parse_relation_declaration("SuperSedes: ep_0a1b2c3d4e5f ")
        assert declaration.relationship_type is RelationshipType.SUPERSEDES
        assert declaration.target_id == "ep_0a1b2c3d4e5f"

    @pytest.mark.parametrize(
        "value",
        [
            "blocks:task_0a1b2c3d4e5f",
            "caused_by:ep_0a1b2c3d4e5f",
            "entity:0a1b2c3d4e5f",
            "supersedes:",
            "https://example.test/doc",
        ],
    )
    def test_unknown_predicate_keeps_the_whole_string(self, value: str) -> None:
        declaration = parse_relation_declaration(value)
        assert declaration.target_id == value
        assert declaration.relationship_type is RelationshipType.RELATED_TO
        assert declaration.declared is False

    def test_blank_entries_are_dropped(self) -> None:
        assert parse_relation_declarations(["", "   ", "ep_0a1b2c3d4e5f"]) == [
            parse_relation_declaration("ep_0a1b2c3d4e5f")
        ]

    def test_targets_strip_predicates(self) -> None:
        assert declared_relation_targets(
            ["supersedes:ep_aaaa", "ep_bbbb", "requires:task_cccc"]
        ) == ["ep_aaaa", "ep_bbbb", "task_cccc"]

    def test_vocabulary_is_closed_and_lives_in_the_enum(self) -> None:
        assert set(DECLARABLE_RELATIONSHIP_PREDICATES) == set(VOCABULARY)
        for predicate, relationship_type in DECLARABLE_RELATIONSHIP_PREDICATES.items():
            assert RelationshipType(predicate.upper()) is relationship_type

    def test_declared_predicates_are_not_scored_below_untyped(self) -> None:
        """A declaration must never cost a hop relative to leaving it untyped.

        The point of the vocabulary is that the expansion scorer already
        weights these predicates; a member that scored under `RELATED_TO`
        would make declaring the truth worse than saying nothing.
        """
        untyped = _GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS["RELATED_TO"]
        for relationship_type in DECLARABLE_RELATIONSHIP_PREDICATES.values():
            weight = _GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS.get(relationship_type.value, untyped)
            assert weight >= untyped, relationship_type


class TestEdgePayloads:
    def test_untyped_payload_is_unchanged(self) -> None:
        [payload] = add_module._declared_relationship_payloads("ep_new", ["ep_old"])
        assert payload["id"] == "rel_ep_new_related_to_ep_old"
        assert payload["source_id"] == "ep_new"
        assert payload["target_id"] == "ep_old"
        assert payload["type"] == "RELATED_TO"
        assert "agent_declared" not in payload["metadata"]

    def test_declared_payload_carries_predicate_and_receipt(self) -> None:
        [payload] = add_module._declared_relationship_payloads("ep_new", ["supersedes:ep_old"])
        assert payload["id"] == "rel_ep_new_supersedes_ep_old"
        assert payload["source_id"] == "ep_new"
        assert payload["target_id"] == "ep_old"
        assert payload["type"] == "SUPERSEDES"
        assert payload["metadata"]["agent_declared"] is True

    def test_two_predicates_at_one_target_do_not_collide(self) -> None:
        payloads = add_module._declared_relationship_payloads(
            "ep_new", ["supersedes:ep_old", "contradicts:ep_old", "ep_old"]
        )
        assert len({payload["id"] for payload in payloads}) == 3


async def _write_and_read_edges(
    monkeypatch: pytest.MonkeyPatch,
    *,
    group_id: str,
    related_to: list[str],
) -> tuple[str, list[dict[str, Any]]]:
    """Run a real `add()` against embedded Surreal and read back its edges."""
    client = SurrealGraphClient(group_id=group_id, url="memory://")
    await client.connect()
    try:
        await prepare_graph_schema(client)
        runtime = GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=group_id),
            relationship_manager=RelationshipManager(client, group_id=group_id),
        )

        async def runtime_factory(requested_group_id: str, **_kwargs: Any) -> GraphRuntime:
            assert requested_group_id == group_id
            return runtime

        monkeypatch.setattr(add_module, "get_graph_runtime", runtime_factory)

        # Auto-discovery and projection mint their own edges; this test is about
        # the ones the writer declared, so both are stubbed out.
        async def no_auto_links(**_kwargs: Any) -> list[tuple[str, float]]:
            return []

        monkeypatch.setattr(add_module, "_auto_discover_links", no_auto_links)

        for index, target in enumerate(declared_relation_targets(related_to)):
            await runtime.entity_manager.create_direct(
                Entity(
                    id=target,
                    entity_type=EntityType.EPISODE,
                    name=f"Existing memory {index}",
                    description="Prior memory the new write points at.",
                    content="Prior memory the new write points at.",
                    metadata={"organization_id": group_id},
                ),
                generate_embedding=False,
            )

        response = await add(
            "Typed predicate write",
            "The memory that declares what it does to its neighbors.",
            related_to=related_to,
            metadata={"organization_id": group_id},
            sync=True,
            generate_embeddings=False,
            check_conflicts=False,
        )
        assert response.success, response.message
        created_id = str(response.id)

        rows = await client.execute_query(
            """
            SELECT name, source_id, target_id, attributes
            FROM relates_to
            WHERE group_id = $group_id AND source_id = $source_id;
            """,
            group_id=group_id,
            source_id=created_id,
        )
        return created_id, _normalize(rows)
    finally:
        await client.close()


def _normalize(rows: Any) -> list[dict[str, Any]]:
    while isinstance(rows, list) and rows and isinstance(rows[0], dict) and "result" in rows[0]:
        rows = rows[0]["result"]
    return [dict(row) for row in rows or []]


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate", VOCABULARY)
async def test_declared_predicate_writes_that_edge_type(
    predicate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = f"episode_{predicate[:6]}00target"
    created_id, rows = await _write_and_read_edges(
        monkeypatch,
        group_id=f"org-predicate-{predicate}",
        related_to=[f"{predicate}:{target}"],
    )

    [row] = [row for row in rows if row["target_id"] == target]
    assert row["name"] == predicate.upper()
    # Direction: the memory being written is the subject, so it is the edge's
    # source and the declared target is the object. Expansion walks outgoing
    # edges from a seed, which is why this orientation is the load-bearing one.
    assert row["source_id"] == created_id
    assert row["attributes"]["agent_declared"] is True


@pytest.mark.asyncio
async def test_undeclared_target_still_writes_related_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "episode_plainbaretarget"
    created_id, rows = await _write_and_read_edges(
        monkeypatch,
        group_id="org-predicate-untyped",
        related_to=[target],
    )

    [row] = [row for row in rows if row["target_id"] == target]
    assert row["name"] == "RELATED_TO"
    assert row["source_id"] == created_id
    assert "agent_declared" not in row["attributes"]


@pytest.mark.asyncio
async def test_unknown_predicate_keeps_the_untyped_collapse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A predicate outside the vocabulary is never an error, only untyped.

    The whole string stays the target id, which is byte-for-byte what the
    write surface did before the vocabulary existed.
    """
    target = "blocks:episode_unknownpredic"
    _created_id, rows = await _write_and_read_edges(
        monkeypatch,
        group_id="org-predicate-unknown",
        related_to=[target],
    )

    [row] = [row for row in rows if row["target_id"] == target]
    assert row["name"] == "RELATED_TO"
    assert "agent_declared" not in row["attributes"]
