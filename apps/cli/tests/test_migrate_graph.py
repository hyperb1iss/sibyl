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
    validate_organization_id,
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
    content: str | None = None,
    summary: str | None = None,
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
        content=content,
        summary=summary,
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


def test_ids_must_have_a_project_or_uuid_shape() -> None:
    assert validate_project_scope(PROJECT) == PROJECT
    assert validate_project_scope("proj_02b86e331f124126") == "proj_02b86e331f124126"
    with pytest.raises(ValueError):
        validate_project_scope("project_x' OR 1=1")
    assert validate_organization_id("E7B94A25-DD4C-4FB8-B300-0C75E83998E2") == (
        "e7b94a25-dd4c-4fb8-b300-0c75e83998e2"
    )
    with pytest.raises(ValueError):
        validate_organization_id("org' OR 1=1")


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


def test_rows_out_of_recall_stay_behind_and_flagged_rows_stay_private() -> None:
    plan = build_plan(
        [
            _entity(
                "episode_contested",
                "episode",
                excluded_from_recall=True,
                lifecycle_state="contested",
            ),
            _entity("episode_retired", "episode", lifecycle_state="retired"),
            _entity(
                "plan_secret", "plan", contains_sensitive=True, sensitivity_flags=["credential"]
            ),
            _entity("plan_shared", "plan", scope="private"),
        ],
        [],
        project=PROJECT,
        share_private=True,
    )

    assert [node.source.uuid for node in plan.entities] == ["plan_secret", "plan_shared"]
    assert _node(plan, "plan_secret").scope == "private"
    assert _node(plan, "plan_shared").scope == "project"
    assert plan.kept_private == 1


def test_the_category_is_part_of_the_id_so_it_disambiguates_titles() -> None:
    plan = build_plan(
        [
            _entity("d1", "decision", name="Retry policy", category="ci"),
            _entity("d2", "decision", name="Retry policy", category="deploy"),
            _entity("d3", "decision", name="Retry policy", category="ci", created="2026-02-01"),
        ],
        [],
        project=PROJECT,
    )

    assert [_node(plan, uuid).name for uuid in ("d1", "d2", "d3")] == [
        "Retry policy",
        "Retry policy",
        "Retry policy (2)",
    ]


def test_a_task_waits_for_its_epic_even_when_the_epic_is_younger() -> None:
    plan = build_plan(
        [
            _entity("task_1", "task", created="2026-01-01"),
            _entity("epic_1", "epic", created="2026-06-01"),
            _entity("epic_2", "epic", created="2026-07-01"),
        ],
        [SourceEdge("BELONGS_TO", "task_1", "epic_2")],
        project=PROJECT,
    )

    layer = _layer_of(plan)
    assert layer["task_1"] > layer["epic_2"]
    order = [node.source.uuid for node in plan.entities]
    assert order.index("epic_2") < order.index("task_1")


def test_a_cycle_loses_one_of_its_own_edges_and_nothing_else() -> None:
    plan = build_plan(
        [
            _entity("a", "decision", created="2026-01-01"),
            _entity("b", "decision", created="2026-01-02"),
            _entity("c", "decision", created="2026-01-03"),
            _entity("waiter", "decision", created="2026-01-04"),
        ],
        [
            SourceEdge("SUPERSEDES", "a", "b"),
            SourceEdge("SUPERSEDES", "b", "c"),
            SourceEdge("SUPERSEDES", "c", "a"),
            SourceEdge("SUPPORTS", "waiter", "a"),
        ],
        project=PROJECT,
    )

    assert len(plan.dropped_edges) == 1
    assert plan.dropped_edges[0].startswith("a -> b")
    assert _node(plan, "waiter").declares == [("supports", "a")]
    layer = _layer_of(plan)
    for node in plan.entities:
        for _, target in node.declares:
            assert layer[target] < node.layer


