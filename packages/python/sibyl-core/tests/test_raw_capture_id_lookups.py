"""raw_captures id lookups are served by the uuid index, with the org verified in Python.

On SurrealDB 3.x an `organization_id = $org` equality beside `uuid IN $ids` makes
the planner take the organization index and filter the id list in memory, which
walks every capture of the organization on each call. These tests pin the
statement shapes that stay on the uuid index and prove the organization guard
moved into Python rather than disappearing.
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.content_schema import (
    CONTENT_SCHEMA_CURRENT_VERSION,
    RAW_CAPTURE_UUID_INDEX_REBUILD,
    _content_schema_migrations,
)
from sibyl_core.backends.surreal.schema_helpers import is_duplicate_unique_index_error
from sibyl_core.services import content_client, eval_publication_guards
from sibyl_core.services.content_models import raw_memory_from_record
from sibyl_core.services.content_raw_persistence import (
    list_raw_memories_for_promotion,
    resolve_raw_memory_prefix,
)
from sibyl_core.services.memory_derivations import unavailable_raw_derivation_ids
from sibyl_core.services.memory_source_validation import SourceReadAuthority

ORG = "org-a"
FOREIGN_ORG = "org-b"

_ORG_EQUALITY_BESIDE_ID_LIST = re.compile(
    r"organization_id\s*=\s*\$\w+\s+AND\s+uuid\s+(IN|INSIDE)\s+\$\w+"
    r"|uuid\s+(IN|INSIDE)\s+\$\w+\s+AND\s+organization_id\s*=\s*\$\w+",
    re.IGNORECASE,
)


def _capture(uuid: str, organization_id: str = ORG, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "uuid": uuid,
        "organization_id": organization_id,
        "source_id": f"source-{uuid}",
        "principal_id": "owner",
        "memory_scope": "private",
        "review_state": "pending",
        "title": uuid,
        "raw_content": f"content of {uuid}",
        "metadata": {},
        "revision": 1,
    }
    record.update(overrides)
    return record


class _ScriptedClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, dict(params)))
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


def _result(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"status": "OK", "result": records}]


def _install(monkeypatch: pytest.MonkeyPatch, client: _ScriptedClient) -> None:
    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(content_client, "surreal_content_client", session)


def test_id_batches_stay_within_the_planner_union_cap() -> None:
    assert content_client.ID_LOOKUP_BATCH_SIZE == 32
    assert content_client.DEFAULT_BATCH_SIZE == 128
    batches = content_client.value_batches(
        [f"id-{index}" for index in range(33)], batch_size=content_client.ID_LOOKUP_BATCH_SIZE
    )
    assert [len(batch) for batch in batches] == [32, 1]


async def test_uuid_lookups_split_at_the_planner_union_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [f"id-{index:03d}" for index in range(33)]
    client = _ScriptedClient([_result([]), _result([])])
    _install(monkeypatch, client)

    await list_raw_memories_for_promotion(organization_id=ORG, raw_memory_ids=ids, limit=50)

    assert [len(params["raw_memory_ids"]) for _, params in client.calls] == [32, 1]


def test_content_migration_rebuilds_the_raw_capture_uuid_index() -> None:
    migrations = {item.version: item for item in _content_schema_migrations(url="")}
    assert CONTENT_SCHEMA_CURRENT_VERSION >= 54
    assert migrations[54].statements == (RAW_CAPTURE_UUID_INDEX_REBUILD,)
    assert re.fullmatch(
        r"REBUILD INDEX IF EXISTS idx_raw_captures_uuid ON TABLE raw_captures;",
        RAW_CAPTURE_UUID_INDEX_REBUILD,
    )


def test_unique_index_rebuild_tolerates_duplicate_rows_like_its_definition() -> None:
    duplicate = RuntimeError("Database index `idx_raw_captures_uuid` already contains 'dirty-row'")
    assert is_duplicate_unique_index_error(RAW_CAPTURE_UUID_INDEX_REBUILD, duplicate)
    assert not is_duplicate_unique_index_error(
        RAW_CAPTURE_UUID_INDEX_REBUILD, RuntimeError("connection reset")
    )
    assert not is_duplicate_unique_index_error(
        "DEFINE INDEX IF NOT EXISTS idx_raw_captures_org ON raw_captures FIELDS organization_id;",
        duplicate,
    )


async def test_promotion_id_lookup_uses_uuid_index_and_verifies_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ScriptedClient(
        [
            _result(
                [
                    _capture("own", memory_scope="organization"),
                    _capture("foreign", FOREIGN_ORG, memory_scope="organization"),
                ]
            )
        ]
    )
    _install(monkeypatch, client)

    memories = await list_raw_memories_for_promotion(
        organization_id=ORG, raw_memory_ids=["foreign", "own"], limit=10
    )

    assert [memory.id for memory in memories] == ["own"]
    query, params = client.calls[0]
    assert "uuid INSIDE $raw_memory_ids" in query
    assert not _ORG_EQUALITY_BESIDE_ID_LIST.search(query)
    assert "organization_id" not in query
    assert params["raw_memory_ids"] == ["foreign", "own"]


async def test_prefix_resolve_unions_the_ranges_before_the_org_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ScriptedClient([_result([_capture("raw_1")])])
    _install(monkeypatch, client)

    memories = await resolve_raw_memory_prefix(organization_id=ORG, prefix="raw_", limit=5)

    assert [memory.id for memory in memories] == ["raw_1"]
    query, params = client.calls[0]
    inner = re.search(r"SELECT \* FROM \((.*?)\) WHERE organization_id = \$organization_id", query)
    assert inner is not None, query
    assert "organization_id" not in inner.group(1)
    assert "uuid >= $prefix AND uuid < $prefix_upper" in inner.group(1)
    assert "source_id >= $prefix AND source_id < $prefix_upper" in inner.group(1)
    assert query.rstrip().endswith("ORDER BY captured_at DESC LIMIT $limit;")
    assert params["organization_id"] == ORG
    assert params["prefix_upper"] == "raw_￿"


async def test_raw_derivation_targets_use_uuid_index_and_verify_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own = raw_memory_from_record(_capture("own"))
    stolen = raw_memory_from_record(_capture("stolen"))
    client = _ScriptedClient(
        [
            _result(
                [
                    {
                        "targets": [_capture("own"), _capture("stolen", FOREIGN_ORG)],
                        "associations": [],
                    }
                ]
            )
        ]
    )
    _install(monkeypatch, client)

    unavailable = await unavailable_raw_derivation_ids(
        ORG, [own, stolen], SourceReadAuthority("owner")
    )

    assert unavailable == {"stolen"}
    query, params = client.calls[0]
    assert "targets: (SELECT * FROM raw_captures WHERE uuid IN $ids)" in query
    assert not _ORG_EQUALITY_BESIDE_ID_LIST.search(query)
    assert params["ids"] == ["own", "stolen"]


async def test_publication_snapshot_looks_captures_up_by_uuid_and_verifies_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {
        "publications": [],
        "captures": [_capture("own"), _capture("foreign", FOREIGN_ORG)],
        "attempts": [],
    }
    client = _ScriptedClient([_result([snapshot])])
    _install(monkeypatch, client)
    seen: list[dict[str, Any]] = []

    def record_snapshot(snapshot, references, rows):
        seen.append(dict(snapshot))
        return set()

    monkeypatch.setattr(eval_publication_guards, "_unavailable_snapshot", record_snapshot)

    async def no_graph_verdicts(*_args, **_kwargs):
        # The graph-side verdict needs a graph runtime; this test is about the
        # content statement shape only.
        return set()

    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.unavailable_graph_derivation_ids",
        no_graph_verdicts,
    )

    await eval_publication_guards.unavailable_publication_ids(ORG, {"row": {}})

    query, _ = client.calls[0]
    assert not _ORG_EQUALITY_BESIDE_ID_LIST.search(query)
    assert (
        "captures: array::flatten($capture_ids.map(|$capture_id|\n"
        "                                (SELECT * FROM raw_captures WHERE uuid = $capture_id LIMIT 1)))"
    ) in query
    assert [row["uuid"] for row in seen[0]["captures"]] == ["own"]


# A live SurrealDB 3.x namespace was found with idx_raw_captures_uuid reporting
# `ready` while every capture created before an earlier index build was absent
# from it, so point lookups by uuid missed those rows. The rewritten lookups
# depend on that index, and migration 54 rebuilds it. This proves, on a real
# server, that after the rebuild the oldest rows of an organization are found
# through the rewritten path. Run with SIBYL_LIVE_SURREAL_TESTS=1 against a
# scratch server named by SIBYL_SURREAL_URL; it creates and removes its own
# namespace.
@pytest.mark.skipif(
    os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1",
    reason="live SurrealDB server required (SIBYL_LIVE_SURREAL_TESTS=1)",
)
async def test_rebuilt_uuid_index_serves_the_oldest_rows_on_a_live_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from sibyl_core.backends.surreal import SurrealContentClient
    from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema

    url = os.environ.get("SIBYL_SURREAL_URL", "")
    assert url, "SIBYL_SURREAL_URL must name the scratch server"
    namespace = f"qp_uuid_rebuild_{uuid4().hex}"
    client = SurrealContentClient(
        url=url,
        username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
        password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
        namespace=namespace,
    )
    try:
        await bootstrap_content_schema(client)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        rows = [
            {
                **_capture(f"row-{index:03d}", memory_scope="organization"),
                "captured_at": base + timedelta(days=index),
                "created_at": base + timedelta(days=index),
            }
            for index in range(48)
        ]
        await client.execute_query("INSERT INTO raw_captures $rows RETURN NONE;", rows=rows)
        await client.execute_query(RAW_CAPTURE_UUID_INDEX_REBUILD)

        oldest = [str(row["uuid"]) for row in rows[:20]]
        found = await client.execute_query(
            "SELECT VALUE uuid FROM raw_captures WITH INDEX idx_raw_captures_uuid "
            "WHERE uuid IN $ids;",
            ids=oldest,
        )
        assert isinstance(found, list)
        assert sorted(str(value) for value in found) == sorted(oldest)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr(content_client, "surreal_content_client", session)
        memories = await list_raw_memories_for_promotion(
            organization_id=ORG, raw_memory_ids=oldest, limit=100
        )
        assert sorted(memory.id for memory in memories) == sorted(oldest)
    finally:
        await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
        await client.close()
