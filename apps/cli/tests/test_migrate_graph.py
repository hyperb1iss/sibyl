"""The graph pass of `sibyl migrate to-team` plans and writes a project's authored graph.

The API only accepts links declared at creation, from the new entity to ones
that already exist, so the plan has to order every write. These cover what
is carried, what is left for the target to re-derive, and that a re-run
resumes without duplicating anything.
"""

from __future__ import annotations

from typing import Any

import pytest

from sibyl_cli.migrate_graph import (
    GraphPlan,
    SourceEdge,
    SourceEntity,
    build_plan,
    execute_plan,
    idempotency_key,
    validate_project_scope,
)

PROJECT = "project_0a1b2c3d4e5f"


def _entity(
    uuid: str,
    entity_type: str,
    *,
    name: str | None = None,
    scope: str | None = "project",
    created: str = "2026-01-01T00:00:00Z",
    status: str | None = None,
    **attributes: Any,
) -> SourceEntity:
    return SourceEntity(
        uuid=uuid,
        entity_type=entity_type,
        name=name or uuid,
        memory_scope=scope,
        attributes=attributes,
        created_at=created,
        status=status,
    )


def _layer_of(plan: GraphPlan) -> dict[str, int]:
    return {node.source.uuid: node.layer for node in plan.entities}


def _node(plan: GraphPlan, uuid: str) -> Any:
    return next(node for node in plan.entities if node.source.uuid == uuid)


def test_derived_rows_are_left_for_the_target_to_rebuild() -> None:
    plan = build_plan(
        [
            _entity("topic_1", "topic", category="memory_projection"),
            _entity("passage_1", "passage", category="passage_projection"),
            _entity("event_1", "event", category="memory_fact_projection"),
            _entity("claim_1", "claim", category="memory_fact_projection"),
            _entity("claim_2", "claim"),
            _entity(PROJECT, "project"),
            _entity("decision_1", "decision"),
        ],
        [],
        project=PROJECT,
    )

    assert [node.source.uuid for node in plan.entities] == ["claim_2", "decision_1"]
    assert sum(plan.skipped.values()) == 5


def test_private_rows_stay_private_unless_shared() -> None:
    rows = [
        _entity("decision_1", "decision", scope="private"),
        _entity("task_1", "task", scope=None),
    ]

    kept = build_plan(rows, [], project=PROJECT)
    shared = build_plan(rows, [], project=PROJECT, share_private=True)

    assert _node(kept, "decision_1").scope == "private"
    assert _node(shared, "decision_1").scope == "project"
    assert _node(kept, "task_1").scope is None


def test_repeated_titles_get_distinct_names_the_target_can_store() -> None:
    long_title = (
        "Retry the nightly export when the upstream bucket rotates its signing keys, " + "x" * 40
    )
    plan = build_plan(
        [
            _entity("t1", "task", name="Fix CI", created="2026-01-01"),
            _entity("t2", "task", name="Fix CI", created="2026-01-02"),
            _entity("t3", "task", name="fix ci", created="2026-01-03"),
            _entity("d1", "decision", name="Fix CI"),
            _entity("long1", "task", name=long_title, created="2026-01-04"),
            _entity("long2", "task", name=long_title, created="2026-01-05"),
        ],
        [],
        project=PROJECT,
    )

    assert _node(plan, "t1").name == "Fix CI"
    assert _node(plan, "t2").name == "Fix CI (2)"
    # A different case already hashes differently, so it keeps its title.
    assert _node(plan, "t3").name == "fix ci"
    assert _node(plan, "d1").name == "Fix CI"
    # The server hashes only the first 100 characters of a title, so a
    # counter appended past them would still collide.
    first, second = _node(plan, "long1").name, _node(plan, "long2").name
    assert first == long_title
    assert second[:100] != first[:100]
    assert second.endswith(" (2)") and len(second) <= 100


