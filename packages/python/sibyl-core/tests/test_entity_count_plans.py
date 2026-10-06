"""Entity counts and existence probes never scan the organization's table.

Inside an organization namespace every row shares group_id, so a
`count() ... WHERE group_id = $g` (or any GROUP BY) made the 3.x planner
scan and decode every entity. Per-type single-column equalities plan as
IndexCountScan, the archived correction comes from the status index, and
the auto-link existence check stops at the first row of a type.
"""

from __future__ import annotations

import re
from typing import Any

from sibyl_core.models.entities import EntityType
from sibyl_core.services.graph_entities import EntityManager


class _BatchClient:
    _url = "memory://"
    pool_size = 1

    def __init__(self, totals: dict[str, int], archived: dict[str, int]) -> None:
        self.totals = totals
        self.archived = archived
        self.batches: list[tuple[str, dict[str, Any]]] = []

    async def execute_query_batch(self, query: str, **params: object) -> list[object]:
        self.batches.append((query, dict(params)))
        results: list[object] = []
        for statement in query.split("\n"):
            if "GROUP BY entity_type" in statement:
                results.append(
                    [
                        {"entity_type": entity_type, "entity_count": count}
                        for entity_type, count in self.archived.items()
                    ]
                )
                continue
            param = re.search(r"entity_type = \$(type_\d+)", statement)
            assert param is not None, statement
            total = self.totals.get(str(params[param.group(1)]), 0)
            results.append([{"entity_count": total}] if total else [])
        return results


async def test_count_by_type_uses_one_index_count_per_type_and_no_group_id() -> None:
    client = _BatchClient({"task": 9, "pattern": 4}, {"task": 2})
    manager = EntityManager(client, group_id="org")  # type: ignore[arg-type]

    active = await manager.count_by_type()
    everything = await manager.count_by_type(include_archived=True)

    assert (active["task"], active["pattern"], active["rule"]) == (7, 4, 0)
    assert (everything["task"], everything["pattern"]) == (9, 4)
    assert set(active) == {entity_type.value for entity_type in EntityType}
    active_query, params = client.batches[0]
    assert "group_id" not in active_query
    assert "GROUP BY" not in active_query.rsplit("\n", 1)[0]
    counted = re.findall(
        r"SELECT count\(\) AS entity_count FROM entity WHERE entity_type = \$type_\d+ GROUP ALL;",
        active_query,
    )
    assert len(counted) == len(EntityType)
    assert active_query.endswith(
        "SELECT entity_type, count() AS entity_count FROM entity "
        "WHERE status = 'archived' GROUP BY entity_type;"
    )
    assert sorted(params.values()) == sorted(entity_type.value for entity_type in EntityType)
    everything_query, _ = client.batches[1]
    assert "archived" not in everything_query


class _ProbeClient:
    _url = "memory://"

    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: object) -> list[object]:
        self.calls.append((query, dict(params)))
        return [{"uuid": "hit"}] if params["entity_type"] in self.present else []


async def test_existence_probe_stops_at_the_first_type_with_a_row() -> None:
    client = _ProbeClient({"rule"})
    manager = EntityManager(client, group_id="org")  # type: ignore[arg-type]

    found = await manager.has_entities_of_types(
        [EntityType.PATTERN, EntityType.RULE, EntityType.TOPIC]
    )
    assert found is True
    assert [params["entity_type"] for _, params in client.calls] == ["pattern", "rule"]
    for query, _ in client.calls:
        assert query == (
            "SELECT uuid FROM entity WHERE entity_type = $entity_type "
            "AND (status IS NONE OR status = '' OR status != 'archived') LIMIT 1;"
        )

    client.calls.clear()
    assert await manager.has_entities_of_types([EntityType.GUIDE], include_archived=True) is False
    assert client.calls[0][0] == "SELECT uuid FROM entity WHERE entity_type = $entity_type LIMIT 1;"
