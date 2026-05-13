"""Tests for sibyl-core temporal query tools.

Covers bi-temporal query functionality including:
- Point-in-time queries (history mode)
- Timeline views (timeline mode)
- Conflict detection (conflicts mode)
- Datetime parsing and edge result processing
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sibyl_core.tools.responses import TemporalEdge, TemporalResponse
from sibyl_core.tools.temporal import (
    _parse_datetime,
    _parse_edge_results,
    find_conflicts,
    get_entity_history,
    get_entity_timeline,
    temporal_query,
)


def _make_temporal_edge(
    edge_id: str,
    *,
    name: str,
    source_id: str,
    target_id: str,
    created_at: datetime,
    fact: str | None = None,
    expired_at: datetime | None = None,
    valid_at: datetime | None = None,
    invalid_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=edge_id,
        name=name,
        fact=fact,
        source_node_uuid=source_id,
        target_node_uuid=target_id,
        created_at=created_at,
        expired_at=expired_at,
        valid_at=valid_at,
        invalid_at=invalid_at,
    )


class _FakeTemporalClient:
    def __init__(
        self,
        *,
        node_edges: list[SimpleNamespace] | None = None,
        group_edges: list[SimpleNamespace] | None = None,
        node_names: dict[str, str] | None = None,
        node_error: Exception | None = None,
        group_error: Exception | None = None,
    ) -> None:
        self.node_edges = list(node_edges or [])
        self.group_edges = list(group_edges if group_edges is not None else node_edges or [])
        self.node_names = dict(node_names or {})
        self.node_error = node_error
        self.group_error = group_error
        self.queries: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.queries.append((query, params))
        entity_id = str(params.get("entity_id") or "")
        if entity_id:
            if self.node_error is not None:
                raise self.node_error
            edges = [
                edge
                for edge in self.node_edges
                if edge.source_node_uuid == entity_id or edge.target_node_uuid == entity_id
            ]
        else:
            if self.group_error is not None:
                raise self.group_error
            edges = list(self.group_edges)

        if "expired_at IS NOT NONE" in query:
            edges = [
                edge for edge in edges if edge.expired_at is not None or edge.invalid_at is not None
            ]

        limit = int(params.get("limit") or len(edges) or 1)
        return [_temporal_edge_row(edge, self.node_names) for edge in edges[:limit]]


def _make_temporal_client(
    *,
    node_edges: list[SimpleNamespace] | None = None,
    group_edges: list[SimpleNamespace] | None = None,
    node_names: dict[str, str] | None = None,
    node_error: Exception | None = None,
    group_error: Exception | None = None,
) -> _FakeTemporalClient:
    return _FakeTemporalClient(
        node_edges=node_edges,
        group_edges=group_edges,
        node_names=node_names,
        node_error=node_error,
        group_error=group_error,
    )


def _temporal_edge_row(edge: SimpleNamespace, names: dict[str, str]) -> dict[str, object]:
    source_id = str(edge.source_node_uuid)
    target_id = str(edge.target_node_uuid)
    return {
        "edge_id": edge.uuid,
        "name": edge.name,
        "fact": edge.fact,
        "source_id": source_id,
        "source_name": names.get(source_id, ""),
        "target_id": target_id,
        "target_name": names.get(target_id, ""),
        "created_at": edge.created_at,
        "expired_at": edge.expired_at,
        "valid_at": edge.valid_at,
        "invalid_at": edge.invalid_at,
    }


# =============================================================================
# Response Model Tests
# =============================================================================


class TestTemporalEdge:
    """Test TemporalEdge dataclass."""

    def test_temporal_edge_creation(self) -> None:
        """TemporalEdge can be created with required fields."""
        edge = TemporalEdge(
            id="edge_1",
            name="RELATED_TO",
            source_id="source_1",
            source_name="Source Entity",
            target_id="target_1",
            target_name="Target Entity",
        )
        assert edge.id == "edge_1"
        assert edge.name == "RELATED_TO"
        assert edge.source_id == "source_1"
        assert edge.target_id == "target_1"
        assert edge.is_current is True  # default

    def test_temporal_edge_with_timestamps(self) -> None:
        """TemporalEdge can store all bi-temporal timestamps."""
        now = datetime.now(UTC)
        past = now - timedelta(days=30)
        future = now + timedelta(days=30)

        edge = TemporalEdge(
            id="edge_1",
            name="DEPENDS_ON",
            source_id="task_1",
            source_name="Task A",
            target_id="task_2",
            target_name="Task B",
            created_at=past,
            expired_at=None,
            valid_at=past,
            invalid_at=future,
            fact="Task A depends on Task B",
            is_current=True,
        )
        assert edge.created_at == past
        assert edge.expired_at is None
        assert edge.valid_at == past
        assert edge.invalid_at == future
        assert edge.fact == "Task A depends on Task B"

    def test_temporal_edge_expired(self) -> None:
        """TemporalEdge can represent expired edges."""
        now = datetime.now(UTC)
        edge = TemporalEdge(
            id="edge_1",
            name="HAS_STATUS",
            source_id="task_1",
            source_name="Task",
            target_id="status_1",
            target_name="Todo",
            created_at=now - timedelta(days=10),
            expired_at=now - timedelta(days=5),
            is_current=False,
        )
        assert edge.expired_at is not None
        assert edge.is_current is False


class TestTemporalResponse:
    """Test TemporalResponse dataclass."""

    def test_temporal_response_history_mode(self) -> None:
        """TemporalResponse for history mode."""
        response = TemporalResponse(
            mode="history",
            entity_id="entity_123",
            edges=[],
            total=0,
            as_of=datetime.now(UTC),
        )
        assert response.mode == "history"
        assert response.entity_id == "entity_123"
        assert response.as_of is not None

    def test_temporal_response_timeline_mode(self) -> None:
        """TemporalResponse for timeline mode."""
        edges = [
            TemporalEdge(
                id="e1",
                name="REL",
                source_id="s1",
                source_name="S1",
                target_id="t1",
                target_name="T1",
            ),
            TemporalEdge(
                id="e2",
                name="REL",
                source_id="s1",
                source_name="S1",
                target_id="t2",
                target_name="T2",
            ),
        ]
        response = TemporalResponse(
            mode="timeline",
            entity_id="entity_123",
            edges=edges,
            total=2,
            message="Timeline shows 2 edges.",
        )
        assert response.mode == "timeline"
        assert len(response.edges) == 2
        assert response.message is not None

    def test_temporal_response_conflicts_mode(self) -> None:
        """TemporalResponse for conflicts mode."""
        response = TemporalResponse(
            mode="conflicts",
            entity_id=None,  # Can be None for global conflict search
            edges=[],
            total=0,
            message="No conflicts found.",
        )
        assert response.mode == "conflicts"
        assert response.entity_id is None


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestParseDatetime:
    """Test _parse_datetime helper function."""

    def test_parse_datetime_none(self) -> None:
        """Returns None for None input."""
        assert _parse_datetime(None) is None

    def test_parse_datetime_from_datetime(self) -> None:
        """Returns datetime as-is if already datetime."""
        now = datetime.now(UTC)
        result = _parse_datetime(now)
        assert result == now

    def test_parse_datetime_iso_string(self) -> None:
        """Parses ISO format datetime string."""
        iso_str = "2025-03-15T10:30:00"
        result = _parse_datetime(iso_str)
        assert result is not None
        assert result.year == 2025
        assert result.month == 3
        assert result.day == 15

    def test_parse_datetime_iso_with_z(self) -> None:
        """Parses ISO format with Z suffix."""
        iso_str = "2025-03-15T10:30:00Z"
        result = _parse_datetime(iso_str)
        assert result is not None
        assert result.tzinfo is not None

    def test_parse_datetime_iso_with_timezone(self) -> None:
        """Parses ISO format with timezone offset."""
        iso_str = "2025-03-15T10:30:00+00:00"
        result = _parse_datetime(iso_str)
        assert result is not None
        assert result.tzinfo is not None

    def test_parse_datetime_date_only(self) -> None:
        """Parses date-only string."""
        date_str = "2025-03-15"
        result = _parse_datetime(date_str)
        assert result is not None
        assert result.year == 2025
        assert result.month == 3
        assert result.day == 15

    def test_parse_datetime_invalid_string(self) -> None:
        """Returns None for invalid datetime string."""
        result = _parse_datetime("not-a-date")
        assert result is None

    def test_parse_datetime_adds_utc_if_naive(self) -> None:
        """Adds UTC timezone if datetime is naive."""
        iso_str = "2025-03-15T10:30:00"
        result = _parse_datetime(iso_str)
        assert result is not None
        assert result.tzinfo == UTC


class TestParseEdgeResults:
    """Test _parse_edge_results helper function."""

    def test_parse_empty_results(self) -> None:
        """Returns empty list for empty input."""
        result = _parse_edge_results([])
        assert result == []

    def test_parse_dict_results(self) -> None:
        """Parses dictionary-style query results."""
        rows = [
            {
                "edge_id": "edge_1",
                "name": "RELATED_TO",
                "fact": "A is related to B",
                "source_id": "source_1",
                "source_name": "Source",
                "target_id": "target_1",
                "target_name": "Target",
                "created_at": "2025-03-15T10:00:00Z",
                "expired_at": None,
                "valid_at": None,
                "invalid_at": None,
            }
        ]
        result = _parse_edge_results(rows)
        assert len(result) == 1
        assert result[0].id == "edge_1"
        assert result[0].name == "RELATED_TO"
        assert result[0].fact == "A is related to B"
        assert result[0].is_current is True

    def test_parse_tuple_results(self) -> None:
        """Parses tuple-style query results."""
        rows = [
            (
                "edge_1",  # edge_id
                "DEPENDS_ON",  # name
                "Task depends on other",  # fact
                "task_1",  # source_id
                "Task A",  # source_name
                "task_2",  # target_id
                "Task B",  # target_name
                "2025-03-15T10:00:00Z",  # created_at
                None,  # expired_at
                None,  # valid_at
                None,  # invalid_at
            )
        ]
        result = _parse_edge_results(rows)
        assert len(result) == 1
        assert result[0].id == "edge_1"
        assert result[0].name == "DEPENDS_ON"

    def test_parse_expired_edge(self) -> None:
        """Correctly identifies expired edges."""
        now = datetime.now(UTC)
        past = (now - timedelta(days=5)).isoformat()
        rows = [
            {
                "edge_id": "edge_1",
                "name": "OLD_REL",
                "fact": None,
                "source_id": "s1",
                "source_name": "S",
                "target_id": "t1",
                "target_name": "T",
                "created_at": (now - timedelta(days=30)).isoformat(),
                "expired_at": past,
                "valid_at": None,
                "invalid_at": None,
            }
        ]
        result = _parse_edge_results(rows)
        assert len(result) == 1
        assert result[0].is_current is False
        assert result[0].expired_at is not None

    def test_parse_invalidated_edge(self) -> None:
        """Correctly identifies invalidated edges."""
        now = datetime.now(UTC)
        past = (now - timedelta(days=5)).isoformat()
        rows = [
            {
                "edge_id": "edge_1",
                "name": "WAS_TRUE",
                "fact": "This was true but no longer is",
                "source_id": "s1",
                "source_name": "S",
                "target_id": "t1",
                "target_name": "T",
                "created_at": (now - timedelta(days=30)).isoformat(),
                "expired_at": None,
                "valid_at": (now - timedelta(days=30)).isoformat(),
                "invalid_at": past,
            }
        ]
        result = _parse_edge_results(rows)
        assert len(result) == 1
        assert result[0].is_current is False
        assert result[0].invalid_at is not None


# =============================================================================
# Temporal Query Function Tests
# =============================================================================


class TestTemporalQuery:
    """Test temporal_query main function."""

    @pytest.mark.asyncio
    async def test_temporal_query_requires_organization_id(self) -> None:
        """temporal_query raises error without organization_id."""
        with pytest.raises(ValueError, match="organization_id is required"):
            await temporal_query(mode="history", organization_id=None)

    @pytest.mark.asyncio
    async def test_temporal_query_invalid_date_format(self) -> None:
        """temporal_query returns error for invalid date format."""
        response = await temporal_query(
            mode="history",
            entity_id="entity_123",
            as_of="invalid-date",
            organization_id="org_123",
        )
        assert response.total == 0
        assert "Invalid as_of date format" in (response.message or "")

    @pytest.mark.asyncio
    async def test_temporal_query_unknown_mode(self) -> None:
        """temporal_query returns error for unknown mode."""
        response = await temporal_query(
            mode="unknown",  # type: ignore
            organization_id="org_123",
        )
        assert response.total == 0
        assert "Unknown mode" in (response.message or "")

    @pytest.mark.asyncio
    async def test_temporal_query_history_mode(self) -> None:
        """temporal_query routes to history mode correctly."""
        client = _make_temporal_client()

        with patch(
            "sibyl_core.tools.temporal.get_graph_runtime",
            return_value=SimpleNamespace(client=client),
        ):
            response = await temporal_query(
                mode="history",
                entity_id="entity_123",
                organization_id="org_123",
            )
            assert response.mode == "history"
            assert response.entity_id == "entity_123"

    @pytest.mark.asyncio
    async def test_temporal_query_timeline_mode(self) -> None:
        """temporal_query routes to timeline mode correctly."""
        client = _make_temporal_client()

        with patch(
            "sibyl_core.tools.temporal.get_graph_runtime",
            return_value=SimpleNamespace(client=client),
        ):
            response = await temporal_query(
                mode="timeline",
                entity_id="entity_123",
                organization_id="org_123",
            )
            assert response.mode == "timeline"

    @pytest.mark.asyncio
    async def test_temporal_query_conflicts_mode(self) -> None:
        """temporal_query routes to conflicts mode correctly."""
        client = _make_temporal_client()

        with patch(
            "sibyl_core.tools.temporal.get_graph_runtime",
            return_value=SimpleNamespace(client=client),
        ):
            response = await temporal_query(
                mode="conflicts",
                organization_id="org_123",
            )
            assert response.mode == "conflicts"


class TestGetEntityHistory:
    """Test get_entity_history function."""

    @pytest.mark.asyncio
    async def test_history_requires_entity_id(self) -> None:
        """get_entity_history returns error without entity_id."""
        client = _make_temporal_client()

        response = await get_entity_history(
            client=client,
            organization_id="org_123",
            entity_id=None,
        )
        assert response.total == 0
        assert "entity_id is required" in (response.message or "")

    @pytest.mark.asyncio
    async def test_history_returns_edges(self) -> None:
        """get_entity_history returns edges for entity."""
        now = datetime(2025, 3, 15, 10, 0, 0, tzinfo=UTC)
        client = _make_temporal_client(
            node_edges=[
                _make_temporal_edge(
                    "edge_1",
                    name="RELATED_TO",
                    fact="Entity is related",
                    source_id="entity_123",
                    target_id="other_entity",
                    created_at=now,
                )
            ],
            node_names={"entity_123": "Test Entity", "other_entity": "Other Entity"},
        )

        response = await get_entity_history(
            client=client,
            organization_id="org_123",
            entity_id="entity_123",
        )
        assert response.mode == "history"
        assert response.total == 1
        assert len(response.edges) == 1
        assert response.edges[0].name == "RELATED_TO"

    @pytest.mark.asyncio
    async def test_history_with_as_of(self) -> None:
        """get_entity_history respects as_of parameter."""
        as_of = datetime(2025, 3, 15, 10, 0, 0, tzinfo=UTC)
        client = _make_temporal_client(
            node_edges=[
                _make_temporal_edge(
                    "edge_early",
                    name="RELATED_TO",
                    source_id="entity_123",
                    target_id="other_entity",
                    created_at=as_of - timedelta(days=1),
                ),
                _make_temporal_edge(
                    "edge_late",
                    name="RELATED_TO",
                    source_id="entity_123",
                    target_id="other_entity",
                    created_at=as_of + timedelta(days=1),
                ),
            ],
            node_names={"entity_123": "Test Entity", "other_entity": "Other Entity"},
        )

        response = await get_entity_history(
            client=client,
            organization_id="org_123",
            entity_id="entity_123",
            as_of=as_of,
        )
        assert response.as_of == as_of
        assert response.total == 1
        assert response.edges[0].id == "edge_early"

    @pytest.mark.asyncio
    async def test_history_handles_query_error(self) -> None:
        """get_entity_history handles query errors gracefully."""
        client = _make_temporal_client(node_error=Exception("DB error"))

        response = await get_entity_history(
            client=client,
            organization_id="org_123",
            entity_id="entity_123",
        )
        assert response.total == 0
        assert "Query failed" in (response.message or "")

    @pytest.mark.asyncio
    async def test_history_queries_native_edges(self) -> None:
        """get_entity_history reads temporal edge rows directly from Surreal."""
        now = datetime.now(UTC)
        client = _make_temporal_client(
            node_edges=[
                _make_temporal_edge(
                    "edge_1",
                    name="RELATED_TO",
                    fact="Entity is related",
                    source_id="entity_123",
                    target_id="other_entity",
                    created_at=now - timedelta(days=1),
                    valid_at=now - timedelta(days=1),
                )
            ],
            node_names={"entity_123": "Test Entity", "other_entity": "Other Entity"},
        )

        response = await get_entity_history(
            client=client,
            organization_id="org_123",
            entity_id="entity_123",
        )

        assert response.total == 1
        assert response.edges[0].source_name == "Test Entity"
        assert response.edges[0].target_name == "Other Entity"
        query, params = client.queries[-1]
        assert "FROM relates_to" in query
        assert "(in.uuid = $entity_id OR out.uuid = $entity_id)" in query
        assert params["entity_id"] == "entity_123"
        assert params["limit"] == 200


class TestGetEntityTimeline:
    """Test get_entity_timeline function."""

    @pytest.mark.asyncio
    async def test_timeline_requires_entity_id(self) -> None:
        """get_entity_timeline returns error without entity_id."""
        client = _make_temporal_client()

        response = await get_entity_timeline(
            client=client,
            organization_id="org_123",
            entity_id=None,
        )
        assert response.total == 0
        assert "entity_id is required" in (response.message or "")

    @pytest.mark.asyncio
    async def test_timeline_returns_all_edges(self) -> None:
        """get_entity_timeline returns all edge versions."""
        now = datetime.now(UTC)
        client = _make_temporal_client(
            node_edges=[
                _make_temporal_edge(
                    "edge_1",
                    name="HAS_STATUS",
                    fact="Status was todo",
                    source_id="task_1",
                    target_id="status_todo",
                    created_at=now - timedelta(days=10),
                    expired_at=now - timedelta(days=5),
                ),
                _make_temporal_edge(
                    "edge_2",
                    name="HAS_STATUS",
                    fact="Status is doing",
                    source_id="task_1",
                    target_id="status_doing",
                    created_at=now - timedelta(days=5),
                ),
            ],
            node_names={"task_1": "Task", "status_todo": "Todo", "status_doing": "Doing"},
        )

        response = await get_entity_timeline(
            client=client,
            organization_id="org_123",
            entity_id="task_1",
        )
        assert response.mode == "timeline"
        assert response.total == 2
        assert response.edges[0].is_current is False
        assert response.edges[1].is_current is True

    @pytest.mark.asyncio
    async def test_timeline_includes_message(self) -> None:
        """get_entity_timeline includes helpful message."""
        client = _make_temporal_client()

        response = await get_entity_timeline(
            client=client,
            organization_id="org_123",
            entity_id="entity_123",
        )
        assert "Timeline shows" in (response.message or "")

    @pytest.mark.asyncio
    async def test_timeline_queries_native_edges(self) -> None:
        """get_entity_timeline builds results from direct Surreal rows."""
        now = datetime.now(UTC)
        client = _make_temporal_client(
            node_edges=[
                _make_temporal_edge(
                    "edge_old",
                    name="HAS_STATUS",
                    fact="Status was todo",
                    source_id="task_1",
                    target_id="status_todo",
                    created_at=now - timedelta(days=10),
                    expired_at=now - timedelta(days=5),
                ),
                _make_temporal_edge(
                    "edge_new",
                    name="HAS_STATUS",
                    fact="Status is doing",
                    source_id="task_1",
                    target_id="status_doing",
                    created_at=now - timedelta(days=5),
                ),
            ],
            node_names={"task_1": "Task", "status_todo": "Todo", "status_doing": "Doing"},
        )

        response = await get_entity_timeline(
            client=client,
            organization_id="org_123",
            entity_id="task_1",
        )

        assert response.total == 2
        assert response.edges[0].id == "edge_old"
        assert response.edges[1].id == "edge_new"
        query, params = client.queries[-1]
        assert "FROM relates_to" in query
        assert params["entity_id"] == "task_1"
        assert params["limit"] == 100


class TestFindConflicts:
    """Test find_conflicts function."""

    @pytest.mark.asyncio
    async def test_conflicts_finds_expired_edges(self) -> None:
        """find_conflicts returns edges with temporal invalidation."""
        now = datetime.now(UTC)
        client = _make_temporal_client(
            group_edges=[
                _make_temporal_edge(
                    "edge_1",
                    name="OLD_FACT",
                    fact="This was believed true",
                    source_id="s1",
                    target_id="t1",
                    created_at=now - timedelta(days=30),
                    expired_at=now - timedelta(days=10),
                )
            ],
            node_names={"s1": "Source", "t1": "Target"},
        )

        response = await find_conflicts(
            client=client,
            organization_id="org_123",
        )
        assert response.mode == "conflicts"
        assert response.total == 1
        assert "invalidated edges" in (response.message or "")

    @pytest.mark.asyncio
    async def test_conflicts_with_entity_filter(self) -> None:
        """find_conflicts can filter by entity_id."""
        client = _make_temporal_client()

        response = await find_conflicts(
            client=client,
            organization_id="org_123",
            entity_id="specific_entity",
        )
        assert response.entity_id == "specific_entity"
        query, params = client.queries[-1]
        assert "(in.uuid = $entity_id OR out.uuid = $entity_id)" in query
        assert "(expired_at IS NOT NONE OR invalid_at IS NOT NONE)" in query
        assert params["entity_id"] == "specific_entity"
        assert params["limit"] == 200

    @pytest.mark.asyncio
    async def test_conflicts_handles_query_error(self) -> None:
        """find_conflicts handles query errors gracefully."""
        client = _make_temporal_client(group_error=Exception("DB error"))

        response = await find_conflicts(
            client=client,
            organization_id="org_123",
        )
        assert response.total == 0
        assert "Query failed" in (response.message or "")

    @pytest.mark.asyncio
    async def test_conflicts_global_search(self) -> None:
        """find_conflicts can search across all entities."""
        client = _make_temporal_client()

        response = await find_conflicts(
            client=client,
            organization_id="org_123",
            entity_id=None,
        )
        assert response.entity_id is None
        query, _ = client.queries[-1]
        assert "FROM relates_to" in query
        assert "entity_id" not in query

    @pytest.mark.asyncio
    async def test_conflicts_queries_native_edges(self) -> None:
        """find_conflicts returns source and target names from Surreal rows."""
        now = datetime.now(UTC)
        client = _make_temporal_client(
            group_edges=[
                _make_temporal_edge(
                    "edge_1",
                    name="OLD_FACT",
                    fact="This was believed true",
                    source_id="s1",
                    target_id="t1",
                    created_at=now - timedelta(days=30),
                    expired_at=now - timedelta(days=10),
                )
            ],
            node_names={"s1": "Source", "t1": "Target"},
        )

        response = await find_conflicts(
            client=client,
            organization_id="org_123",
        )

        assert response.total == 1
        assert response.edges[0].source_name == "Source"
        assert response.edges[0].target_name == "Target"


# =============================================================================
# Integration Tests
# =============================================================================


class TestTemporalIntegration:
    """Integration tests for temporal query components."""

    def test_response_models_are_serializable(self) -> None:
        """Response models can be serialized to dict."""
        now = datetime.now(UTC)
        edge = TemporalEdge(
            id="edge_1",
            name="REL",
            source_id="s1",
            source_name="S",
            target_id="t1",
            target_name="T",
            created_at=now,
        )
        assert hasattr(edge, "__dict__")

        response = TemporalResponse(
            mode="history",
            entity_id="e1",
            edges=[edge],
            total=1,
        )
        assert hasattr(response, "__dict__")

    @pytest.mark.asyncio
    async def test_temporal_query_end_to_end_mock(self) -> None:
        """End-to-end test with mocked graph client."""
        now = datetime.now(UTC)
        client = _make_temporal_client(
            node_edges=[
                _make_temporal_edge(
                    "edge_1",
                    name="BELONGS_TO",
                    fact="Task belongs to project",
                    source_id="task_1",
                    target_id="project_1",
                    created_at=now,
                )
            ],
            node_names={"task_1": "Implement feature", "project_1": "Sibyl"},
        )

        with patch(
            "sibyl_core.tools.temporal.get_graph_runtime",
            return_value=SimpleNamespace(client=client),
        ):
            history = await temporal_query(
                mode="history",
                entity_id="task_1",
                organization_id="org_123",
            )
            assert history.mode == "history"
            assert history.total == 1
            assert history.edges[0].name == "BELONGS_TO"
            assert history.edges[0].is_current is True