def test_a_limited_plan_is_a_prefix_that_keeps_its_link_targets() -> None:
    plan = build_plan(
        [_entity(f"t{n}", "task", created=f"2026-01-{n + 1:02d}") for n in range(6)],
        [SourceEdge("DEPENDS_ON", "t1", "t5"), SourceEdge("DEPENDS_ON", "t2", "t1")],
        project=PROJECT,
    )

    limited = plan.limited(3)
    included = {node.source.uuid for node in limited.entities}
    assert len(included) == 3
    for node in limited.entities:
        assert set(node.depends_on) <= included


class _Target:
    """Records every write; target ids are derived from the origin id."""

    def __init__(self, fail: set[str] | None = None, no_id: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.fail = fail or set()
        self.no_id = no_id or set()

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        *,
        _buffer_pending: bool = True,
    ) -> dict[str, Any]:
        assert _buffer_pending is False
        self.calls.append((method, path, json))
        if method == "POST":
            origin = json["metadata"]["migration"]["origin_entity_id"]  # type: ignore[index]
            if origin in self.fail:
                raise RuntimeError("boom")
            if origin in self.no_id:
                return {}
            return {"id": f"target-{origin}"}
        return {}

    def posts(self) -> dict[str, dict[str, Any]]:
        return {
            call[2]["metadata"]["migration"]["origin_entity_id"]: call[2]  # type: ignore[index]
            for call in self.calls
            if call[0] == "POST"
        }


def _plan() -> GraphPlan:
    return build_plan(
        [
            _entity("epic_1", "epic", status="in_progress"),
            _entity("task_1", "task", status="done", learnings="what we learned"),
            _entity("task_2", "task", status="todo", created="2026-02-01"),
            _entity(
                "decision_1",
                "decision",
                scope="private",
                content="the full decision body",
                summary="the summary",
                retrieval_keys=["E_RETRY_LIMIT"],
            ),
        ],
        [
            SourceEdge("BELONGS_TO", "task_1", "epic_1"),
            SourceEdge("DEPENDS_ON", "task_2", "task_1"),
            SourceEdge("RELATED_TO", "decision_1", "task_1"),
        ],
        project=PROJECT,
    )


class _Ledger:
    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.statuses: dict[str, str] = {}
        self.partial: dict[str, list[str]] = {}
        self.saves = 0


async def _execute(target: _Target, ledger: _Ledger, plan: GraphPlan | None = None) -> Any:
    def save() -> None:
        ledger.saves += 1

    return await execute_plan(
        target,
        plan or _plan(),
        ids=ledger.ids,
        statuses=ledger.statuses,
        partial=ledger.partial,
        target_project_id="project_target",
        origin_org="org-src",
        save=save,
    )


@pytest.mark.asyncio
async def test_execute_writes_bodies_links_and_statuses() -> None:
    target = _Target()
    ledger = _Ledger()

    outcome = await _execute(target, ledger)

    assert outcome.created == 4 and outcome.failures == []
    posts = target.posts()
    task_1 = posts["task_1"]
    assert task_1["metadata"]["epic_id"] == "target-epic_1"
    assert task_1["metadata"]["learnings"] == "what we learned"
    assert task_1["metadata"]["project_id"] == "project_target"
    assert posts["task_2"]["metadata"]["depends_on"] == ["target-task_1"]
    decision = posts["decision_1"]
    assert decision["related_to"] == ["target-task_1"]
    assert decision["metadata"]["memory_scope"] == "private"
    assert decision["content"] == "the full decision body"
    assert decision["retrieval_keys"] == ["E_RETRY_LIMIT"]
    assert posts["epic_1"]["metadata"]["migration"]["origin_status"] == "in_progress"
    patches = [call for call in target.calls if call[0] == "PATCH"]
    assert [(p[1], p[2]) for p in patches] == [("/tasks/target-task_1", {"status": "done"})]
    assert ledger.statuses == {"task_1": "done"}
    assert ledger.partial == {}
    assert ledger.saves >= 1