def test_typed_links_and_task_structure_order_their_targets_first() -> None:
    plan = build_plan(
        [
            _entity("epic_1", "epic"),
            _entity("task_parent", "task"),
            _entity("task_child", "task", created="2026-01-05"),
            _entity("task_dep", "task"),
            _entity("decision_old", "decision", created="2026-03-01"),
            _entity("decision_new", "decision", created="2026-02-01"),
        ],
        [
            SourceEdge("BELONGS_TO", "task_child", "epic_1"),
            SourceEdge("BELONGS_TO", "task_child", "task_parent"),
            SourceEdge("DEPENDS_ON", "task_child", "task_dep"),
            SourceEdge("BELONGS_TO", "task_child", PROJECT),
            SourceEdge("SUPERSEDES", "decision_new", "decision_old"),
        ],
        project=PROJECT,
    )
    layer = _layer_of(plan)
    child = _node(plan, "task_child")

    assert (child.epic, child.parent, child.depends_on) == ("epic_1", "task_parent", ["task_dep"])
    assert layer["task_child"] > max(layer["epic_1"], layer["task_parent"], layer["task_dep"])
    assert _node(plan, "decision_new").declares == [("supersedes", "decision_old")]
    assert layer["decision_new"] > layer["decision_old"]


def test_an_untyped_two_way_pair_is_declared_once_from_the_later_end() -> None:
    plan = build_plan(
        [_entity("task_1", "task"), _entity("procedure_1", "procedure")],
        [
            SourceEdge("USES_PROCEDURE", "task_1", "procedure_1"),
            SourceEdge("DERIVED_FROM", "procedure_1", "task_1"),
        ],
        project=PROJECT,
    )

    assert _node(plan, "task_1").declares == []
    assert _node(plan, "procedure_1").declares == [("", "task_1")]
    assert {c["type"] for c in _node(plan, "procedure_1").coerced} == {
        "USES_PROCEDURE",
        "DERIVED_FROM",
    }
    assert plan.edge_counts == {"USES_PROCEDURE": 1, "DERIVED_FROM": 1}


def test_a_hard_cycle_is_broken_and_reported() -> None:
    plan = build_plan(
        [
            _entity("task_a", "task", created="2026-01-01"),
            _entity("task_b", "task", created="2026-01-02"),
        ],
        [
            SourceEdge("DEPENDS_ON", "task_a", "task_b"),
            SourceEdge("DEPENDS_ON", "task_b", "task_a"),
        ],
        project=PROJECT,
    )

    assert len(plan.dropped_edges) == 1
    layer = _layer_of(plan)
    later = max(plan.entities, key=lambda n: layer[n.source.uuid])
    earlier = min(plan.entities, key=lambda n: layer[n.source.uuid])
    assert later.depends_on == [earlier.source.uuid]
    assert earlier.depends_on == []


def test_every_declared_target_lives_in_an_earlier_layer() -> None:
    entities = [_entity(f"e{n}", "decision", created=f"2026-01-{n + 1:02d}") for n in range(20)]
    edges = [SourceEdge("RELATED_TO", f"e{n}", f"e{(n * 7) % 20}") for n in range(20)]
    edges += [SourceEdge("SUPPORTS", f"e{n}", f"e{n + 1}") for n in range(0, 19, 3)]

    plan = build_plan(entities, edges, project=PROJECT)
    layer = _layer_of(plan)

    for node in plan.entities:
        for _, target in node.declares:
            assert layer[target] < node.layer


def test_project_scope_rejects_anything_but_a_project_id() -> None:
    assert validate_project_scope(PROJECT) == PROJECT
    with pytest.raises(ValueError):
        validate_project_scope("project_x' OR 1=1")


def test_legacy_task_statuses_map_to_workflow_statuses() -> None:
    from sibyl_cli.migrate_graph import _task_status

    plan = build_plan(
        [
            _entity("a", "task", status="completed"),
            _entity("b", "task", status="in_progress"),
            _entity("c", "task", status="todo"),
            _entity("d", "task", status="nonsense"),
        ],
        [],
        project=PROJECT,
    )

    assert [_task_status(_node(plan, uuid)) for uuid in "abcd"] == ["done", "doing", None, None]


