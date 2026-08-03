"""Bounded traversal verbs: authorization, budgets, and window shape.

Every verb here is a read surface, so each behavior is asserted as a pair: the
row a reader is entitled to survives, and the row belonging to someone else does
not. Testing only the deny direction is how a scope fix ships as an outage, and
testing only the allow direction is how a leak ships as a feature.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.projection.passages import PASSAGE_COVERS_PARENT_KEY
from sibyl_core.retrieval.search import SearchFilter, expand_neighbor_records
from sibyl_core.tools.traverse import (
    DEFAULT_SLICE_WINDOW,
    MAX_EXPAND_LIMIT,
    MAX_EXPAND_ORIGINS,
    MAX_TRAVERSAL_DEPTH,
    expand_neighbors,
    fetch_slice,
)

ORG = "org_traverse"
OWNER = "owner-principal"
OTHER = "other-principal"

# A real grant, built the way the API mints one. Spelling this as "project:proj_a"
# denies every scope instead of just the private ones, which makes a deny test
# pass without exercising the rule it names.
PROJECT_GRANT = memory_scope_policy_key(MemoryScope.PROJECT, "proj_a")


def _entity_row(
    uuid: str,
    *,
    entity_type: str = "decision",
    name: str | None = None,
    content: str = "row content",
    memory_scope: str | None = None,
    principal_id: str | None = None,
    project_id: str | None = None,
    scope_key: str | None = None,
    extra_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A Surreal entity row shaped the way the hydration query returns one."""
    attributes: dict[str, Any] = {"description": "", "entity_type": entity_type}
    if memory_scope is not None:
        attributes["memory_scope"] = memory_scope
    if principal_id is not None:
        attributes["principal_id"] = principal_id
    if project_id is not None:
        attributes["project_id"] = project_id
    if scope_key is not None:
        attributes["scope_key"] = scope_key
    attributes.update(extra_attributes or {})
    return {
        "uuid": uuid,
        "name": name or uuid,
        "entity_type": entity_type,
        "content": content,
        "summary": "",
        "description": "",
        "attributes": attributes,
        "group_id": ORG,
        "memory_scope": memory_scope,
        "project_id": project_id,
        "created_at": datetime(2026, 8, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
        "revision": 1,
    }


def _entity(
    entity_id: str,
    *,
    entity_type: EntityType = EntityType.DECISION,
    content: str = "body",
    metadata: dict[str, Any] | None = None,
) -> Entity:
    return Entity(
        id=entity_id,
        entity_type=entity_type,
        name=entity_id,
        description="",
        content=content,
        organization_id=ORG,
        metadata=metadata or {},
    )


def _private(entity_id: str, owner: str, **kwargs: Any) -> Entity:
    return _entity(
        entity_id,
        metadata={"memory_scope": "private", "principal_id": owner},
        **kwargs,
    )


class _FakeClient:
    """Answers the four query shapes the expansion BFS issues."""

    def __init__(
        self,
        *,
        outgoing: list[dict[str, Any]] | None = None,
        incoming: list[dict[str, Any]] | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.outgoing = outgoing or []
        self.incoming = incoming or []
        self.rows = {row["uuid"]: row for row in (rows or [])}
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append((query, params))
        if "FROM mentions" in query:
            return []
        if "FROM relates_to" in query:
            if "BELONGS_TO" in query:
                return []
            if "target_id IN $target_uuids" in query:
                return list(self.incoming)
            return list(self.outgoing)
        if "FROM entity" in query:
            wanted = params.get("uuids") or []
            return [self.rows[uuid] for uuid in wanted if uuid in self.rows]
        return []


class _GraphClient:
    """A client backed by real adjacency, so multi-hop walks can be observed.

    ``_FakeClient`` answers every frontier with one canned edge list, which is
    enough for shape assertions and useless for anything about distance or about
    which rows a walk routes through.
    """

    def __init__(
        self,
        *,
        edges: list[tuple[str, str, str]],
        rows: list[dict[str, Any]],
    ) -> None:
        self.edges = edges
        self.rows = {row["uuid"]: row for row in rows}
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append((query, params))
        if "FROM mentions" in query or "BELONGS_TO" in query:
            return []
        if "FROM relates_to" in query:
            if "target_id IN $target_uuids" in query:
                wanted = set(params.get("target_uuids") or [])
                return [
                    {"uuid": source, "relationship": name}
                    for source, target, name in self.edges
                    if target in wanted
                ]
            wanted = set(params.get("source_uuids") or [])
            return [
                {"uuid": target, "relationship": name}
                for source, target, name in self.edges
                if source in wanted
            ]
        if "FROM entity" in query:
            requested = params.get("uuids") or []
            return [self.rows[uuid] for uuid in requested if uuid in self.rows]
        return []


class _FakeEntityManager:
    def __init__(self, entities: dict[str, Entity]) -> None:
        self.entities = entities

    async def get(self, entity_id: str) -> Entity:
        if entity_id not in self.entities:
            raise KeyError(entity_id)
        return self.entities[entity_id]

    async def get_many(self, entity_ids: Any) -> list[Entity]:
        return [self.entities[value] for value in entity_ids if value in self.entities]


class _FakeRelationshipManager:
    def __init__(self, pairs: list[tuple[Entity, Relationship]]) -> None:
        self.pairs = pairs
        self.calls: list[dict[str, Any]] = []

    async def get_related_entities(
        self,
        entity_id: str,
        relationship_types: Any = None,
        max_depth: int = 1,
        limit: int = 50,
    ) -> list[tuple[Entity, Relationship]]:
        self.calls.append(
            {
                "entity_id": entity_id,
                "relationship_types": list(relationship_types or ()),
                "limit": limit,
            }
        )
        return list(self.pairs)


class _FakeRuntime:
    def __init__(
        self,
        *,
        client: Any = None,
        entity_manager: Any = None,
        relationship_manager: Any = None,
    ) -> None:
        self.client = client
        self.entity_manager = entity_manager
        self.relationship_manager = relationship_manager


def _runtime_patch(runtime: _FakeRuntime) -> Any:
    return patch(
        "sibyl_core.tools.traverse.get_graph_runtime",
        AsyncMock(return_value=runtime),
    )


def _span(
    parent_id: str,
    index: int,
    total: int,
    *,
    memory_scope: str | None = None,
    principal_id: str | None = None,
    covers_parent: bool = True,
) -> Entity:
    metadata: dict[str, Any] = {
        "parent_entity_id": parent_id,
        "passage_index": index,
        "passage_total": total,
        "passage_breadcrumb": f"trail {index}",
        PASSAGE_COVERS_PARENT_KEY: covers_parent,
    }
    if memory_scope is not None:
        metadata["memory_scope"] = memory_scope
    if principal_id is not None:
        metadata["principal_id"] = principal_id
    return _entity(
        f"{parent_id}_passage_{index}",
        entity_type=EntityType.PASSAGE,
        content=f"span {index} body",
        metadata=metadata,
    )


def _part_of(span: Entity, parent_id: str) -> Relationship:
    return Relationship(
        id=f"rel_{span.id}",
        source_id=span.id,
        target_id=parent_id,
        relationship_type=RelationshipType.PART_OF,
    )


class TestExpandNeighborRecords:
    """The primitive: hop tagging, authorization before the cap, and filters."""

    @pytest.mark.asyncio
    async def test_tags_hops_and_hydrates_rows(self) -> None:
        client = _FakeClient(
            outgoing=[{"uuid": "decision_b", "relationship": "SUPERSEDES"}],
            rows=[_entity_row("decision_b")],
        )

        records = await expand_neighbor_records(
            client=client,
            origin_uuids=["decision_a"],
            group_id=ORG,
            max_depth=1,
            limit=5,
        )

        assert [record["uuid"] for record in records] == ["decision_b"]
        assert records[0]["graph_expansion_relationship"] == "SUPERSEDES"
        assert records[0]["graph_expansion_depth"] == 1
        assert records[0]["graph_expansion_direction"] == "outgoing"

    @pytest.mark.asyncio
    async def test_walks_inbound_edges_and_marks_direction(self) -> None:
        client = _FakeClient(
            incoming=[{"uuid": "task_dependent", "relationship": "DEPENDS_ON"}],
            rows=[_entity_row("task_dependent", entity_type="task")],
        )

        records = await expand_neighbor_records(
            client=client,
            origin_uuids=["task_blocker"],
            group_id=ORG,
            max_depth=1,
            limit=5,
        )

        assert [record["graph_expansion_direction"] for record in records] == ["incoming"]

    @pytest.mark.asyncio
    async def test_inbound_stays_off_for_the_scored_lane(self) -> None:
        """The lane that seeds from a scored search must be unchanged."""
        client = _FakeClient(
            incoming=[{"uuid": "task_dependent", "relationship": "DEPENDS_ON"}],
            rows=[_entity_row("task_dependent", entity_type="task")],
        )

        records = await expand_neighbor_records(
            client=client,
            origin_uuids=["task_blocker"],
            group_id=ORG,
            max_depth=1,
            limit=5,
            include_incoming=False,
        )

        assert records == []
        assert not any("target_id IN $target_uuids" in query for query, _ in client.queries)

    @pytest.mark.asyncio
    async def test_relationship_filter_reaches_the_query(self) -> None:
        client = _FakeClient(
            outgoing=[{"uuid": "decision_b", "relationship": "DEPENDS_ON"}],
            rows=[_entity_row("decision_b")],
        )

        await expand_neighbor_records(
            client=client,
            origin_uuids=["decision_a"],
            group_id=ORG,
            max_depth=1,
            limit=5,
            relationship_names=["DEPENDS_ON"],
        )

        edge_queries = [params for query, params in client.queries if "FROM relates_to" in query]
        assert edge_queries
        assert all(params["relationship_names"] == ["DEPENDS_ON"] for params in edge_queries)

    @pytest.mark.asyncio
    async def test_authorization_runs_before_the_cap(self) -> None:
        """A denied row must not consume the caller's budget.

        Filtering after the cap is the bug this asserts against: two denied rows
        ranked above one allowed row would fill a limit of two and report the
        neighborhood as empty.
        """
        client = _FakeClient(
            outgoing=[
                {"uuid": "denied_1", "relationship": "DECIDES"},
                {"uuid": "denied_2", "relationship": "DECIDES"},
                {"uuid": "allowed", "relationship": "RELATED_TO"},
            ],
            rows=[
                _entity_row("denied_1"),
                _entity_row("denied_2"),
                _entity_row("allowed"),
            ],
        )

        records = await expand_neighbor_records(
            client=client,
            origin_uuids=["decision_a"],
            group_id=ORG,
            max_depth=1,
            limit=2,
            row_allowed=lambda row: not str(row.get("uuid", "")).startswith("denied"),
        )

        assert [record["uuid"] for record in records] == ["allowed"]

    @pytest.mark.asyncio
    async def test_trims_to_the_requested_limit(self) -> None:
        client = _FakeClient(
            outgoing=[
                {"uuid": f"decision_{index}", "relationship": "RELATED_TO"} for index in range(6)
            ],
            rows=[_entity_row(f"decision_{index}") for index in range(6)],
        )

        records = await expand_neighbor_records(
            client=client,
            origin_uuids=["decision_seed"],
            group_id=ORG,
            max_depth=1,
            limit=3,
            search_filter=SearchFilter(),
        )

        assert len(records) == 3


class TestExpandNeighborsAuthorization:
    """The allow and deny directions of the verb, asserted as a pair."""

    @staticmethod
    def _neighbors_runtime(rows: list[dict[str, Any]], seeds: dict[str, Entity]) -> _FakeRuntime:
        return _FakeRuntime(
            client=_FakeClient(
                outgoing=[{"uuid": row["uuid"], "relationship": "RELATED_TO"} for row in rows],
                rows=rows,
            ),
            entity_manager=_FakeEntityManager(seeds),
        )

    @pytest.mark.asyncio
    async def test_reader_sees_own_private_and_project_neighbors(self) -> None:
        runtime = self._neighbors_runtime(
            [
                _entity_row("own_private", memory_scope="private", principal_id=OWNER),
                _entity_row(
                    "project_row",
                    memory_scope="project",
                    scope_key="proj_a",
                    project_id="proj_a",
                ),
            ],
            {"seed": _entity("seed")},
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects={"proj_a"},
            )

        assert {neighbor.id for neighbor in response.neighbors} == {"own_private", "project_row"}
        assert response.origins == ["seed"]

    @pytest.mark.asyncio
    async def test_another_principals_private_neighbor_is_absent(self) -> None:
        runtime = self._neighbors_runtime(
            [
                _entity_row("own_private", memory_scope="private", principal_id=OWNER),
                _entity_row("their_private", memory_scope="private", principal_id=OTHER),
            ],
            {"seed": _entity("seed")},
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert [neighbor.id for neighbor in response.neighbors] == ["own_private"]

    @pytest.mark.asyncio
    async def test_unrequested_projects_rows_drop(self) -> None:
        runtime = self._neighbors_runtime(
            [
                _entity_row(
                    "mine",
                    memory_scope="project",
                    scope_key="proj_a",
                    project_id="proj_a",
                ),
                _entity_row(
                    "theirs",
                    memory_scope="project",
                    scope_key="proj_b",
                    project_id="proj_b",
                ),
            ],
            {"seed": _entity("seed")},
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects={"proj_a"},
            )

        assert [neighbor.id for neighbor in response.neighbors] == ["mine"]

    @pytest.mark.asyncio
    async def test_unstamped_work_item_of_another_project_drops(self) -> None:
        """A task carries its audience in project_id rather than a scope stamp."""
        runtime = self._neighbors_runtime(
            [
                _entity_row("task_mine", entity_type="task", project_id="proj_a"),
                _entity_row("task_theirs", entity_type="task", project_id="proj_b"),
            ],
            {"seed": _entity("seed")},
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects={"proj_a"},
            )

        assert [neighbor.id for neighbor in response.neighbors] == ["task_mine"]

    @pytest.mark.asyncio
    async def test_seed_the_reader_cannot_read_yields_no_walk(self) -> None:
        """Expanding from an unreadable seed would leak its neighbor list."""
        client = _FakeClient(
            outgoing=[{"uuid": "neighbor", "relationship": "RELATED_TO"}],
            rows=[_entity_row("neighbor")],
        )
        runtime = _FakeRuntime(
            client=client,
            entity_manager=_FakeEntityManager({"secret": _private("secret", OTHER)}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["secret"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert response.origins == []
        assert response.unresolved == ["secret"]
        assert response.neighbors == []
        assert client.queries == []

    @pytest.mark.asyncio
    async def test_owner_may_expand_from_their_own_private_seed(self) -> None:
        client = _FakeClient(
            outgoing=[{"uuid": "neighbor", "relationship": "RELATED_TO"}],
            rows=[_entity_row("neighbor")],
        )
        runtime = _FakeRuntime(
            client=client,
            entity_manager=_FakeEntityManager({"secret": _private("secret", OWNER)}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["secret"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert response.origins == ["secret"]
        assert [neighbor.id for neighbor in response.neighbors] == ["neighbor"]

    @pytest.mark.asyncio
    async def test_missing_and_denied_seeds_are_reported_alike(self) -> None:
        runtime = _FakeRuntime(
            client=_FakeClient(),
            entity_manager=_FakeEntityManager({"secret": _private("secret", OTHER)}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["secret", "never_existed"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert sorted(response.unresolved) == ["never_existed", "secret"]


class TestExpandNeighborsBudget:
    @pytest.mark.asyncio
    async def test_depth_and_limit_clamp_to_the_ceiling(self) -> None:
        rows = [_entity_row(f"decision_{index}") for index in range(MAX_EXPAND_LIMIT + 4)]
        runtime = _FakeRuntime(
            client=_FakeClient(
                outgoing=[{"uuid": row["uuid"], "relationship": "RELATED_TO"} for row in rows],
                rows=rows,
            ),
            entity_manager=_FakeEntityManager({"seed": _entity("seed")}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                depth=99,
                limit=999,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert response.depth == MAX_TRAVERSAL_DEPTH
        assert response.limit == MAX_EXPAND_LIMIT
        assert len(response.neighbors) == MAX_EXPAND_LIMIT
        assert response.truncated is True

    @pytest.mark.asyncio
    async def test_seed_overflow_is_reported_rather_than_silently_dropped(self) -> None:
        seed_ids = [f"seed_{index}" for index in range(MAX_EXPAND_ORIGINS + 2)]
        runtime = _FakeRuntime(
            client=_FakeClient(),
            entity_manager=_FakeEntityManager({seed_id: _entity(seed_id) for seed_id in seed_ids}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                seed_ids,
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert len(response.origins) == MAX_EXPAND_ORIGINS
        assert response.unresolved == seed_ids[MAX_EXPAND_ORIGINS:]
        assert response.filters["origin_limit"] == MAX_EXPAND_ORIGINS

    @pytest.mark.asyncio
    async def test_previews_are_bounded_and_flagged(self) -> None:
        runtime = _FakeRuntime(
            client=_FakeClient(
                outgoing=[{"uuid": "long_row", "relationship": "RELATED_TO"}],
                rows=[_entity_row("long_row", content="word " * 400)],
            ),
            entity_manager=_FakeEntityManager({"seed": _entity("seed")}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                content_max_chars=50,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        neighbor = response.neighbors[0]
        assert len(neighbor.content) <= 53
        assert neighbor.metadata["content_truncated"] is True

    @pytest.mark.asyncio
    async def test_passage_neighbor_names_its_widening_move(self) -> None:
        runtime = _FakeRuntime(
            client=_FakeClient(
                outgoing=[{"uuid": "span_row", "relationship": "PART_OF"}],
                rows=[
                    _entity_row(
                        "span_row",
                        entity_type="passage",
                        extra_attributes={
                            "passage_index": 2,
                            "passage_total": 5,
                            "parent_entity_id": "decision_parent",
                        },
                    )
                ],
            ),
            entity_manager=_FakeEntityManager({"seed": _entity("seed")}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        metadata = response.neighbors[0].metadata
        assert metadata["passage_index"] == 2
        assert metadata["parent_entity_id"] == "decision_parent"
        assert metadata["widen_with"] == "fetch_slice"

    @pytest.mark.asyncio
    async def test_no_seeds_returns_empty_without_touching_the_graph(self) -> None:
        runtime = _FakeRuntime(client=_FakeClient(), entity_manager=_FakeEntityManager({}))

        with _runtime_patch(runtime):
            response = await expand_neighbors([], organization_id=ORG, principal_id=OWNER)

        assert response.total == 0
        assert response.neighbors == []

    @pytest.mark.asyncio
    async def test_organization_is_required(self) -> None:
        with pytest.raises(ValueError, match="organization_id"):
            await expand_neighbors(["seed"], organization_id="")


class TestFetchSliceWindow:
    @staticmethod
    def _sliced_runtime(
        parent: Entity,
        spans: list[Entity],
    ) -> _FakeRuntime:
        entities = {parent.id: parent, **{span.id: span for span in spans}}
        return _FakeRuntime(
            entity_manager=_FakeEntityManager(entities),
            relationship_manager=_FakeRelationshipManager(
                [(span, _part_of(span, parent.id)) for span in spans]
            ),
        )

    @pytest.mark.asyncio
    async def test_window_centers_on_the_named_span(self) -> None:
        parent = _entity("decision_parent", content="whole body")
        spans = [_span("decision_parent", index, 5) for index in range(5)]
        runtime = self._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                spans[2].id,
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert [passage.passage_index for passage in response.passages] == [1, 2, 3]
        assert response.window == DEFAULT_SLICE_WINDOW
        assert response.window_start == 1
        assert response.parent_id == "decision_parent"
        assert response.sliced is True

    @pytest.mark.asyncio
    async def test_window_slides_inside_the_available_spans(self) -> None:
        parent = _entity("decision_parent")
        spans = [_span("decision_parent", index, 4) for index in range(4)]
        runtime = self._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            first = await fetch_slice(spans[0].id, organization_id=ORG, principal_id=OWNER)
            last = await fetch_slice(spans[3].id, organization_id=ORG, principal_id=OWNER)

        assert [passage.passage_index for passage in first.passages] == [0, 1, 2]
        assert [passage.passage_index for passage in last.passages] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_parent_id_starts_the_window_at_the_first_span(self) -> None:
        parent = _entity("decision_parent")
        spans = [_span("decision_parent", index, 5) for index in range(5)]
        runtime = self._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_parent",
                organization_id=ORG,
                principal_id=OWNER,
            )

        assert [passage.passage_index for passage in response.passages] == [0, 1, 2]
        assert response.passage_total == 5

    @pytest.mark.asyncio
    async def test_spans_come_back_in_index_order(self) -> None:
        parent = _entity("decision_parent")
        spans = [_span("decision_parent", index, 3) for index in range(3)]
        runtime = self._sliced_runtime(parent, list(reversed(spans)))

        with _runtime_patch(runtime):
            response = await fetch_slice("decision_parent", organization_id=ORG, principal_id=OWNER)

        assert [passage.passage_index for passage in response.passages] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_unsliced_memory_returns_its_whole_body(self) -> None:
        parent = _entity("decision_short", content="short enough to serve whole")
        runtime = _FakeRuntime(
            entity_manager=_FakeEntityManager({parent.id: parent}),
            relationship_manager=_FakeRelationshipManager([]),
        )

        with _runtime_patch(runtime):
            response = await fetch_slice("decision_short", organization_id=ORG, principal_id=OWNER)

        assert response.sliced is False
        assert response.total == 1
        assert response.passages[0].content == "short enough to serve whole"
        assert response.passages[0].passage_index is None

    @pytest.mark.asyncio
    async def test_window_shares_one_character_budget(self) -> None:
        parent = _entity("decision_parent")
        spans = [_span("decision_parent", index, 3) for index in range(3)]
        runtime = self._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_parent",
                organization_id=ORG,
                content_max_chars=12,
                principal_id=OWNER,
            )

        assert response.content_chars <= 12 + len("...")
        assert any(passage.truncated for passage in response.passages)

    @pytest.mark.asyncio
    async def test_partial_projection_does_not_claim_to_cover_the_parent(self) -> None:
        parent = _entity("decision_parent")
        spans = [
            _span("decision_parent", 0, 2, covers_parent=True),
            _span("decision_parent", 1, 2, covers_parent=False),
        ]
        runtime = self._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice("decision_parent", organization_id=ORG, principal_id=OWNER)

        assert response.covers_parent is False

    @pytest.mark.asyncio
    async def test_window_clamps_below_one(self) -> None:
        parent = _entity("decision_parent")
        spans = [_span("decision_parent", index, 3) for index in range(3)]
        runtime = self._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_parent",
                organization_id=ORG,
                window=0,
                principal_id=OWNER,
            )

        assert response.window == 1
        assert len(response.passages) == 1


class TestFetchSliceAuthorization:
    @pytest.mark.asyncio
    async def test_owner_reads_the_window_of_their_private_memory(self) -> None:
        parent = _private("decision_secret", OWNER)
        spans = [
            _span("decision_secret", index, 3, memory_scope="private", principal_id=OWNER)
            for index in range(3)
        ]
        runtime = TestFetchSliceWindow._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_secret",
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert len(response.passages) == 3

    @pytest.mark.asyncio
    async def test_another_principals_private_memory_is_not_found(self) -> None:
        parent = _private("decision_secret", OTHER)
        spans = [
            _span("decision_secret", index, 3, memory_scope="private", principal_id=OTHER)
            for index in range(3)
        ]
        runtime = TestFetchSliceWindow._sliced_runtime(parent, spans)

        with _runtime_patch(runtime), pytest.raises(KeyError):
            await fetch_slice(
                "decision_secret",
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

    @pytest.mark.asyncio
    async def test_absent_and_denied_ids_fail_identically(self) -> None:
        runtime = _FakeRuntime(
            entity_manager=_FakeEntityManager({"secret": _private("secret", OTHER)}),
            relationship_manager=_FakeRelationshipManager([]),
        )

        with _runtime_patch(runtime):
            with pytest.raises(KeyError):
                await fetch_slice("secret", organization_id=ORG, principal_id=OWNER)
            with pytest.raises(KeyError):
                await fetch_slice("never_existed", organization_id=ORG, principal_id=OWNER)

    @pytest.mark.asyncio
    async def test_a_span_the_reader_may_not_read_leaves_the_window(self) -> None:
        """A span carries its own scope, so one may be denied inside a readable parent."""
        parent = _entity(
            "decision_parent",
            metadata={"memory_scope": "project", "scope_key": "proj_a", "project_id": "proj_a"},
        )
        spans = [
            _span("decision_parent", 0, 3),
            _span("decision_parent", 1, 3, memory_scope="private", principal_id=OTHER),
            _span("decision_parent", 2, 3),
        ]
        runtime = TestFetchSliceWindow._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_parent",
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects={"proj_a"},
            )

        assert [passage.passage_index for passage in response.passages] == [0, 2]

    @pytest.mark.asyncio
    async def test_a_span_whose_parent_is_denied_still_serves_itself(self) -> None:
        span = _span("decision_secret", 1, 3)
        runtime = _FakeRuntime(
            entity_manager=_FakeEntityManager(
                {span.id: span, "decision_secret": _private("decision_secret", OTHER)}
            ),
            relationship_manager=_FakeRelationshipManager([]),
        )

        with _runtime_patch(runtime):
            response = await fetch_slice(
                span.id,
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert response.total == 1
        assert response.filters["parent_unreadable"] is True

    @pytest.mark.asyncio
    async def test_api_key_without_a_private_grant_cannot_read_its_own_rows(self) -> None:
        """A key narrowed to a project does not inherit the principal's privates."""
        parent = _private("decision_secret", OWNER)
        spans = [
            _span("decision_secret", index, 2, memory_scope="private", principal_id=OWNER)
            for index in range(2)
        ]
        runtime = TestFetchSliceWindow._sliced_runtime(parent, spans)

        with _runtime_patch(runtime), pytest.raises(KeyError):
            await fetch_slice(
                "decision_secret",
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects={"proj_a"},
                allowed_memory_scope_keys={PROJECT_GRANT},
            )

    @pytest.mark.asyncio
    async def test_entity_id_is_required(self) -> None:
        with pytest.raises(ValueError, match="entity_id"):
            await fetch_slice("  ", organization_id=ORG)


class TestWalkAuthorizationGatesRoutes:
    """A row the reader may not see must not be a route, only a non-result."""

    @pytest.mark.asyncio
    async def test_a_denied_row_is_not_a_route_to_its_own_neighbors(self) -> None:
        """seed -> secret -> leaf, where secret belongs to another principal.

        Withholding `secret` while still returning `leaf` would disclose that
        something sits between them: a depth-2 row with no depth-1 parent in the
        response is an admission of a hidden intermediate.
        """
        client = _GraphClient(
            edges=[
                ("seed", "secret", "RELATED_TO"),
                ("secret", "leaf", "RELATED_TO"),
            ],
            rows=[
                _entity_row("secret", memory_scope="private", principal_id=OTHER),
                _entity_row("leaf"),
            ],
        )
        runtime = _FakeRuntime(
            client=client,
            entity_manager=_FakeEntityManager({"seed": _entity("seed")}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                depth=2,
                limit=8,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert [neighbor.id for neighbor in response.neighbors] == []

    @pytest.mark.asyncio
    async def test_the_same_route_is_walked_when_the_middle_row_is_readable(self) -> None:
        """The allow direction of the same graph, so the deny is not vacuous."""
        client = _GraphClient(
            edges=[
                ("seed", "middle", "RELATED_TO"),
                ("middle", "leaf", "RELATED_TO"),
            ],
            rows=[_entity_row("middle"), _entity_row("leaf")],
        )
        runtime = _FakeRuntime(
            client=client,
            entity_manager=_FakeEntityManager({"seed": _entity("seed")}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                depth=2,
                limit=8,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        by_id = {neighbor.id: neighbor for neighbor in response.neighbors}
        assert set(by_id) == {"middle", "leaf"}
        assert by_id["middle"].distance == 1
        assert by_id["leaf"].distance == 2

    @pytest.mark.asyncio
    async def test_a_seed_is_never_returned_as_its_own_neighbor(self) -> None:
        """Every inbound edge is a 2-cycle, so depth 2 rediscovers the seed."""
        client = _GraphClient(
            edges=[("seed", "neighbor", "RELATED_TO")],
            rows=[_entity_row("seed"), _entity_row("neighbor")],
        )
        runtime = _FakeRuntime(
            client=client,
            entity_manager=_FakeEntityManager({"seed": _entity("seed")}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                depth=2,
                limit=8,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        assert [neighbor.id for neighbor in response.neighbors] == ["neighbor"]
        assert "seed" not in {neighbor.id for neighbor in response.neighbors}

    @pytest.mark.asyncio
    async def test_deeper_hops_score_below_adjacent_ones(self) -> None:
        client = _GraphClient(
            edges=[
                ("seed", "near", "RELATED_TO"),
                ("near", "far", "RELATED_TO"),
            ],
            rows=[_entity_row("near"), _entity_row("far")],
        )
        runtime = _FakeRuntime(
            client=client,
            entity_manager=_FakeEntityManager({"seed": _entity("seed")}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                depth=2,
                limit=8,
                principal_id=OWNER,
                accessible_projects=set(),
            )

        by_id = {neighbor.id: neighbor for neighbor in response.neighbors}
        assert by_id["far"].score < by_id["near"].score

    @pytest.mark.asyncio
    async def test_api_key_without_a_private_grant_sees_no_private_neighbor(self) -> None:
        """A key narrowed to a project does not inherit the principal's privates."""
        rows = [
            _entity_row("own_private", memory_scope="private", principal_id=OWNER),
            _entity_row(
                "project_row",
                memory_scope="project",
                scope_key="proj_a",
                project_id="proj_a",
            ),
        ]
        runtime = _FakeRuntime(
            client=_FakeClient(
                outgoing=[{"uuid": row["uuid"], "relationship": "RELATED_TO"} for row in rows],
                rows=rows,
            ),
            entity_manager=_FakeEntityManager({"seed": _entity("seed")}),
        )

        with _runtime_patch(runtime):
            response = await expand_neighbors(
                ["seed"],
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects={"proj_a"},
                allowed_memory_scope_keys={PROJECT_GRANT},
            )

        assert [neighbor.id for neighbor in response.neighbors] == ["project_row"]


class TestFetchSliceCoversParent:
    """`covers_parent` is a claim a reader acts on by dropping the parent."""

    @pytest.mark.asyncio
    async def test_a_partial_window_does_not_claim_to_cover_the_parent(self) -> None:
        """The bug this pins: 3 spans of 4 all carry the flag, but miss a quarter."""
        parent = _entity("decision_parent")
        spans = [_span("decision_parent", index, 4) for index in range(4)]
        runtime = TestFetchSliceWindow._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_parent",
                organization_id=ORG,
                window=3,
                principal_id=OWNER,
            )

        assert [passage.passage_index for passage in response.passages] == [0, 1, 2]
        assert response.passage_total == 4
        assert response.covers_parent is False

    @pytest.mark.asyncio
    async def test_a_complete_window_does_claim_to_cover_the_parent(self) -> None:
        parent = _entity("decision_parent")
        spans = [_span("decision_parent", index, 3) for index in range(3)]
        runtime = TestFetchSliceWindow._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_parent",
                organization_id=ORG,
                window=3,
                principal_id=OWNER,
            )

        assert [passage.passage_index for passage in response.passages] == [0, 1, 2]
        assert response.covers_parent is True

    @pytest.mark.asyncio
    async def test_spans_from_two_generations_do_not_claim_coverage(self) -> None:
        """A stale higher-index span can count to the right number and still lie."""
        parent = _entity("decision_parent")
        spans = [
            _span("decision_parent", 0, 2),
            _span("decision_parent", 1, 2),
            _span("decision_parent", 2, 3),
        ]
        runtime = TestFetchSliceWindow._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_parent",
                organization_id=ORG,
                window=3,
                principal_id=OWNER,
            )

        assert response.covers_parent is False

    @pytest.mark.asyncio
    async def test_a_denied_sibling_costs_the_window_its_coverage_claim(self) -> None:
        parent = _entity(
            "decision_parent",
            metadata={"memory_scope": "project", "scope_key": "proj_a", "project_id": "proj_a"},
        )
        spans = [
            _span("decision_parent", 0, 3),
            _span("decision_parent", 1, 3, memory_scope="private", principal_id=OTHER),
            _span("decision_parent", 2, 3),
        ]
        runtime = TestFetchSliceWindow._sliced_runtime(parent, spans)

        with _runtime_patch(runtime):
            response = await fetch_slice(
                "decision_parent",
                organization_id=ORG,
                principal_id=OWNER,
                accessible_projects={"proj_a"},
            )

        assert [passage.passage_index for passage in response.passages] == [0, 2]
        assert response.covers_parent is False

    @pytest.mark.asyncio
    async def test_an_unsliced_memory_covers_itself(self) -> None:
        parent = _entity("decision_short", content="served whole")
        runtime = _FakeRuntime(
            entity_manager=_FakeEntityManager({parent.id: parent}),
            relationship_manager=_FakeRelationshipManager([]),
        )

        with _runtime_patch(runtime):
            response = await fetch_slice("decision_short", organization_id=ORG, principal_id=OWNER)

        assert response.sliced is False
        assert response.covers_parent is True


class TestFetchSliceAnchorNotInSpanSet:
    @pytest.mark.asyncio
    async def test_a_window_never_silently_omits_the_span_it_was_asked_for(self) -> None:
        """The anchor is authorized but absent from its parent's discovered set."""
        parent = _entity("decision_parent")
        siblings = [_span("decision_parent", index, 9) for index in range(3)]
        anchor = _span("decision_parent", 7, 9)
        entities = {
            parent.id: parent,
            anchor.id: anchor,
            **{span.id: span for span in siblings},
        }
        runtime = _FakeRuntime(
            entity_manager=_FakeEntityManager(entities),
            relationship_manager=_FakeRelationshipManager(
                [(span, _part_of(span, parent.id)) for span in siblings]
            ),
        )

        with _runtime_patch(runtime):
            response = await fetch_slice(anchor.id, organization_id=ORG, principal_id=OWNER)

        assert [passage.id for passage in response.passages] == [anchor.id]
        assert response.passages[0].passage_index == 7
        assert response.covers_parent is False
