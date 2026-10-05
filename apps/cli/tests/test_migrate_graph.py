"""The graph pass of `sibyl migrate to-team` plans and writes a project's authored graph.

Creation declares links to existing targets, and additive recovery fills
missing links without rewriting an imported body. These tests cover ordering,
selection and saved intents across partial failures and lost responses.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

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


async def test_repeated_limited_runs_advance_and_finish_task_statuses() -> None:
    plan = build_plan(
        [_entity(f"t{n}", "task", status="done") for n in range(6)],
        [SourceEdge("DEPENDS_ON", "t1", "t5"), SourceEdge("DEPENDS_ON", "t2", "t1")],
        project=PROJECT,
    )
    target = _Target()
    ledger = _Ledger()
    for expected in (2, 4, 6):
        batch = plan.limited(2, done=set(ledger.ids))
        outcome = await _execute(target, ledger, batch)
        assert outcome.created == 2
        assert len(ledger.ids) == expected
        assert len(ledger.statuses) == expected
        for node in batch.entities:
            assert set(node.depends_on) <= set(ledger.ids)
    outcome = await _execute(target, ledger, plan.limited(2, done=set(ledger.ids)))
    assert outcome.created == 0
    assert outcome.resumed == 6


async def test_graph_command_loads_progress_before_applying_limit(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_cli import migrate

    rows = [_entity(f"t{n}", "task", status="done") for n in range(3)]
    monkeypatch.setattr(migrate, "_read_source_graph", lambda **_: (rows, []))
    monkeypatch.setattr(migrate, "_LEDGER_DIR", tmp_path)
    target = _Target()
    route = {
        "source_org": "source",
        "target_context": "team",
        "target_org_id": "target-org",
        "target_project_id": "target",
    }
    for _ in range(3):
        failures = await migrate._migrate_graph(
            target,
            route=route,
            organization_id="source",
            project=PROJECT,
            target_project_id="target",
            dry_run=False,
            share_private=False,
            limit=1,
            surreal_url="memory://",
            username=None,
            password=None,
        )
        assert failures == []
    ids, statuses, _ = migrate._load_graph_ledger(migrate._graph_ledger_path(route), route)
    assert set(ids) == {"t0", "t1", "t2"}
    assert statuses == dict.fromkeys(ids, "done")


def test_a_zero_limit_keeps_completed_rows_for_status_and_link_recovery() -> None:
    plan = build_plan([_entity("a", "task"), _entity("b", "task")], [], project=PROJECT)
    assert [node.source.uuid for node in plan.limited(0, done={"b"}).entities] == ["b"]
    assert plan.limited(0).entities == []


class _ApiError(Exception):
    def __init__(self, status_code: int, error_code: str | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.error_code = error_code


class _Target:
    """An in-memory team server: target ids derive from the origin id, rows are kept."""

    def __init__(
        self,
        fail: set[str] | None = None,
        no_id: set[str] | None = None,
        taken_containers: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.fail = fail or set()
        self.no_id = no_id or set()
        self.taken = taken_containers or {}
        self.rows: dict[str, dict[str, Any]] = {}

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
        self.calls.append((method, path, json))
        if method == "POST" and path == "/search/explore":
            return {"entities": [{"id": tid, "name": name} for name, tid in self.taken.items()]}
        if method == "POST" and path.endswith("/links"):
            body = json or {}
            target_id = path.split("/")[-2]
            row = self.rows[target_id]
            metadata = row["metadata"]
            for field in ("epic_id", "parent_task_id"):
                if body.get(field):
                    metadata[field] = body[field]
            if body.get("depends_on"):
                metadata["depends_on"] = body["depends_on"]
            row["revision"] += 1
            return {
                "entity_id": target_id,
                "revision": row["revision"],
                "added_relationship_ids": [],
                "existing_relationship_ids": [],
                "epic_id": metadata.get("epic_id"),
                "parent_task_id": metadata.get("parent_task_id"),
                "depends_on": metadata.get("depends_on") or [],
                "replayed": False,
            }
        if method == "POST":
            body = json or {}
            origin = body["metadata"]["migration"]["origin_entity_id"]
            if origin in self.fail:
                raise RuntimeError("boom")
            if origin in self.no_id:
                return {}
            if body["name"] in self.taken:
                raise _ApiError(409)
            target_id = f"target-{origin}"
            self.rows[target_id] = {
                "id": target_id,
                "revision": 1,
                "name": body["name"],
                "content": body["content"],
                "description": body.get("description"),
                "tags": body.get("tags") or [],
                "metadata": {**body["metadata"], "status": "todo"},
            }
            return {"id": target_id, "revision": 1}
        if method == "GET":
            target_id = path.rsplit("/", 1)[-1]
            if target_id not in self.rows:
                raise _ApiError(404)
            return self.rows[target_id]
        if method == "PATCH":
            assert params == {"sync": "true", "replay_interrupted": "false"}
            target_id = path.rsplit("/", 1)[-1]
            row = self.rows[target_id]
            if (json or {}).get("expected_revision") != row["revision"]:
                raise _ApiError(409)
            row["metadata"]["status"] = (json or {})["status"]
            row["revision"] += 1
            return {"mutation_receipt": {"applied": True, "revision": row["revision"]}}
        return {}

    def posts(self) -> dict[str, dict[str, Any]]:
        return {
            call[2]["metadata"]["migration"]["origin_entity_id"]: call[2]  # type: ignore[index]
            for call in self.calls
            if call[0] == "POST" and call[1] == "/entities"
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
        self.partial: dict[str, dict[str, Any]] = {}
        self.structure: dict[str, dict[str, Any]] = {}
        self.preexisting: set[str] = set()
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
    assert [(p[1], p[2]) for p in patches] == [
        ("/tasks/target-task_1", {"status": "done", "expected_revision": 1})
    ]
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
    target = _Target(fail={"task_1"})
    first = await _execute(target, ledger)

    assert any("task_1" in failure for failure in first.failures)
    assert "task_1" not in ledger.ids
    assert {"epic_1", "task_2", "decision_1"} <= set(ledger.ids)
    assert set(ledger.partial) == {"task_1", "task_2", "decision_1"}

    target.fail = set()
    second = await _execute(target, ledger)

    assert second.failures == []
    assert second.created == 1 and second.relinked == 2
    rows = target.rows
    assert rows["target-decision_1"]["metadata"]["migration"]["origin_entity_id"] == "decision_1"
    linked = [c for c in target.calls if c[0] == "POST" and c[1].endswith("/links")]
    assert any(c[2]["related_to"] == ["target-task_1"] for c in linked)  # type: ignore[index]
    assert all("content" not in c[2] and "metadata" not in c[2] for c in linked)  # type: ignore[operator]
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
    pending = {"b": {"missing": ["a"], "digest": "abc"}}
    migrate._save_graph_ledger(path, route, {"a": "t-a"}, {"a": "done"}, pending)

    assert migrate._load_graph_ledger(path, route) == ({"a": "t-a"}, {"a": "done"}, pending)
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


def test_a_task_keeps_a_description_longer_than_its_content() -> None:
    from sibyl_cli.migrate_graph import PlannedEntity, _payload

    entity = SourceEntity(
        uuid="task_1",
        entity_type="task",
        name="Short body, longer description",
        memory_scope=None,
        attributes={},
        content="the short body",
        description="the longer description that the task view shows",
    )
    body, _ = _payload(
        PlannedEntity(source=entity, name=entity.name, scope=None),
        ids={},
        target_project_id="project_target",
        origin_org="org",
    )

    assert "the short body" in body["content"]
    assert "the longer description that the task view shows" in body["content"]
    assert "description" not in body


def _epic_and_done_task() -> GraphPlan:
    return build_plan(
        [
            _entity("epic_1", "epic"),
            _entity("task_1", "task", status="done"),
        ],
        [SourceEdge("BELONGS_TO", "task_1", "epic_1")],
        project=PROJECT,
    )


@pytest.mark.asyncio
async def test_relinking_a_done_task_puts_its_status_back() -> None:
    ledger = _Ledger()
    target = _Target(fail={"epic_1"})
    await _execute(target, ledger, _epic_and_done_task())
    assert target.rows["target-task_1"]["metadata"]["status"] == "done"

    target.fail = set()
    outcome = await _execute(target, ledger, _epic_and_done_task())

    assert outcome.relinked == 1
    assert target.rows["target-task_1"]["metadata"]["epic_id"] == "target-epic_1"
    assert target.rows["target-task_1"]["metadata"]["status"] == "done"


@pytest.mark.asyncio
async def test_a_row_edited_on_the_team_server_is_not_written_again() -> None:
    ledger = _Ledger()
    target = _Target(fail={"epic_1"})
    await _execute(target, ledger, _epic_and_done_task())
    target.rows["target-task_1"]["content"] = "edited on the team server"
    target.rows["target-task_1"]["metadata"]["status"] = "doing"

    target.fail = set()
    outcome = await _execute(target, ledger, _epic_and_done_task())

    assert outcome.relinked == 0
    assert target.rows["target-task_1"]["content"] == "edited on the team server"
    assert target.rows["target-task_1"]["metadata"]["status"] == "doing"
    assert any("changed on the team server" in line for line in outcome.unlinked)
    assert "task_1" not in ledger.partial


@pytest.mark.asyncio
async def test_a_teammates_epic_of_the_same_name_is_adopted() -> None:
    ledger = _Ledger()
    target = _Target(taken_containers={"epic_1": "team-epic"})

    outcome = await _execute(target, ledger, _epic_and_done_task())

    assert outcome.adopted == 1 and outcome.failures == []
    assert ledger.ids["epic_1"] == "team-epic"
    assert target.posts()["task_1"]["metadata"]["epic_id"] == "team-epic"


def test_titles_that_agree_for_100_characters_get_distinct_ids() -> None:
    prefix = (
        "Retry the nightly export when the upstream bucket rotates its signing keys and " + "z" * 30
    )
    plan = build_plan(
        [
            _entity("a", "decision", name=prefix + " first ending", created="2026-01-01"),
            _entity("b", "decision", name=prefix + " second ending", created="2026-01-02"),
        ],
        [],
        project=PROJECT,
    )

    first, second = _node(plan, "a").name, _node(plan, "b").name
    assert first == prefix + " first ending"
    assert second[:100] != first[:100]


@pytest.mark.asyncio
async def test_the_ledger_is_saved_during_a_large_layer() -> None:
    plan = build_plan(
        [
            _entity(f"d{n}", "decision", created=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z")
            for n in range(250)
        ],
        [],
        project=PROJECT,
    )
    ledger = _Ledger()

    await _execute(_Target(), ledger, plan)

    assert len(plan.layers) == 1
    assert ledger.saves >= 3


@pytest.mark.parametrize(
    ("field", "value"),
    [("priority", "critical"), ("learnings", "what the team learned"), ("epic_id", "team-epic")],
)
@pytest.mark.asyncio
async def test_a_task_field_edited_on_the_team_server_survives_a_relink(
    field: str, value: str
) -> None:
    plan = build_plan(
        [
            _entity("epic_1", "epic"),
            _entity("dep_1", "task", created="2025-12-01"),
            _entity("task_1", "task", status="done", priority="low", learnings="source learnings"),
        ],
        [SourceEdge("DEPENDS_ON", "task_1", "dep_1"), SourceEdge("BELONGS_TO", "task_1", "epic_1")],
        project=PROJECT,
    )
    ledger = _Ledger()
    target = _Target(fail={"dep_1"})
    await _execute(target, ledger, plan)
    target.rows["target-task_1"]["metadata"][field] = value

    target.fail = set()
    outcome = await _execute(target, ledger, plan)

    assert outcome.relinked == 0
    assert target.rows["target-task_1"]["metadata"][field] == value


class _FlakyReads(_Target):
    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if method == "GET":
            raise RuntimeError("read timed out")
        return await super()._request(method, path, *args, **kwargs)


@pytest.mark.asyncio
async def test_a_failed_read_after_landing_neither_stops_the_run_nor_loses_the_row() -> None:
    ledger = _Ledger()

    outcome = await _execute(_FlakyReads(fail={"task_1"}), ledger)

    assert {"epic_1", "task_2", "decision_1"} <= set(ledger.ids)
    assert set(ledger.partial) == {"task_1", "task_2", "decision_1"}
    assert all(entry["digest"] is None for entry in ledger.partial.values())
    assert any("read timed out" in failure for failure in outcome.failures)

    rerun = await _execute(_Target(), ledger)

    assert rerun.relinked == 0
    assert any("could not confirm" in line for line in rerun.unlinked)
    assert ledger.partial == {}


def test_an_older_ledger_with_list_entries_loads_as_unconfirmed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from sibyl_cli import migrate

    monkeypatch.setattr(migrate, "_LEDGER_DIR", tmp_path)
    route = {
        "source_org": "s",
        "target_context": "t",
        "target_org_id": "o",
        "target_project_id": "p",
    }
    path = migrate._graph_ledger_path(route)
    path.write_text(
        json.dumps({"route": route, "ids": {"a": "t-a"}, "statuses": {}, "partial": {"a": ["b"]}})
    )

    _ids, _statuses, partial = migrate._load_graph_ledger(path, route)

    assert partial == {"a": {"missing": ["b"], "digest": None}}


@pytest.mark.parametrize(
    "attributes",
    [
        {"lifecycle_state": "archived"},
        {"lifecycle_state": "deleted"},
        {"review_state": "redacted"},
        {"lifecycle_flags": ["hidden"]},
        {"source_validation_pending": {"source": True}},
        {"correction_blockers": {"source": True}},
    ],
)
def test_canonical_lifecycle_exclusions_are_not_migrated(attributes: dict[str, Any]) -> None:
    from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable

    assert not graph_metadata_recallable(attributes)
    plan = build_plan([_entity("decision", "decision", **attributes)], [], project=PROJECT)
    assert plan.entities == []
    assert sum(plan.skipped.values()) == 1


def test_name_disambiguation_matches_the_server_colon_framing() -> None:
    from sibyl_core.tools.helpers import _generate_id

    plan = build_plan(
        [
            _entity("first", "decision", name="alpha:beta", category="gamma"),
            _entity("second", "decision", name="alpha", category="beta:gamma"),
        ],
        [],
        project=PROJECT,
    )
    actual_ids = {
        _generate_id(node.source.entity_type, node.name, node.source.category or "general")
        for node in plan.entities
    }
    assert len(actual_ids) == 2


class _FailedStatuses(_Target):
    fail_status = False
    queue_status = False

    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if method == "PATCH" and self.fail_status:
            raise RuntimeError("status write failed")
        if method == "PATCH" and self.queue_status:
            assert kwargs["params"] == {"sync": "true", "replay_interrupted": "false"}
            return {"mutation_receipt": {"applied": False}, "data": {"job_id": "queued"}}
        return await super()._request(method, path, *args, **kwargs)


async def test_linking_preserves_team_status_without_another_status_write() -> None:
    ledger = _Ledger()
    target = _FailedStatuses(fail={"epic_1"})
    plan = _epic_and_done_task()
    await _execute(target, ledger, plan)
    target.rows["target-task_1"]["metadata"]["status"] = "doing"
    target.fail.clear()
    target.fail_status = True
    linked = await _execute(target, ledger, plan)
    assert linked.failures == []
    assert target.rows["target-task_1"]["metadata"]["status"] == "doing"
    assert ledger.partial == {}
    await _execute(target, ledger, plan)
    assert target.rows["target-task_1"]["metadata"]["status"] == "doing"


async def test_a_legacy_status_restoration_without_revision_is_denied() -> None:
    ledger = _Ledger()
    target = _FailedStatuses()
    plan = build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    await _execute(target, ledger, plan)
    target.rows["target-task_1"]["metadata"]["status"] = "doing"
    ledger.statuses.clear()
    ledger.partial["task_1"] = {"restore_status": "done", "missing": [], "digest": None}
    old_calls = len(target.calls)
    result = await _execute(target, ledger, plan)
    assert result.failures and "no trustworthy saved revision" in result.failures[0]
    assert ledger.partial["task_1"]["restore_status"] == "done"
    assert target.rows["target-task_1"]["metadata"]["status"] == "doing"
    assert len(target.calls) == old_calls


async def test_queued_status_response_does_not_mark_the_ledger_applied() -> None:
    ledger = _Ledger()
    target = _FailedStatuses()
    target.queue_status = True
    result = await _execute(
        target, ledger, build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    )
    assert result.failures
    assert ledger.statuses == {}
    target.queue_status = False
    result = await _execute(
        target, ledger, build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    )
    assert result.failures == []
    assert target.rows["target-task_1"]["metadata"]["status"] == "done"


class _IdempotentTarget(_Target):
    def __init__(self) -> None:
        super().__init__()
        self.responses: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.keys: list[str] = []
        self.lose_ack = False

    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        import copy

        if method != "POST" or path != "/entities":
            return await super()._request(method, path, *args, **kwargs)
        key = kwargs["_idempotency_key"]
        assert key
        self.keys.append(key)
        body = kwargs["json"]
        if key in self.responses:
            previous, response = self.responses[key]
            if previous != body:
                raise _ApiError(409)
            return copy.deepcopy(response)
        response = await super()._request(method, path, *args, **kwargs)
        self.responses[key] = (copy.deepcopy(body), copy.deepcopy(response))
        if self.lose_ack:
            self.lose_ack = False
            raise RuntimeError("lost acknowledgement")
        return response


async def test_unknown_create_replays_receipt_without_replacing_a_team_edit() -> None:
    ledger = _Ledger()
    target = _IdempotentTarget()
    target.lose_ack = True
    plan = build_plan([_entity("decision_1", "decision")], [], project=PROJECT)
    result = await _execute(target, ledger, plan)
    assert result.failures and not ledger.ids
    target.rows["target-decision_1"]["content"] = "team edit"
    result = await _execute(target, ledger, plan)
    assert result.failures == []
    assert target.rows["target-decision_1"]["content"] == "team edit"
    assert len(target.keys) == 2 and target.keys[0] == target.keys[1]


async def test_source_edits_do_not_mint_a_new_key_after_an_unknown_create() -> None:
    ledger = _Ledger()
    target = _IdempotentTarget()
    target.lose_ack = True
    original = build_plan(
        [_entity("decision_1", "decision", content="original")], [], project=PROJECT
    )
    result = await _execute(target, ledger, original)
    assert result.failures
    changed = build_plan(
        [_entity("decision_1", "decision", content="changed")], [], project=PROJECT
    )
    result = await _execute(target, ledger, changed)
    assert result.failures and not ledger.ids
    assert target.rows["target-decision_1"]["content"] == "original"
    assert len(set(target.keys)) == 1
    assert any("source changed" in failure for failure in result.failures)


async def test_relink_uses_a_distinct_key_for_the_resolved_links() -> None:
    ledger = _Ledger()
    target = _IdempotentTarget()
    target.fail = {"epic_1"}
    plan = _epic_and_done_task()
    await _execute(target, ledger, plan)
    target.fail.clear()
    result = await _execute(target, ledger, plan)
    assert result.failures == []
    task_keys = [
        key
        for key, (body, _) in target.responses.items()
        if body["metadata"]["migration"]["origin_entity_id"] == "task_1"
    ]
    assert len(task_keys) == 1
    links = [c for c in target.calls if c[1].endswith("/links")]
    assert len(links) == 1 and links[0][2]["expected_revision"] == 2


async def test_operation_namespace_qualifies_the_create_key() -> None:
    plan = build_plan([_entity("decision_1", "decision")], [], project=PROJECT)
    targets = [_IdempotentTarget(), _IdempotentTarget()]
    for target, namespace in zip(targets, ("route-one", "route-two"), strict=True):
        await execute_plan(
            target,
            plan,
            ids={},
            statuses={},
            partial={},
            target_project_id=PROJECT,
            origin_org="source-org",
            save=lambda: None,
            operation_namespace=namespace,
        )
    assert targets[0].keys[0] != targets[1].keys[0]


async def test_unknown_create_keeps_its_body_when_a_dependency_lands_on_retry() -> None:
    ledger = _Ledger()
    target = _IdempotentTarget()
    target.fail = {"epic_1"}
    target.lose_ack = True
    plan = _epic_and_done_task()
    first = await _execute(target, ledger, plan)
    assert first.failures and not ledger.ids
    assert "epic_id" not in ledger.partial["task_1"]["create_body"]["metadata"]
    original = ledger.partial["task_1"]["create_body"]
    target.fail.clear()
    second = await _execute(target, ledger, plan)
    assert second.failures == []
    assert target.rows["target-task_1"]["metadata"]["epic_id"] == "target-epic_1"
    assert target.rows["target-task_1"]["metadata"]["status"] == "done"
    assert ledger.partial == {}
    stored = [
        body
        for body, _ in target.responses.values()
        if body["metadata"]["migration"]["origin_entity_id"] == "task_1"
    ]
    assert stored == [original]
    assert any(call[1].endswith("/links") for call in target.calls)


async def test_create_intent_is_saved_before_any_target_write() -> None:
    ledger = _Ledger()
    plan = build_plan([_entity("decision_1", "decision")], [], project=PROJECT)

    class Target(_Target):
        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            if method == "POST":
                assert ledger.saves >= 1
                assert ledger.partial["decision_1"]["create_body"] == kwargs["json"]
            return await super()._request(method, path, *args, **kwargs)

    result = await _execute(Target(), ledger, plan)
    assert result.failures == [] and ledger.partial == {}


async def test_every_write_finds_its_own_intent_on_disk() -> None:
    import copy

    ledger = _Ledger()
    disk: dict[str, dict[str, Any]] = {}
    entities = [_entity("epic_1", "epic")] + [
        _entity(f"task_{n}", "task", status="done") for n in range(40)
    ]
    edges = [SourceEdge("BELONGS_TO", f"task_{n}", "epic_1") for n in range(40)]
    plan = build_plan(entities, edges, project=PROJECT)

    class Target(_Target):
        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            body = kwargs.get("json")
            if method == "POST" and path == "/entities":
                origin = body["metadata"]["migration"]["origin_entity_id"]
                assert disk[origin]["create_body"] == body
            if method == "PATCH":
                origin = next(o for o, t in ledger.ids.items() if path.endswith(t))
                assert disk[origin]["status_intent"]["body"] == body
            return await super()._request(method, path, *args, **kwargs)

    def save() -> None:
        ledger.saves += 1
        disk.clear()
        disk.update(copy.deepcopy(ledger.partial))

    result = await execute_plan(
        Target(),
        plan,
        ids=ledger.ids,
        statuses=ledger.statuses,
        partial=ledger.partial,
        target_project_id="project_target",
        origin_org="org-src",
        save=save,
    )
    assert result.failures == [] and result.created == 41 and result.statuses == 40


async def test_rows_starting_together_share_one_intent_save() -> None:
    ledger = _Ledger()
    plan = build_plan(
        [_entity(f"decision_{n}", "decision") for n in range(60)], [], project=PROJECT
    )

    result = await _execute(_Target(), ledger, plan)

    assert result.created == 60 and result.failures == []
    # One intent save for the layer and one when it finishes, not one per row.
    assert ledger.saves == 2


class _IdempotentStatusTarget(_IdempotentTarget):
    def __init__(self) -> None:
        super().__init__()
        self.status_responses: dict[str, dict[str, Any]] = {}
        self.status_keys: list[str] = []
        self.lose_status_ack = False

    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if method != "PATCH":
            return await super()._request(method, path, *args, **kwargs)
        key = kwargs["_idempotency_key"]
        assert key
        self.status_keys.append(key)
        if key in self.status_responses:
            return self.status_responses[key]
        result = await super()._request(method, path, *args, **kwargs)
        self.status_responses[key] = result
        if self.lose_status_ack:
            self.lose_status_ack = False
            raise RuntimeError("lost completed status response")
        return result


async def test_completed_status_retry_does_not_replace_a_newer_team_status() -> None:
    ledger = _Ledger()
    target = _IdempotentStatusTarget()
    target.lose_status_ack = True
    plan = build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    first = await _execute(target, ledger, plan)
    assert first.failures and ledger.statuses == {}
    target.rows["target-task_1"]["metadata"]["status"] = "doing"
    second = await _execute(target, ledger, plan)
    assert second.failures == []
    assert target.rows["target-task_1"]["metadata"]["status"] == "doing"
    assert len(target.status_keys) == 2 and len(set(target.status_keys)) == 1


async def test_link_only_recovery_does_not_reuse_the_initial_status_operation() -> None:
    ledger = _Ledger()
    target = _IdempotentStatusTarget()
    target.fail = {"epic_1"}
    plan = _epic_and_done_task()
    await _execute(target, ledger, plan)
    target.fail.clear()
    second = await _execute(target, ledger, plan)
    assert second.failures == []
    assert target.rows["target-task_1"]["metadata"]["status"] == "done"
    assert len(target.status_keys) == 1


async def test_interrupted_container_conflict_cannot_adopt_a_same_named_row() -> None:
    ledger = _Ledger()
    target = _Target(taken_containers={"epic_1": "team-epic"})

    class UncertainTarget(_Target):
        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            if method == "POST" and path == "/entities":
                error = _ApiError(409)
                error.error_code = "idempotency_reconciliation_required"
                raise error
            return await target._request(method, path, *args, **kwargs)

    result = await _execute(UncertainTarget(), ledger, _epic_and_done_task())
    assert result.failures
    assert result.adopted == 0
    assert "epic_1" not in ledger.ids
    assert target.calls == []


class _GuardedLinksTarget(_Target):
    def __init__(self) -> None:
        super().__init__()
        self.link_requests: list[dict[str, Any]] = []
        self.link_keys: list[str] = []
        self.stored_declarations: dict[str, set[str]] = {}
        self.edit_before_links = False
        self.lose_link_ack = False
        self.missing_link_endpoint = False

    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        import copy

        if method != "POST" or not path.endswith("/links"):
            return await super()._request(method, path, *args, **kwargs)
        request = copy.deepcopy(kwargs["json"])
        self.link_requests.append(request)
        self.link_keys.append(kwargs["_idempotency_key"])
        assert set(request) == {
            "expected_revision",
            "related_to",
            "epic_id",
            "parent_task_id",
            "depends_on",
        }
        if self.missing_link_endpoint:
            raise _ApiError(404)
        target_id = path.split("/")[-2]
        row = self.rows[target_id]
        if self.edit_before_links:
            self.edit_before_links = False
            row["content"] = "teammate edit after comparison"
            row["metadata"]["status"] = "doing"
            row["revision"] += 1
        metadata = row["metadata"]
        requested_edges = set(request["related_to"])
        existing_edges = self.stored_declarations.setdefault(target_id, set())
        complete = (
            requested_edges <= existing_edges
            and set(request["depends_on"]) <= set(metadata.get("depends_on") or [])
            and all(
                not request.get(field) or metadata.get(field) == request[field]
                for field in ("epic_id", "parent_task_id")
            )
        )
        if complete:
            return {"entity_id": target_id, "revision": row["revision"], "replayed": True}
        if row["revision"] != request["expected_revision"]:
            raise _ApiError(409)
        result = await super()._request(method, path, *args, **kwargs)
        existing_edges.update(requested_edges)
        if self.lose_link_ack:
            self.lose_link_ack = False
            raise RuntimeError("lost link acknowledgement")
        return result


async def test_additive_links_refuse_an_edit_between_comparison_and_write() -> None:
    ledger = _Ledger()
    target = _GuardedLinksTarget()
    target.fail = {"epic_1"}
    plan = _epic_and_done_task()
    await _execute(target, ledger, plan)
    target.fail.clear()
    target.edit_before_links = True
    outcome = await _execute(target, ledger, plan)
    assert outcome.failures and outcome.relinked == 0
    row = target.rows["target-task_1"]
    assert row["content"] == "teammate edit after comparison"
    assert row["metadata"]["status"] == "doing"
    assert "epic_id" not in row["metadata"]
    assert ledger.partial["task_1"]["link_body"]["expected_revision"] == 2
    assert len([c for c in target.calls if c[0] == "POST" and c[1] == "/entities"]) == 3


async def test_unknown_link_replays_the_same_intent_without_touching_team_edits() -> None:
    ledger = _Ledger()
    target = _GuardedLinksTarget()
    target.fail = {"epic_1"}
    plan = _epic_and_done_task()
    await _execute(target, ledger, plan)
    target.fail.clear()
    target.lose_link_ack = True
    failed = await _execute(target, ledger, plan)
    assert failed.failures
    intent = ledger.partial["task_1"]["link_body"]
    row = target.rows["target-task_1"]
    assert row["metadata"]["epic_id"] == "target-epic_1"
    row["content"] = "edited after completed link"
    row["metadata"]["status"] = "doing"
    row["revision"] += 1
    calls = len(target.calls)
    recovered = await _execute(target, ledger, plan)
    assert recovered.failures == [] and recovered.relinked == 1
    assert ledger.partial == {}
    assert row["content"] == "edited after completed link"
    assert row["metadata"]["status"] == "doing"
    assert target.link_requests == [intent, intent]
    assert len(set(target.link_keys)) == 1
    assert target.calls[calls:] == []


async def test_old_server_link_endpoint_requires_upgrade_without_full_rewrite() -> None:
    ledger = _Ledger()
    target = _GuardedLinksTarget()
    target.fail = {"epic_1"}
    plan = _epic_and_done_task()
    await _execute(target, ledger, plan)
    target.fail.clear()
    target.missing_link_endpoint = True
    result = await _execute(target, ledger, plan)
    assert result.failures and any("upgrade" in failure for failure in result.failures)
    assert target.rows["target-task_1"]["metadata"]["status"] == "done"
    assert ledger.partial["task_1"]["link_body"]["expected_revision"] == 2
    assert len([c for c in target.calls if c[0] == "POST" and c[1] == "/entities"]) == 3


@pytest.mark.parametrize("revision", [None, 0, True, "1"])
async def test_missing_or_coerced_revisions_cannot_authorize_additive_links(revision: Any) -> None:
    ledger = _Ledger()
    target = _GuardedLinksTarget()
    target.fail = {"epic_1"}
    plan = _epic_and_done_task()
    await _execute(target, ledger, plan)
    target.rows["target-task_1"]["revision"] = revision
    target.fail.clear()
    result = await _execute(target, ledger, plan)
    assert result.failures and any("revision" in failure for failure in result.failures)
    assert target.link_requests == []


async def test_link_intent_is_saved_before_the_additive_request() -> None:
    ledger = _Ledger()

    class Target(_GuardedLinksTarget):
        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            if path.endswith("/links"):
                assert ledger.partial["task_1"]["link_body"] == kwargs["json"]
                assert ledger.saves > saves_before
            return await super()._request(method, path, *args, **kwargs)

    target = Target()
    target.fail = {"epic_1"}
    plan = _epic_and_done_task()
    await _execute(target, ledger, plan)
    saves_before = ledger.saves
    target.fail.clear()
    result = await _execute(target, ledger, plan)
    assert result.failures == [] and ledger.partial == {}


class _GuardedInitialStatusTarget(_IdempotentStatusTarget):
    edit_before_status = False
    status_requests: list[dict[str, Any]]

    def __init__(self) -> None:
        super().__init__()
        self.status_requests = []

    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        import copy

        if method == "PATCH":
            self.status_requests.append(copy.deepcopy(kwargs["json"]))
            if self.edit_before_status:
                self.edit_before_status = False
                row = self.rows[path.rsplit("/", 1)[-1]]
                row["metadata"]["status"] = "doing"
                row["content"] = "new team body"
                row["revision"] += 1
        return await super()._request(method, path, *args, **kwargs)


async def test_initial_status_guard_refuses_a_team_edit_after_creation() -> None:
    ledger = _Ledger()
    target = _GuardedInitialStatusTarget()
    target.edit_before_status = True
    plan = build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    result = await _execute(target, ledger, plan)
    assert result.failures and result.statuses == 0 and ledger.statuses == {}
    row = target.rows["target-task_1"]
    assert row["metadata"]["status"] == "doing" and row["content"] == "new team body"
    assert row["revision"] == 2
    assert ledger.partial["task_1"]["status_intent"]["body"] == {
        "status": "done",
        "expected_revision": 1,
    }
    second = await _execute(target, ledger, plan)
    assert second.failures and target.status_requests == [
        {"status": "done", "expected_revision": 1},
        {"status": "done", "expected_revision": 1},
    ]
    assert row["metadata"]["status"] == "doing" and row["revision"] == 2


async def test_lost_create_receipt_does_not_adopt_an_edited_revision_for_status() -> None:
    ledger = _Ledger()
    target = _GuardedInitialStatusTarget()
    target.lose_ack = True
    plan = build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    first = await _execute(target, ledger, plan)
    assert first.failures and ledger.ids == {}
    row = target.rows["target-task_1"]
    row["metadata"]["status"] = "doing"
    row["revision"] += 1
    second = await _execute(target, ledger, plan)
    assert second.failures and ledger.statuses == {}
    assert target.status_requests == [{"status": "done", "expected_revision": 1}]
    assert row["metadata"]["status"] == "doing" and row["revision"] == 2


async def test_status_intent_is_saved_from_the_exact_create_receipt_before_patch() -> None:
    ledger = _Ledger()

    class Target(_Target):
        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            if method == "POST" and path == "/entities":
                assert kwargs["params"] == {
                    "sync": "true",
                    "replay_interrupted": "false",
                    "protect_ownership": "true",
                }
                response = await super()._request(method, path, *args, **kwargs)
                self.rows[response["id"]]["revision"] = 7
                return {**response, "revision": 7}
            if method == "PATCH":
                intent = ledger.partial["task_1"]["status_intent"]
                assert intent["target_id"] == "target-task_1"
                assert (
                    intent["body"] == kwargs["json"] == {"status": "done", "expected_revision": 7}
                )
                assert intent["key"] == kwargs["_idempotency_key"]
                assert ledger.saves >= 2
            return await super()._request(method, path, *args, **kwargs)

    target = Target()
    result = await _execute(
        target, ledger, build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    )
    assert result.failures == [] and ledger.partial == {} and ledger.statuses == {"task_1": "done"}
    assert target.rows["target-task_1"]["revision"] == 8


@pytest.mark.parametrize("revision", [None, 0, True, "1"])
async def test_missing_or_coerced_create_revision_cannot_authorize_status(revision: Any) -> None:
    ledger = _Ledger()

    class Target(_Target):
        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            response = await super()._request(method, path, *args, **kwargs)
            return {**response, "revision": revision} if method == "POST" else response

    target = Target()
    result = await _execute(
        target, ledger, build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    )
    assert result.failures and ledger.ids == {} and ledger.statuses == {}
    assert not any(call[0] == "PATCH" for call in target.calls)
    assert "create_body" in ledger.partial["task_1"]


@pytest.mark.parametrize("changed_status", ["blocked", "todo"])
async def test_changed_source_status_cannot_replace_a_saved_status_intent(
    changed_status: str,
) -> None:
    ledger = _Ledger()
    target = _IdempotentStatusTarget()
    target.lose_status_ack = True
    original = build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    first = await _execute(target, ledger, original)
    assert first.failures
    keys = target.status_keys.copy()
    changed = build_plan([_entity("task_1", "task", status=changed_status)], [], project=PROJECT)
    result = await _execute(target, ledger, changed)
    assert result.failures and "source status" in result.failures[0]
    assert target.status_keys == keys
    assert ledger.partial["task_1"]["status_intent"]["body"]["status"] == "done"


async def test_legacy_landed_task_without_status_revision_fails_without_target_read() -> None:
    ledger = _Ledger()
    ledger.ids["task_1"] = "target-task_1"
    target = _Target()
    target.rows["target-task_1"] = {"metadata": {"status": "doing"}, "revision": 4}
    result = await _execute(
        target, ledger, build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    )
    assert result.failures and "no trustworthy saved create revision" in result.failures[0]
    assert target.calls == [] and target.rows["target-task_1"]["metadata"]["status"] == "doing"
    retry = await _execute(
        target, ledger, build_plan([_entity("task_1", "task", status="done")], [], project=PROJECT)
    )
    assert retry.failures and "no trustworthy saved create revision" in retry.failures[0]
    assert target.calls == []


async def test_saved_create_revision_recovers_status_after_initial_target_read_failure() -> None:
    import json

    class Target(_GuardedInitialStatusTarget):
        fail_first_task_read = True

        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            if method == "GET" and path == "/entities/target-task_1" and self.fail_first_task_read:
                self.fail_first_task_read = False
                raise RuntimeError("first post-create read failed")
            return await super()._request(method, path, *args, **kwargs)

    target = Target()
    target.fail = {"epic_1"}
    plan = _epic_and_done_task()
    first_ledger = _Ledger()
    first = await _execute(target, first_ledger, plan)
    assert first.failures
    assert first_ledger.ids == {"task_1": "target-task_1"}
    assert first_ledger.partial["task_1"]["created_revision"] == 1
    assert "status_intent" not in first_ledger.partial["task_1"]
    assert first_ledger.statuses == {}
    # Reconstruct the next process's ledger from the saved JSON values.
    saved = json.loads(
        json.dumps(
            {
                "ids": first_ledger.ids,
                "partial": first_ledger.partial,
                "statuses": first_ledger.statuses,
            }
        )
    )
    resumed = _Ledger()
    resumed.ids, resumed.partial, resumed.statuses = (
        saved["ids"],
        saved["partial"],
        saved["statuses"],
    )
    target.fail.clear()
    second = await _execute(target, resumed, plan)
    assert second.failures == []
    assert resumed.statuses == {"task_1": "done"}
    assert target.status_requests == [{"status": "done", "expected_revision": 1}]
    assert target.rows["target-task_1"]["metadata"]["status"] == "done"
    assert second.unlinked  # No saved body cut means the missing links remain explicitly reported.


@pytest.mark.parametrize("kind", ["decision", "epic", "task"])
async def test_saved_complete_create_receipt_clears_empty_partial_without_target_io(
    kind: str,
) -> None:
    import json

    plan = build_plan([_entity("complete_1", kind, status="todo")], [], project=PROJECT)
    target = _Target()
    original = _Ledger()
    first = await _execute(target, original, plan)
    assert first.created == 1 and first.failures == []
    # A sibling can save the create receipt before this row's final cleanup.
    saved = json.loads(
        json.dumps(
            {
                "ids": original.ids,
                "statuses": original.statuses,
                "partial": {"complete_1": {"missing": [], "digest": None, "created_revision": 1}},
            }
        )
    )
    resumed = _Ledger()
    resumed.ids, resumed.statuses, resumed.partial = (
        saved["ids"],
        saved["statuses"],
        saved["partial"],
    )
    before = json.loads(json.dumps(target.rows))
    target.calls.clear()
    second = await _execute(target, resumed, plan)
    assert second.resumed == 1 and second.created == second.relinked == 0
    assert second.failures == second.unlinked == []
    assert resumed.partial == {} and target.calls == [] and target.rows == before
    assert resumed.saves == 1


class _UndoTarget(_Target):
    """Answers status receipts with their revision and honours guarded deletes.

    Target ids in `shared` stand for rows someone outside the migration
    depends on; the server refuses an unshared delete of them.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shared: set[str] = set()

    def _refusal(self, target_id: str, params: dict[str, Any]) -> _ApiError | None:
        if target_id not in self.rows:
            return _ApiError(404)
        if self.rows[target_id]["revision"] != int(params["expected_revision"]):
            return _ApiError(409, "revision_conflict")
        if target_id in self.shared:
            return _ApiError(409, "entity_shared")
        return None

    async def _request(self, method: str, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        params = kwargs.get("params") or {}
        if method == "GET" and path.endswith("/deletable"):
            refusal = self._refusal(path.split("/")[-2], params)
            if refusal is None:
                return {"deletable": True}
            if refusal.status_code == 404:
                raise refusal
            return {"deletable": False, "error": refusal.error_code, "reason": "refused"}
        if method == "DELETE":
            self.calls.append((method, path, params))
            assert params.get("if_unshared") == "true"
            target_id = path.rsplit("/", 1)[-1]
            if (refusal := self._refusal(target_id, params)) is not None:
                raise refusal
            del self.rows[target_id]
            return {}
        response = await super()._request(method, path, *args, **kwargs)
        if method == "PATCH":
            target_id = path.rsplit("/", 1)[-1]
            return {
                "mutation_receipt": {"applied": True, "revision": self.rows[target_id]["revision"]}
            }
        return response


async def _migrate_with_revisions(
    target: _Target, plan: GraphPlan | None = None
) -> tuple[_Ledger, dict[str, int]]:
    ledger = _Ledger()
    revisions: dict[str, int] = {}
    await execute_plan(
        target,
        plan or _plan(),
        ids=ledger.ids,
        statuses=ledger.statuses,
        partial=ledger.partial,
        target_project_id="project_target",
        origin_org="org-src",
        save=lambda: None,
        revisions=revisions,
        structure=ledger.structure,
        preexisting=ledger.preexisting,
    )
    return ledger, revisions


async def _undo(
    target: _Target,
    ledger: _Ledger,
    revisions: dict[str, int],
    plan: GraphPlan | None = None,
    *,
    dry_run: bool = False,
) -> Any:
    from sibyl_cli.migrate_graph import undo_plan

    return await undo_plan(
        target,
        structure=ledger.structure,
        ids=ledger.ids,
        revisions=revisions,
        statuses=ledger.statuses,
        partial=ledger.partial,
        save=lambda: None,
        dry_run=dry_run,
    )


@pytest.mark.asyncio
async def test_revisions_track_the_migrations_last_write_to_each_row() -> None:
    target = _UndoTarget()

    _ledger, revisions = await _migrate_with_revisions(target)

    for origin, target_id in _ledger.ids.items():
        assert revisions[origin] == target.rows[target_id]["revision"], origin
    assert revisions["task_1"] == 2


@pytest.mark.asyncio
async def test_undo_removes_untouched_rows_and_keeps_edited_ones_with_what_they_link_to() -> None:
    target = _UndoTarget()
    ledger, revisions = await _migrate_with_revisions(target)
    edited = target.rows["target-decision_1"]
    edited["content"] = "a teammate's revision"
    edited["revision"] += 1

    outcome = await _undo(target, ledger, revisions)

    assert set(target.rows) == {"target-decision_1", "target-task_1", "target-epic_1"}
    assert outcome.removed == 1
    assert [line.split(":")[0] for line in outcome.kept_edited] == ["decision decision_1"]
    assert {line.split(":")[0] for line in outcome.kept_linked} == {"task task_1", "epic epic_1"}
    assert "task_2" not in ledger.ids and "task_2" not in revisions
    assert set(ledger.ids) == {"decision_1", "task_1", "epic_1"}
    deletes = [call[1] for call in target.calls if call[0] == "DELETE"]
    assert deletes == ["/entities/target-task_2"]


@pytest.mark.asyncio
async def test_an_undo_of_untouched_rows_removes_everything_it_created() -> None:
    target = _UndoTarget()
    ledger, revisions = await _migrate_with_revisions(target)

    outcome = await _undo(target, ledger, revisions)

    assert outcome.removed == 4 and target.rows == {}
    assert ledger.ids == {} and revisions == {}


@pytest.mark.asyncio
async def test_an_undo_dry_run_writes_nothing() -> None:
    target = _UndoTarget()
    ledger, revisions = await _migrate_with_revisions(target)
    before = set(target.rows)

    outcome = await _undo(target, ledger, revisions, dry_run=True)

    assert outcome.removed == 4
    assert set(target.rows) == before
    assert not [call for call in target.calls if call[0] == "DELETE"]
    assert len(ledger.ids) == 4


@pytest.mark.asyncio
async def test_an_undo_never_removes_a_teammates_adopted_container() -> None:
    target = _UndoTarget(taken_containers={"epic_1": "team-epic"})
    target.rows["team-epic"] = {"id": "team-epic", "revision": 7, "metadata": {}}
    plan = _epic_and_done_task()
    ledger, revisions = await _migrate_with_revisions(target, plan)

    outcome = await _undo(target, ledger, revisions, plan)

    assert "team-epic" in target.rows
    assert "target-task_1" not in target.rows
    assert any("epic_1" in line for line in outcome.kept_unrecorded)


@pytest.mark.asyncio
async def test_an_undo_leaves_a_row_that_is_no_longer_this_migrations() -> None:
    target = _UndoTarget()
    ledger, revisions = await _migrate_with_revisions(target)
    target.rows["target-decision_1"]["metadata"]["migration"] = {"origin_entity_id": "someone_else"}

    outcome = await _undo(target, ledger, revisions)

    assert "target-decision_1" in target.rows
    assert any("decision_1" in line for line in outcome.kept_unrecorded)


@pytest.mark.asyncio
async def test_a_large_layer_reports_row_level_progress() -> None:
    plan = build_plan(
        [
            _entity(f"d{n}", "decision", created=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z")
            for n in range(600)
        ],
        [],
        project=PROJECT,
    )
    lines: list[str] = []
    ledger = _Ledger()

    await execute_plan(
        _Target(),
        plan,
        ids=ledger.ids,
        statuses=ledger.statuses,
        partial=ledger.partial,
        target_project_id="project_target",
        origin_org="org-src",
        save=lambda: None,
        log=lines.append,
    )

    progress = [line for line in lines if "rows (" in line]
    assert [line.split(" of ")[0].strip() for line in progress] == ["250", "500"]
    assert all("of 600 rows" in line for line in progress)


@pytest.mark.asyncio
async def test_undo_runs_a_layer_concurrently_and_still_protects_kept_links() -> None:
    entities = [_entity("epic_1", "epic")] + [_entity(f"task_{n}", "task") for n in range(12)]
    edges = [SourceEdge("BELONGS_TO", f"task_{n}", "epic_1") for n in range(12)]
    plan = build_plan(entities, edges, project=PROJECT)

    class Target(_UndoTarget):
        in_flight = 0
        peak = 0
        order: ClassVar[list[str]] = []

        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            if method != "DELETE":
                return await super()._request(method, path, *args, **kwargs)
            Target.in_flight += 1
            Target.peak = max(Target.peak, Target.in_flight)
            await asyncio.sleep(0.001)
            Target.in_flight -= 1
            Target.order.append(path.rsplit("/", 1)[-1])
            return await super()._request(method, path, *args, **kwargs)

    target = Target()
    ledger, revisions = await _migrate_with_revisions(target, plan)
    # A teammate edits the last task; the epic it belongs to must stay with it.
    target.rows["target-task_11"]["revision"] += 1

    outcome = await _undo(target, ledger, revisions, plan)

    assert outcome.removed == 11 and Target.peak > 1
    assert set(target.rows) == {"target-task_11", "target-epic_1"}
    assert [entry.split(":")[0] for entry in outcome.kept_linked] == ["epic epic_1"]


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.asyncio
async def test_undo_keeps_a_row_someone_outside_the_migration_depends_on(dry_run: bool) -> None:
    plan = build_plan(
        [_entity("epic_1", "epic"), _entity("task_1", "task"), _entity("task_2", "task")],
        [
            SourceEdge("BELONGS_TO", "task_1", "epic_1"),
            SourceEdge("BELONGS_TO", "task_2", "epic_1"),
        ],
        project=PROJECT,
    )
    target = _UndoTarget()
    ledger, revisions = await _migrate_with_revisions(target, plan)
    # A teammate filed a task under the migrated epic.
    target.shared = {"target-epic_1"}

    outcome = await _undo(target, ledger, revisions, plan, dry_run=dry_run)

    assert outcome.removed == 2
    assert [line.split(":")[0] for line in outcome.kept_shared] == ["epic epic_1"]
    assert ("target-epic_1" in target.rows) and ("epic_1" in ledger.ids)
    assert len(target.rows) == (3 if dry_run else 1)


async def _execute_tracked(
    target: _Target, ledger: _Ledger, revisions: dict[str, int], plan: GraphPlan
) -> Any:
    return await execute_plan(
        target,
        plan,
        ids=ledger.ids,
        statuses=ledger.statuses,
        partial=ledger.partial,
        target_project_id="project_target",
        origin_org="org-src",
        save=lambda: None,
        revisions=revisions,
        structure=ledger.structure,
        preexisting=ledger.preexisting,
    )


@pytest.mark.asyncio
async def test_a_teammates_status_change_stops_a_relink_and_is_not_claimed() -> None:
    ledger, revisions = _Ledger(), {}
    target = _Target(fail={"epic_1"})
    await _execute_tracked(target, ledger, revisions, _epic_and_done_task())
    migrated_at = revisions["task_1"]
    # The relink digest does not cover status, so only the revision shows this.
    row = target.rows["target-task_1"]
    row["metadata"]["status"] = "doing"
    row["revision"] += 1

    target.fail = set()
    outcome = await _execute_tracked(target, ledger, revisions, _epic_and_done_task())

    assert outcome.relinked == 0
    assert row["metadata"]["status"] == "doing"
    assert revisions["task_1"] == migrated_at
    assert any("changed on the team server" in line for line in outcome.unlinked)


@pytest.mark.asyncio
async def test_a_create_that_landed_on_an_existing_row_is_never_undone() -> None:
    plan = build_plan(
        [_entity("decision_1", "decision"), _entity("decision_2", "decision")], [], project=PROJECT
    )

    class Target(_UndoTarget):
        async def _request(
            self, method: str, path: str, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            response = await super()._request(method, path, *args, **kwargs)
            body = kwargs.get("json") or {}
            origin = ((body.get("metadata") or {}).get("migration") or {}).get("origin_entity_id")
            if method == "POST" and path == "/entities" and origin == "decision_2":
                # The id already held the author's own row, which the create updated.
                self.rows["target-decision_2"]["revision"] = 4
                return {**response, "revision": 4}
            return response

    target = Target()
    ledger, revisions = await _migrate_with_revisions(target, plan)
    assert ledger.preexisting == {"decision_2"} and "decision_2" not in revisions

    outcome = await _undo(target, ledger, revisions, plan)

    assert outcome.removed == 1 and set(target.rows) == {"target-decision_2"}
    assert [line.split(":")[0] for line in outcome.kept_unrecorded] == ["decision decision_2"]


@pytest.mark.asyncio
async def test_undo_follows_the_ledger_not_the_source() -> None:
    target = _UndoTarget()
    ledger, revisions = await _migrate_with_revisions(target, _epic_and_done_task())
    target.rows["target-task_1"]["revision"] += 1  # a teammate edits the task

    # No plan at all: the source could have changed or be gone.
    from sibyl_cli.migrate_graph import undo_plan

    outcome = await undo_plan(
        target,
        structure=ledger.structure,
        ids=ledger.ids,
        revisions=revisions,
        statuses=ledger.statuses,
        partial=ledger.partial,
        save=lambda: None,
    )

    assert outcome.removed == 0
    assert [line.split(":")[0] for line in outcome.kept_linked] == ["epic epic_1"]
    assert set(target.rows) == {"target-task_1", "target-epic_1"}
