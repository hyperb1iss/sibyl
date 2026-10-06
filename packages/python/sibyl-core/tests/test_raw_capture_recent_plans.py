"""Raw capture scope reads stream from recent-first indexes.

The scope index `(organization_id, memory_scope, scope_key)` matches every
private capture of an organization because private rows leave scope_key
empty, so recall, listing and the embedding coverage probe fetched the whole
scope and sorted it in memory. Migration 55 adds indexes that end in the
order the readers use, and the readers order by exactly that tail so the
3.x planner streams the scan and bounds it by the limit.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any

import pytest

from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    _content_schema_migrations,
)
from sibyl_core.backends.surreal.schema_invariants import _DEFINE_INDEX_RE
from sibyl_core.services import content_client, content_raw_recall
from sibyl_core.services.content_models import MemoryScope

ORG = "org-a"

_EXPECTED_INDEXES = {
    "idx_raw_captures_private_recent": (
        "organization_id",
        "memory_scope",
        "principal_id",
        "captured_at",
        "uuid",
    ),
    "idx_raw_captures_scoped_recent": (
        "organization_id",
        "memory_scope",
        "scope_key",
        "captured_at",
        "uuid",
    ),
    "idx_raw_captures_surface_review": (
        "organization_id",
        "capture_surface",
        "review_state",
        "captured_at",
        "uuid",
    ),
    "idx_raw_captures_org_created": ("organization_id", "created_at", "uuid"),
}


def _defined_indexes(statements: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    defined: dict[str, tuple[str, ...]] = {}
    for statement in statements:
        match = _DEFINE_INDEX_RE.match(statement)
        assert match is not None, statement
        assert match.group("table") == "raw_captures"
        fields = tuple(field.strip() for field in match.group("body").split(","))
        defined[match.group("name")] = fields
    return defined


def test_migration_55_defines_the_recent_first_indexes() -> None:
    migrations = {item.version: item for item in _content_schema_migrations(url="")}
    assert CONTENT_SCHEMA_CURRENT_VERSION >= 55
    assert migrations[55].name == "content_raw_capture_recent_indexes"
    assert _defined_indexes(migrations[55].statements) == _EXPECTED_INDEXES


class _ScriptedClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, dict(params)))
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


def _result(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"status": "OK", "result": records}]


_RECENT_FIRST_TAIL = re.compile(r"ORDER BY captured_at DESC, uuid DESC LIMIT \$limit;\s*$")


async def test_lexical_lane_orders_by_the_index_tail() -> None:
    client = _ScriptedClient([_result([])])

    await content_raw_recall._recall_raw_memory_lexical(
        client,  # type: ignore[arg-type]
        organization_id=ORG,
        principal_id="owner",
        query="planner",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        agent_id=None,
        project_id=None,
        limit=10,
    )

    query, params = client.calls[0]
    assert _RECENT_FIRST_TAIL.search(query), query
    assert query.startswith("SELECT id AS record_id, uuid,")
    assert "WHERE organization_id = $organization_id AND memory_scope = $memory_scope" in query
    assert params["limit"] == 40


async def test_list_raw_memories_for_scope_orders_by_the_index_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ScriptedClient([_result([])])

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)

    await content_raw_recall.list_raw_memories_for_scope(
        organization_id=ORG,
        principal_id="owner",
        memory_scope="team",
        scope_key="team:x",
        limit=25,
    )

    query, params = client.calls[0]
    assert _RECENT_FIRST_TAIL.search(query), query
    assert query.startswith("SELECT * FROM raw_captures WHERE organization_id = $organization_id")
    assert params["scope_key"] == "team:x"
    assert params["limit"] == 25 * content_client.LIFECYCLE_FILTER_OVERFETCH_FACTOR


async def test_coverage_walk_pages_newest_first_by_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def ineligible_row(index: int) -> dict[str, object]:
        return {
            "uuid": f"{index:08d}",
            "organization_id": ORG,
            "source_id": f"source-{index}",
            "principal_id": "owner",
            "raw_content": "archived",
            "revision": 1,
            "metadata": {"superseded_by_source_id": "another-source"},
        }

    async def pages(client, query, **params):
        calls.append((query, dict(params)))
        limit = int(params["coverage_limit"])
        offset = int(params["coverage_offset"])
        count = limit if offset == 0 else 3
        return [ineligible_row(offset + index) for index in range(count)]

    monkeypatch.setattr(content_client, "select_many_raw", pages)

    verdict = await content_raw_recall._eligible_rows_present(
        object(),  # type: ignore[arg-type]
        where_clause="organization_id = $organization_id AND principal_id = $principal_id",
        params={"organization_id": ORG, "principal_id": "owner"},
        as_of=None,
        extra_clause=" AND embedding = NONE",
        page_size=128,
    )

    assert verdict is False
    assert [params["coverage_offset"] for _, params in calls] == [0, 128]
    for query, params in calls:
        assert query.rstrip().endswith(
            "ORDER BY captured_at DESC, uuid DESC LIMIT $coverage_limit START $coverage_offset;"
        )
        assert "coverage_cursor" not in query
        assert "uuid >" not in query
        assert params["coverage_limit"] == 128