@pytest.mark.asyncio
async def test_a_rerun_resumes_from_the_ledger_without_writing_again() -> None:
    ledger = _Ledger()
    await _execute(_Target(), ledger)
    rerun = _Target()

    outcome = await _execute(rerun, ledger)

    assert outcome.created == 0 and outcome.resumed == 4
    assert rerun.calls == []


@pytest.mark.asyncio
async def test_a_failed_write_is_reported_and_its_dependents_land_then_get_relinked() -> None:
    ledger = _Ledger()
    first = await _execute(_Target(fail={"task_1"}), ledger)

    assert any("task_1" in failure for failure in first.failures)
    assert "task_1" not in ledger.ids
    assert {"epic_1", "task_2", "decision_1"} <= set(ledger.ids)
    assert set(ledger.partial) == {"task_2", "decision_1"}

    rerun = _Target()
    second = await _execute(rerun, ledger)

    assert second.failures == []
    assert second.created == 1 and second.relinked == 2
    posts = rerun.posts()
    assert posts["decision_1"]["related_to"] == ["target-task_1"]
    assert posts["task_2"]["metadata"]["depends_on"] == ["target-task_1"]
    assert ledger.partial == {}


@pytest.mark.asyncio
async def test_a_write_that_returns_no_id_is_a_failure_not_a_landing() -> None:
    ledger = _Ledger()

    outcome = await _execute(_Target(no_id={"decision_1"}), ledger)

    assert any("decision_1" in failure and "no id" in failure for failure in outcome.failures)
    assert "decision_1" not in ledger.ids


def test_the_graph_ledger_round_trips_and_refuses_another_route(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_cli import migrate

    monkeypatch.setattr(migrate, "_LEDGER_DIR", tmp_path)
    route = {
        "source_org": "s",
        "target_context": "team",
        "target_org_id": "o",
        "target_project_id": "p",
    }
    path = migrate._graph_ledger_path(route)
    migrate._save_graph_ledger(path, route, {"a": "t-a"}, {"a": "done"}, {"b": ["a"]})

    assert migrate._load_graph_ledger(path, route) == ({"a": "t-a"}, {"a": "done"}, {"b": ["a"]})
    with pytest.raises(RuntimeError, match="different migration route"):
        migrate._load_graph_ledger(path, {**route, "target_org_id": "elsewhere"})


def test_the_source_org_is_found_by_the_namespace_holding_the_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_cli import migrate

    holder = "org_e7b94a25dd4c4fb8b3000c75e83998e2"

    def fake_sql(*, statement: str, namespace: str = "sibyl_content", **_kwargs: Any) -> list[Any]:
        if statement.startswith("INFO FOR ROOT"):
            return [
                {
                    "namespaces": {
                        holder: "",
                        "org_00000000000000000000000000000000": "",
                        "sibyl_auth": "",
                    }
                }
            ]
        return [[{"n": 12}] if namespace == holder else []]

    monkeypatch.setattr(migrate, "_source_sql", fake_sql)

    found = migrate._source_orgs_with_graph(
        surreal_url="ws://localhost:8000/rpc", username=None, password=None, project=PROJECT
    )

    assert found == ["e7b94a25-dd4c-4fb8-b300-0c75e83998e2"]


def test_a_source_row_keeps_its_full_content_over_the_summary() -> None:
    from sibyl_cli import migrate
    from sibyl_cli.migrate_graph import PlannedEntity, _payload

    row = {
        "uuid": "decision_1",
        "entity_type": "decision",
        "name": "Long decision",
        "summary": "x" * 500,
        "content": "the whole " + "body " * 2000,
        "description": "x" * 500,
        "memory_scope": "project",
        "attributes": {"retrieval_keys": ["E_LIMIT"], "category": "ci"},
    }
    entity = migrate._source_entity(row)
    body, _missing = _payload(
        PlannedEntity(source=entity, name=entity.name, scope="project"),
        ids={},
        target_project_id="project_target",
        origin_org="org",
    )

    assert body["content"] == row["content"]
    assert body["retrieval_keys"] == ["E_LIMIT"]