class _Target:
    """Records every write; ids are derived so re-runs are checkable."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, str | None]] = []
        self.fail = fail or set()

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        *,
        _buffer_pending: bool = True,
        _idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        assert _buffer_pending is False
        self.calls.append((method, path, json, _idempotency_key))
        if method == "POST":
            origin = json["metadata"]["migration"]["origin_entity_id"]  # type: ignore[index]
            if origin in self.fail:
                raise RuntimeError("boom")
            return {"id": f"target-{origin}"}
        return {}


def _plan() -> GraphPlan:
    return build_plan(
        [
            _entity("epic_1", "epic", status="in_progress"),
            _entity("task_1", "task", status="done", learnings="what we learned"),
            _entity("task_2", "task", status="todo", created="2026-02-01"),
            _entity("decision_1", "decision", scope="private", content="the decision"),
        ],
        [
            SourceEdge("BELONGS_TO", "task_1", "epic_1"),
            SourceEdge("DEPENDS_ON", "task_2", "task_1"),
            SourceEdge("RELATED_TO", "decision_1", "task_1"),
        ],
        project=PROJECT,
    )


async def _execute(target: _Target, ids: dict[str, str], statuses: dict[str, str]) -> Any:
    saves: list[int] = []
    outcome = await execute_plan(
        target,
        _plan(),
        ids=ids,
        statuses=statuses,
        route_key="route",
        target_project_id="project_target",
        origin_org="org-src",
        save=lambda: saves.append(len(ids)),
    )
    return outcome, saves


@pytest.mark.asyncio
async def test_execute_writes_bodies_links_and_statuses() -> None:
    target = _Target()
    ids: dict[str, str] = {}
    statuses: dict[str, str] = {}

    outcome, saves = await _execute(target, ids, statuses)

    assert outcome.created == 4 and outcome.failures == []
    posts = {
        call[2]["metadata"]["migration"]["origin_entity_id"]: call
        for call in target.calls
        if call[0] == "POST"
    }  # type: ignore[index]
    task_1 = posts["task_1"][2]
    assert task_1["metadata"]["epic_id"] == "target-epic_1"
    assert task_1["metadata"]["learnings"] == "what we learned"
    assert task_1["metadata"]["project_id"] == "project_target"
    assert posts["task_2"][2]["metadata"]["depends_on"] == ["target-task_1"]
    decision = posts["decision_1"][2]
    assert decision["related_to"] == ["target-task_1"]
    assert decision["metadata"]["memory_scope"] == "private"
    assert decision["content"] == "the decision"
    assert posts["epic_1"][2]["metadata"]["status"] == "in_progress"
    patches = [call for call in target.calls if call[0] == "PATCH"]
    assert [(p[1], p[2]) for p in patches] == [("/tasks/target-task_1", {"status": "done"})]
    assert posts["task_1"][3] == idempotency_key("route", "task_1", "create")
    assert statuses == {"task_1": "done"}
    assert saves and saves[-1] == 4


@pytest.mark.asyncio
async def test_a_rerun_resumes_from_the_ledger_without_writing_again() -> None:
    target = _Target()
    ids: dict[str, str] = {}
    statuses: dict[str, str] = {}
    await _execute(target, ids, statuses)
    rerun = _Target()

    outcome, _ = await _execute(rerun, ids, statuses)

    assert outcome.created == 0 and outcome.resumed == 4
    assert rerun.calls == []


@pytest.mark.asyncio
async def test_a_failed_write_is_reported_and_its_dependents_still_land_unlinked() -> None:
    target = _Target(fail={"task_1"})
    ids: dict[str, str] = {}

    outcome, _ = await _execute(target, ids, {})

    assert any("task_1" in failure for failure in outcome.failures)
    assert "task_1" not in ids
    assert {"epic_1", "task_2", "decision_1"} <= set(ids)
    assert len(outcome.unlinked) == 2
    post = next(
        c
        for c in target.calls
        if c[0] == "POST" and c[2]["metadata"]["migration"]["origin_entity_id"] == "decision_1"
    )  # type: ignore[index]
    assert "related_to" not in post[2]
