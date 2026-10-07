"""The exact vector pass is skipped when the filtered walk returned every row.

A typed HNSW walk asked for more candidates than the type holds always
falls short of its pool, and the completion pass then cosine-scored every
stamped vector of the namespace (8,256 rows, over half a second on the
dev namespace) to re-rank rows the walk already had. One IndexCountScan
per requested type tells the two cases apart.
"""

from __future__ import annotations

import pytest

from sibyl_core.models.entities import EntityType
from sibyl_core.services.graph_entities import EntityManager
from tests.test_surreal_knn import _entity_row, _overfetch_provider, _ScriptedClient


@pytest.fixture(autouse=True)
def _lane_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from sibyl_core.services.embedding_lane_readiness import LaneReadiness

    async def ready(**_kwargs: object) -> LaneReadiness:
        return LaneReadiness(run=True, reason="test", admit_unstamped=False)

    monkeypatch.setattr("sibyl_core.services.embedding_lane_readiness.vector_lane_readiness", ready)


def _labels(client: _ScriptedClient) -> list[str]:
    return [str(params.get("_query_label")) for _, params in client.calls]


async def test_exhausted_type_skips_the_exact_pass() -> None:
    walked = [_entity_row(f"rule_{i:02d}", entity_type="rule") for i in range(12)]
    client = _ScriptedClient(
        "org-overfetch",
        {
            "entity.search.vector": walked,
            "entity.search.vector.type_total": [{"total": 12}],
            "entity.search.vector.exact": [_entity_row("never_used", entity_type="rule")],
        },
    )
    manager = EntityManager(
        client, group_id=client.group_id, embedding_provider=_overfetch_provider("exhausted")
    )

    found = await manager._vector_search(query="rules", entity_types=[EntityType.RULE], limit=10)

    assert [entity.id for entity, _ in found] == [row["uuid"] for row in walked]
    assert _labels(client) == ["entity.search.vector", "entity.search.vector.type_total"]
    count_query, count_params = client.calls[1]
    assert (
        count_query == "SELECT count() AS total FROM entity WHERE entity_type = 'rule' GROUP ALL;"
    )
    assert "entity_type" not in count_params


async def test_shortfall_with_rows_left_still_runs_the_exact_pass() -> None:
    walked = [_entity_row(f"rule_{i:02d}", entity_type="rule") for i in range(12)]
    complete = [*walked, _entity_row("rule_missed", entity_type="rule")]
    client = _ScriptedClient(
        "org-overfetch",
        {
            "entity.search.vector": walked,
            "entity.search.vector.type_total": [{"total": 13}],
            "entity.search.vector.exact": complete,
        },
    )
    manager = EntityManager(
        client, group_id=client.group_id, embedding_provider=_overfetch_provider("shortfall")
    )

    found = await manager._vector_search(query="rules", entity_types=[EntityType.RULE], limit=10)

    assert [entity.id for entity, _ in found] == [row["uuid"] for row in complete]
    assert _labels(client) == [
        "entity.search.vector",
        "entity.search.vector.type_total",
        "entity.search.vector.exact",
    ]


async def test_unanswered_count_keeps_the_exact_pass() -> None:
    walked = [_entity_row(f"topic_{i:02d}") for i in range(3)]
    client = _ScriptedClient(
        "org-overfetch",
        {"entity.search.vector": walked, "entity.search.vector.exact": walked},
    )
    manager = EntityManager(
        client, group_id=client.group_id, embedding_provider=_overfetch_provider("unanswered")
    )

    await manager._vector_search(query="topics", entity_types=None, limit=10)

    assert _labels(client) == [
        "entity.search.vector",
        "entity.search.vector.type_total",
        "entity.search.vector.exact",
    ]
    assert client.calls[1][0] == "SELECT count() AS total FROM entity GROUP ALL;"
