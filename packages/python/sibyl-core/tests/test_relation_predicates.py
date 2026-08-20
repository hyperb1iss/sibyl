"""The closed predicate vocabulary, from declaration to the edge on disk."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import sibyl_core.services.memory as memory_module
import sibyl_core.tools.add as add_module
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.entities import Entity, EntityType, RelationshipType
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.models.relations import (
    DECLARABLE_RELATIONSHIP_PREDICATES,
    PREDICATE_EXPANSION_PATH_SCORES,
    PREDICATE_HYBRID_MULTIPLIERS,
    PredicateDirection,
    PredicateLifecycleEffect,
    declared_relation_targets,
    parse_relation_declaration,
    parse_relation_declarations,
    predicate_direction_allows,
    predicate_policy,
)
from sibyl_core.retrieval.hybrid import DEFAULT_GRAPH_RELATIONSHIP_TYPE_WEIGHTS
from sibyl_core.retrieval.search import _GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS
from sibyl_core.services.graph import (
    EntityManager,
    GraphRuntime,
    RelationshipManager,
    SurrealGraphClient,
    prepare_graph_schema,
)
from sibyl_core.services.surreal_content import MemoryScope
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

    @pytest.mark.parametrize(
        "value",
        [
            "requires:a:b",
            "supersedes:has space",
            "supports:   ",
        ],
    )
    def test_implausible_remainder_is_not_a_target(self, value: str) -> None:
        """A prefix only splits when what follows could be a single id.

        Otherwise a stray URL or a mistyped entry gets torn into a predicate
        and a fragment, and the link silently points somewhere it was never
        asked to point.
        """
        declaration = parse_relation_declaration(value)
        assert declaration.target_id == value.strip()
        assert declaration.relationship_type is RelationshipType.RELATED_TO
        assert declaration.declared is False

    def test_an_id_spelled_like_a_declaration_is_read_as_one(self) -> None:
        """The one ambiguity syntax cannot settle, pinned so it stays known.

        `Entity.id` is an unconstrained string and restore validates whatever a
        backup carries, so `supersedes:legacy` is a representable id. Nothing
        in Sibyl mints that shape, and telling it apart from a declaration
        needs the store rather than the string.
        """
        declaration = parse_relation_declaration("supersedes:legacy")
        assert declaration.relationship_type is RelationshipType.SUPERSEDES
        assert declaration.target_id == "legacy"

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

    @pytest.mark.parametrize(
        ("predicate", "hybrid", "expansion", "direction", "lifecycle"),
        [
            (
                RelationshipType.SUPERSEDES,
                1.10,
                0.95,
                PredicateDirection.INCOMING,
                PredicateLifecycleEffect.HIDE_TARGET,
            ),
            (
                RelationshipType.CONTRADICTS,
                1.00,
                0.64,
                PredicateDirection.BOTH,
                PredicateLifecycleEffect.MARK_CONTESTED,
            ),
            (
                RelationshipType.REQUIRES,
                1.15,
                0.98,
                PredicateDirection.OUTGOING,
                PredicateLifecycleEffect.NONE,
            ),
            (
                RelationshipType.SUPPORTS,
                1.00,
                0.94,
                PredicateDirection.OUTGOING,
                PredicateLifecycleEffect.NONE,
            ),
            (
                RelationshipType.DECIDES,
                1.00,
                1.00,
                PredicateDirection.OUTGOING,
                PredicateLifecycleEffect.NONE,
            ),
            (
                RelationshipType.RELATED_TO,
                0.85,
                0.64,
                PredicateDirection.EXISTING,
                PredicateLifecycleEffect.NONE,
            ),
        ],
    )
    def test_canonical_predicate_contract(
        self,
        predicate: RelationshipType,
        hybrid: float,
        expansion: float,
        direction: PredicateDirection,
        lifecycle: PredicateLifecycleEffect,
    ) -> None:
        policy = predicate_policy(predicate)

        assert policy is not None
        assert policy.hybrid_multiplier == hybrid
        assert policy.expansion_path_score == expansion
        assert policy.direction is direction
        assert policy.lifecycle_effect is lifecycle
        assert policy.receipt_label == predicate.value.lower()

    def test_both_retrieval_engines_consume_canonical_numeric_policy(self) -> None:
        assert DEFAULT_GRAPH_RELATIONSHIP_TYPE_WEIGHTS is PREDICATE_HYBRID_MULTIPLIERS
        assert _GRAPH_EXPANSION_RELATIONSHIP_WEIGHTS is PREDICATE_EXPANSION_PATH_SCORES

    def test_canonical_policy_preserves_each_engines_existing_numeric_table(self) -> None:
        assert dict(PREDICATE_HYBRID_MULTIPLIERS) == {
            "APPLIES_TO": 1.10,
            "BELONGS_TO": 1.20,
            "BLOCKS": 1.25,
            "BREAKS": 1.15,
            "CONFLICTS_WITH": 1.10,
            "CONTAINS": 1.10,
            "CONTRADICTS": 1.00,
            "DECIDES": 1.00,
            "DEPENDS_ON": 1.25,
            "DERIVED_FROM": 1.05,
            "DOCUMENTED_IN": 1.00,
            "ENABLES": 1.15,
            "ENCOUNTERED": 1.10,
            "IMPLEMENTED": 1.15,
            "MENTIONS": 0.35,
            "REFERENCES": 1.15,
            "RELATED_TO": 0.85,
            "REQUIRES": 1.15,
            "SUPERSEDES": 1.10,
            "SUPPORTS": 1.00,
            "USES_PROCEDURE": 1.15,
            "VALIDATED_BY": 1.15,
        }
        assert dict(PREDICATE_EXPANSION_PATH_SCORES) == {
            "ABOUT": 0.78,
            "BELONGS_TO": 0.72,
            "BLOCKS": 0.96,
            "CONTAINS": 0.72,
            "CONTRADICTS": 0.64,
            "DECIDES": 1.00,
            "DEPENDS_ON": 0.98,
            "DERIVED_FROM": 0.70,
            "DOCUMENTED_IN": 0.66,
            "ENCOUNTERED": 0.86,
            "IMPLEMENTED": 0.90,
            "MENTIONS": 0.58,
            "PRODUCES": 0.82,
            "REFERENCES": 0.86,
            "RELATED_TO": 0.64,
            "REQUIRES": 0.98,
            "SHARES_COMMUNITY": 0.74,
            "SUPERSEDES": 0.95,
            "SUPPORTS": 0.94,
            "TOUCHES": 0.82,
            "USES_PROCEDURE": 0.92,
            "VALIDATED_BY": 0.94,
        }

    def test_declared_directions_are_enforced_without_changing_existing_defaults(self) -> None:
        assert predicate_direction_allows(RelationshipType.SUPERSEDES, "incoming") is True
        assert predicate_direction_allows(RelationshipType.SUPERSEDES, "outgoing") is False
        assert predicate_direction_allows(RelationshipType.CONTRADICTS, "incoming") is True
        assert predicate_direction_allows(RelationshipType.CONTRADICTS, "outgoing") is True
        assert predicate_direction_allows(RelationshipType.REQUIRES, "outgoing") is True
        assert predicate_direction_allows(RelationshipType.REQUIRES, "incoming") is False
        assert predicate_direction_allows(RelationshipType.DEPENDS_ON, "incoming") is True


class _WritableTargets:
    """Entity manager stub whose every target the writer is allowed to write."""

    def __init__(self, owner: str = "principal-a") -> None:
        self.owner = owner

    async def get(self, entity_id: str) -> Entity:
        return Entity(
            id=entity_id,
            entity_type=EntityType.EPISODE,
            name="Target",
            description="",
            content="",
            created_by=self.owner,
            metadata={"memory_scope": "private", "principal_id": self.owner},
        )


class TestEdgePayloads:
    @pytest.mark.asyncio
    async def test_untyped_payload_is_unchanged(self) -> None:
        [payload] = await add_module._declared_relationship_payloads("ep_new", ["ep_old"])
        assert payload["id"] == "rel_ep_new_related_to_ep_old"
        assert payload["source_id"] == "ep_new"
        assert payload["target_id"] == "ep_old"
        assert payload["type"] == "RELATED_TO"
        assert "agent_declared" not in payload["metadata"]

    @pytest.mark.asyncio
    async def test_declared_payload_carries_predicate_and_receipt(self) -> None:
        [payload] = await add_module._declared_relationship_payloads(
            "ep_new",
            ["supersedes:ep_old"],
            entity_manager=_WritableTargets(),
            principal_id="principal-a",
        )
        assert payload["id"] == "rel_ep_new_supersedes_ep_old"
        assert payload["source_id"] == "ep_new"
        assert payload["target_id"] == "ep_old"
        assert payload["type"] == "SUPERSEDES"
        assert payload["metadata"]["agent_declared"] is True

    @pytest.mark.asyncio
    async def test_two_predicates_at_one_target_do_not_collide(self) -> None:
        payloads = await add_module._declared_relationship_payloads(
            "ep_new",
            ["supersedes:ep_old", "contradicts:ep_old", "ep_old"],
            entity_manager=_WritableTargets(),
            principal_id="principal-a",
        )
        assert len({payload["id"] for payload in payloads}) == 3

    @pytest.mark.asyncio
    async def test_non_suppressing_predicates_need_no_target_lookup(self) -> None:
        """requires/supports/decides only raise a weight, so they stay ungated.

        Passing no entity manager proves the authorization path is not even
        consulted for them, which is what keeps the common case free of an
        extra read per declared link.
        """
        payloads = await add_module._declared_relationship_payloads(
            "ep_new", ["requires:ep_a", "supports:ep_b", "decides:ep_c"]
        )
        assert [payload["type"] for payload in payloads] == [
            "REQUIRES",
            "SUPPORTS",
            "DECIDES",
        ]
        assert all(payload["metadata"]["agent_declared"] for payload in payloads)


class TestSuppressionAuthorization:
    """SUPERSEDES and CONTRADICTS are claims about someone else's memory."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("predicate", ["supersedes", "contradicts"])
    async def test_unwritable_target_downgrades_to_related_to(self, predicate: str) -> None:
        [payload] = await add_module._declared_relationship_payloads(
            "ep_new",
            [f"{predicate}:ep_someone_elses"],
            entity_manager=_WritableTargets(owner="principal-b"),
            principal_id="principal-a",
        )
        assert payload["type"] == "RELATED_TO"
        assert payload["id"] == "rel_ep_new_related_to_ep_someone_elses"
        assert "agent_declared" not in payload["metadata"]
        # The link the agent asked for survives; only the claim is dropped.
        assert payload["target_id"] == "ep_someone_elses"

    @pytest.mark.asyncio
    async def test_missing_target_downgrades_rather_than_raising(self) -> None:
        class Missing:
            async def get(self, entity_id: str) -> None:
                return None

        [payload] = await add_module._declared_relationship_payloads(
            "ep_new",
            ["supersedes:ep_gone"],
            entity_manager=Missing(),
            principal_id="principal-a",
        )
        assert payload["type"] == "RELATED_TO"

    @pytest.mark.asyncio
    async def test_absent_entity_manager_fails_closed(self) -> None:
        [payload] = await add_module._declared_relationship_payloads(
            "ep_new", ["supersedes:ep_old"]
        )
        assert payload["type"] == "RELATED_TO"


PRINCIPAL = "principal-declaring"


async def _write_and_read_edges(
    monkeypatch: pytest.MonkeyPatch,
    *,
    group_id: str,
    related_to: list[str],
    target_owner: str = PRINCIPAL,
) -> tuple[str, list[dict[str, Any]]]:
    """Run a real `add()` against embedded Surreal and read back its edges.

    Targets are written as private memories owned by `target_owner`, so the
    default run is a writer superseding its own memory and passing a
    `target_owner` someone else owns exercises the refusal.
    """
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
                    created_by=target_owner,
                    metadata={
                        "organization_id": group_id,
                        "memory_scope": "private",
                        "principal_id": target_owner,
                    },
                ),
                generate_embedding=False,
            )

        response = await add(
            "Typed predicate write",
            "The memory that declares what it does to its neighbors.",
            related_to=related_to,
            metadata={"organization_id": group_id},
            principal_id=PRINCIPAL,
            memory_scope="private",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate", ["supersedes", "contradicts"])
async def test_suppressing_predicate_at_another_principals_memory_is_refused(
    predicate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read back from Surreal that the refusal reaches the stored edge.

    A writer that could bury another principal's memory by asserting it stale
    would turn the declaration channel into a denial of recall, so the edge
    that lands is the untyped one and carries no `agent_declared` receipt.
    """
    target = f"episode_{predicate[:6]}notmine"
    created_id, rows = await _write_and_read_edges(
        monkeypatch,
        group_id=f"org-refuse-{predicate}",
        related_to=[f"{predicate}:{target}"],
        target_owner="principal-someone-else",
    )

    [row] = [row for row in rows if row["target_id"] == target]
    assert row["name"] == "RELATED_TO"
    assert row["source_id"] == created_id
    assert "agent_declared" not in row["attributes"]


class TestPromotionLinkTargets:
    """Promotion reports edge counts, so its link list must not leak existence.

    `persist_reflection_candidate` returns how many relationships it requested
    and how many it wrote, and the graph writer silently skips an edge whose
    endpoint does not resolve. A target that exists therefore lands as a
    created edge and one that does not lands as a shortfall, which reads a
    guessed id back to the caller unless invisible and absent collapse to the
    same answer.
    """

    @staticmethod
    def _runtime(rows: dict[str, Entity]) -> Any:
        class Manager:
            async def get(self, entity_id: str) -> Entity:
                if entity_id not in rows:
                    raise KeyError(entity_id)
                return rows[entity_id]

        return SimpleNamespace(entity_manager=Manager())

    @staticmethod
    def _row(entity_id: str, **metadata: Any) -> Entity:
        return Entity(
            id=entity_id,
            entity_type=EntityType.EPISODE,
            name="Prior",
            description="",
            content="",
            metadata=metadata,
        )

    @pytest.mark.asyncio
    async def test_invisible_and_absent_targets_both_drop_out(self) -> None:
        hidden = self._row("ep_hidden", memory_scope="private", principal_id="principal-b")
        runtime = self._runtime({"ep_hidden": hidden})

        invisible = await memory_module._linkable_related_targets(
            runtime=runtime,
            related_to=["supersedes:ep_hidden"],
            principal_id="principal-a",
            accessible_projects=set(),
        )
        absent = await memory_module._linkable_related_targets(
            runtime=runtime,
            related_to=["supersedes:ep_absent"],
            principal_id="principal-a",
            accessible_projects=set(),
        )
        assert invisible == absent == []

    @pytest.mark.asyncio
    async def test_a_visible_target_still_links(self) -> None:
        runtime = self._runtime({"ep_visible": self._row("ep_visible")})
        linkable = await memory_module._linkable_related_targets(
            runtime=runtime,
            related_to=["supersedes:ep_visible", "ep_visible"],
            principal_id="principal-a",
            accessible_projects=set(),
        )
        assert linkable == ["ep_visible", "ep_visible"]

    def test_promotion_still_links_untyped(self) -> None:
        """The predicate is resolved to its target and then discarded here.

        SUPERSEDES on the promotion path is minted only from the authorized
        `supersedes` channel, so honoring a predicate declared on the free
        `related_to` list would route around that gate.
        """
        relationships = memory_module._relationships_for_promotion(
            "ep_new",
            project=None,
            source_id=None,
            related_to=["ep_visible"],
            supersedes=[],
            raw_source_ids=[],
        )
        assert [rel.relationship_type for rel in relationships] == [RelationshipType.RELATED_TO]

    @pytest.mark.asyncio
    async def test_a_transient_store_failure_is_not_a_missing_target(self) -> None:
        """An unreachable store must not masquerade as absence.

        The receipt reports requested and created counts, so a swallowed
        failure here reads as a promotion that was never asked to link
        anything: complete, zero requested, zero failed. The error has to
        surface instead.
        """

        class Unreachable:
            async def get(self, entity_id: str) -> Entity:
                raise TimeoutError("surreal unreachable")

        with pytest.raises(TimeoutError):
            await memory_module._linkable_related_targets(
                runtime=SimpleNamespace(entity_manager=Unreachable()),
                related_to=["supersedes:ep_target"],
                principal_id="principal-a",
                accessible_projects=set(),
            )

    @pytest.mark.asyncio
    async def test_both_absence_signals_still_drop_the_target(self) -> None:
        class Absent:
            def __init__(self, error: Exception) -> None:
                self.error = error

            async def get(self, entity_id: str) -> Entity:
                raise self.error

        for error in (KeyError("ep_gone"), EntityNotFoundError("episode", "ep_gone")):
            assert (
                await memory_module._linkable_related_targets(
                    runtime=SimpleNamespace(entity_manager=Absent(error)),
                    related_to=["supersedes:ep_gone"],
                    principal_id="principal-a",
                    accessible_projects=set(),
                )
                == []
            )

    @pytest.mark.asyncio
    async def test_nothing_is_persisted_when_resolution_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution runs before the row lands, so the promotion writes nothing.

        Resolving after `create_direct` left a persisted memory behind whenever
        the store faltered mid-promotion, and the response still called that
        complete.
        """
        created: list[Any] = []

        class Runtime:
            class entity_manager:
                @staticmethod
                async def create_direct(entity: Any) -> str:
                    created.append(entity)
                    return "decision_should_not_exist"

                @staticmethod
                async def get(entity_id: str) -> Entity:
                    raise TimeoutError("surreal unreachable")

            relationship_manager = SimpleNamespace()

        monkeypatch.setattr(
            memory_module,
            "get_surreal_graph_runtime",
            _always(Runtime()),
        )

        with pytest.raises(TimeoutError):
            await memory_module.persist_reflection_candidate(
                candidate=ReflectionCandidate(
                    kind="decision",
                    title="Decision: never lands",
                    content="The store falls over while resolving link targets.",
                    reason="covers the failure ordering",
                    confidence=0.9,
                    tags=["decision"],
                ),
                organization_id="org-transient",
                principal_id="principal-a",
                related_to=["supersedes:ep_target"],
                memory_scope=MemoryScope.PRIVATE,
            )

        assert created == []


def _always(value: Any) -> Any:
    async def _factory(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _factory
