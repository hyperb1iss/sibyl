"""Who each readable row credits, what kind of event it is, and the rollup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from surrealdb.data.types.datetime import Datetime

from sibyl.api.routes import activity_rollup as rollup
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl_core.models.entities import Entity, EntityType

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
ALICE = str(uuid4())
BOB = str(uuid4())
STRANGER = str(uuid4())
MEMBERS = {
    ALICE: rollup.Member(ALICE, "Alice", "alice@example.test", None, "member"),
    BOB: rollup.Member(BOB, "Bob", None, "https://avatars.test/bob", "owner"),
}
WEEK = rollup.resolve_window("7d", now=NOW)


def entity(
    entity_type: EntityType = EntityType.DECISION,
    *,
    created_by: str | None = ALICE,
    age: timedelta = timedelta(hours=1),
    metadata: dict[str, object] | None = None,
    modified_by: str | None = None,
    updated_age: timedelta | None = None,
) -> Entity:
    return Entity(
        id=f"{entity_type.value}_{uuid4().hex[:8]}",
        entity_type=entity_type,
        name="Row",
        created_by=created_by,
        modified_by=modified_by,
        created_at=NOW - age,
        updated_at=NOW - (updated_age if updated_age is not None else age),
        metadata=metadata or {},
    )


def events(row: Entity) -> list[tuple[str, str]]:
    return [
        (event.kind, event.actor_id)
        for event in rollup.entity_events(row, window=WEEK, members=MEMBERS, project_id=None)
    ]


def test_windows_end_now_and_reach_back_their_span() -> None:
    for label, span in (("24h", timedelta(hours=24)), ("7d", timedelta(days=7))):
        window = rollup.resolve_window(label, now=NOW)
        assert (window.until, window.until - window.since) == (NOW, span)
    month = rollup.resolve_window("30d", now=NOW.replace(tzinfo=None))
    assert month.until == NOW
    assert month.holds(NOW - timedelta(days=29))
    assert not month.holds(NOW)
    with pytest.raises(KeyError):
        rollup.resolve_window("1y", now=NOW)


def test_stored_instants_from_every_plane_compare_in_utc() -> None:
    aware = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    assert rollup.utc(aware.replace(tzinfo=None)) == aware
    assert rollup.utc("2026-10-01T09:30:00Z") == aware
    assert rollup.utc(Datetime("2026-10-01T09:30:00.000000Z")) == aware
    assert rollup.utc("not a date") is None
    assert rollup.utc(None) is None


@pytest.mark.parametrize(
    ("entity_type", "kind"),
    [
        (EntityType.DECISION, "decision"),
        (EntityType.NOTE, "note"),
        (EntityType.PROCEDURE, "procedure"),
        (EntityType.TASK, "task_created"),
        (EntityType.ERROR_PATTERN, "entity"),
        (EntityType.EPIC, "entity"),
    ],
)
def test_authored_rows_count_under_their_kind(entity_type: EntityType, kind: str) -> None:
    assert events(entity(entity_type)) == [(kind, ALICE)]


@pytest.mark.parametrize(
    "row",
    [
        entity(EntityType.TOPIC),
        entity(EntityType.PASSAGE),
        entity(EntityType.COMMUNITY),
        entity(metadata={"category": "memory_projection"}),
        entity(metadata={"category": "memory_fact_projection"}),
        entity(metadata={"capture_mode": "reflect"}),
        entity(metadata={"capture_surface": "reflection"}),
        entity(metadata={"reflection_identity": {"version": 2}}),
        entity(metadata={"migration": {"tool": "sibyl migrate to-team"}}),
    ],
)
def test_server_derived_rows_credit_nobody(row: Entity) -> None:
    assert events(row) == []


def test_authorship_reads_created_by_then_the_stamped_principal() -> None:
    assert events(entity(created_by=None, metadata={"principal_id": BOB})) == [("decision", BOB)]
    assert events(entity(created_by=STRANGER, metadata={"principal_id": BOB})) == [
        ("decision", BOB)
    ]
    assert events(entity(created_by="system")) == []
    assert events(entity(created_by=STRANGER)) == []
    assert events(entity(created_by=ALICE.upper())) == [("decision", ALICE)]


def test_rows_created_outside_the_window_do_not_count() -> None:
    assert events(entity(age=timedelta(days=8))) == []


def done(**metadata: object) -> dict[str, object]:
    return {"status": "done", **metadata}


def test_a_stamped_completion_credits_its_actor() -> None:
    row = entity(
        EntityType.TASK,
        created_by=ALICE,
        age=timedelta(days=30),
        updated_age=timedelta(hours=2),
        modified_by=ALICE,
        metadata=done(completed_at=(NOW - timedelta(hours=2)).isoformat(), completed_by=BOB),
    )
    assert events(row) == [("task_completed", BOB)]


def test_a_completion_by_a_non_member_credits_nobody() -> None:
    row = entity(
        EntityType.TASK,
        age=timedelta(days=30),
        updated_age=timedelta(hours=2),
        modified_by=ALICE,
        metadata=done(completed_at=(NOW - timedelta(hours=2)).isoformat(), completed_by=STRANGER),
    )
    assert events(row) == []


def test_an_unstamped_workflow_completion_does_not_guess() -> None:
    row = entity(
        EntityType.TASK,
        age=timedelta(days=30),
        updated_age=timedelta(hours=2),
        modified_by=BOB,
        metadata=done(completed_at=(NOW - timedelta(hours=2)).isoformat()),
    )
    assert events(row) == []


def test_a_done_task_without_a_completion_record_credits_nobody() -> None:
    """Its last edit is not its completion: crediting the editor would guess."""
    row = entity(
        EntityType.TASK,
        age=timedelta(days=30),
        updated_age=timedelta(hours=4),
        modified_by=BOB,
        metadata=done(),
    )
    assert events(row) == []


def test_a_finisher_without_an_instant_credits_nobody() -> None:
    row = entity(
        EntityType.TASK,
        age=timedelta(days=30),
        updated_age=timedelta(hours=4),
        metadata=done(completed_by=BOB),
    )
    assert events(row) == []


def test_a_stamped_completion_is_dated_by_its_own_instant() -> None:
    row = entity(
        EntityType.TASK,
        age=timedelta(days=30),
        updated_age=timedelta(minutes=5),
        metadata=done(completed_at=(NOW - timedelta(hours=4)).isoformat(), completed_by=BOB),
    )
    [event] = rollup.entity_events(row, window=WEEK, members=MEMBERS, project_id="p1")
    assert (event.kind, event.actor_id, event.at) == (
        "task_completed",
        BOB,
        NOW - timedelta(hours=4),
    )
    assert event.project_id == "p1"


def test_a_completion_before_the_window_does_not_count() -> None:
    row = entity(
        EntityType.TASK,
        age=timedelta(days=30),
        updated_age=timedelta(hours=1),
        metadata=done(completed_at=(NOW - timedelta(days=9)).isoformat(), completed_by=BOB),
    )
    assert events(row) == []


def test_a_task_created_and_completed_in_the_window_counts_twice() -> None:
    row = entity(
        EntityType.TASK,
        created_by=ALICE,
        age=timedelta(hours=5),
        metadata=done(completed_at=(NOW - timedelta(hours=1)).isoformat(), completed_by=BOB),
    )
    assert events(row) == [("task_created", ALICE), ("task_completed", BOB)]


def test_hrefs_follow_the_web_routes() -> None:
    task = entity(EntityType.TASK)
    assert rollup.entity_href(task) == f"/tasks/{task.id}"
    epic = entity(EntityType.EPIC)
    assert rollup.entity_href(epic) == f"/epics/{epic.id}"
    project = entity(EntityType.PROJECT)
    assert rollup.entity_href(project) == f"/projects?id={project.id}"
    note = entity(EntityType.NOTE, metadata={"task_id": "task_abc"})
    assert rollup.entity_href(note) == "/tasks/task_abc"
    decision = entity()
    assert rollup.entity_href(decision) == f"/entities/{decision.id}"
    assert rollup.capture_href("a b") == "/memory/captures?id=a%20b"


def capture(**fields: object) -> RawCaptureRecord:
    defaults: dict[str, object] = {
        "organization_id": UUID(int=1),
        "title": "Paste mid-pipeline swallows stdin",
        "raw_content": "body",
        "entity_type": "raw_memory",
        "principal_id": ALICE,
        "created_at": (NOW - timedelta(hours=1)).replace(tzinfo=None),
    }
    return RawCaptureRecord(**{**defaults, **fields})  # type: ignore[arg-type]


def test_a_graph_row_counted_as_created_stands_for_its_raw_memory() -> None:
    raw_id = str(uuid4())
    written = entity(created_by=None, metadata={"principal_id": ALICE, "raw_memory_id": raw_id})
    old_row = entity(age=timedelta(days=20), metadata={"raw_memory_id": "kept-old"})
    derived = entity(
        EntityType.PASSAGE,
        metadata={"raw_memory_id": "kept-derived", "category": "passage_projection"},
    )
    rows = [written, old_row, derived]
    feed = [
        item
        for row in rows
        for item in rollup.entity_events(row, window=WEEK, members=MEMBERS, project_id=None)
    ]
    # Only the counted row hides its memory; the old and derived rows do not.
    assert rollup.mirrored_capture_ids(rows, feed) == {raw_id}


def test_a_standalone_capture_credits_its_principal() -> None:
    record = capture(project_id="p1")
    event = rollup.capture_event(record, window=WEEK, members=MEMBERS)
    assert event is not None
    assert (event.kind, event.actor_id, event.project_id) == ("capture", ALICE, "p1")
    assert event.at == NOW - timedelta(hours=1)
    assert event.href == f"/memory/captures?id={record.id}"


@pytest.mark.parametrize(
    "record",
    [
        capture(entity_type="decision", entity_id="decision_1"),
        capture(metadata={"projected_capture_id": str(uuid4())}),
        capture(metadata={"projected_entity_id": "decision_1"}),
        capture(capture_surface="reflection_candidate"),
        capture(metadata={"capture_surface": "synthesis_artifact"}),
        capture(capture_surface="migration"),
        capture(deleted_at=NOW.replace(tzinfo=None)),
        capture(principal_id=STRANGER),
        capture(created_at=(NOW - timedelta(days=8)).replace(tzinfo=None)),
    ],
)
def test_mirrors_system_rows_and_strangers_credit_nobody(record: RawCaptureRecord) -> None:
    assert rollup.capture_event(record, window=WEEK, members=MEMBERS) is None


def event(
    kind: str, actor: str, hours: float, ident: str = "", project: str | None = None
) -> rollup.ActivityEvent:
    return rollup.ActivityEvent(
        kind=kind,  # type: ignore[arg-type]
        id=ident or uuid4().hex,
        title=kind,
        entity_type=None,
        project_id=project,
        actor_id=actor,
        at=NOW - timedelta(hours=hours),
        href="/x",
    )


def test_summary_lists_every_member_busiest_first() -> None:
    carol = str(uuid4())
    members = {
        **MEMBERS,
        carol: rollup.Member(carol, "carol", None, None, "viewer"),
    }
    summary = rollup.summarize(
        [
            event("capture", ALICE, 3),
            event("decision", BOB, 2),
            event("task_completed", BOB, 1),
            event("note", STRANGER, 0.5),
        ],
        members=members,
        window=WEEK,
    )
    assert [person.name for person in summary.people] == ["Bob", "Alice", "carol"]
    bob = summary.people[0]
    assert bob.counts.decisions == 1
    assert bob.counts.tasks_completed == 1
    assert bob.last_active_at == NOW - timedelta(hours=1)
    assert bob.role == "owner"
    assert summary.people[2].counts.model_dump() == dict.fromkeys(
        rollup.ActivityCounts.model_fields, 0
    )
    assert summary.people[2].last_active_at is None
    # The stranger's row is dropped from the feed as well as the counts.
    assert [item.actor_id for item in summary.recent] == [BOB, BOB, ALICE]
    assert summary.truncated is False
    assert summary.window.label == "7d"
    assert (summary.project_id, summary.project_ids, summary.actor_id) == (None, None, None)


def test_recent_keeps_the_newest_hundred_and_says_so() -> None:
    feed = [event("capture", ALICE, hour / 10) for hour in range(rollup.RECENT_LIMIT + 5)]
    summary = rollup.summarize(feed, members=MEMBERS, window=WEEK, project_ids=["p1"])
    assert len(summary.recent) == rollup.RECENT_LIMIT
    assert summary.truncated is True
    assert summary.recent[0].at == NOW
    assert summary.project_id == "p1"
    assert summary.project_ids == ["p1"]
    # Counts cover every event, not only the listed ones.
    assert summary.people[0].counts.captures == rollup.RECENT_LIMIT + 5


def test_feed_items_name_their_actor_and_only_readable_projects() -> None:
    summary = rollup.summarize(
        [event("decision", BOB, 1, project="p1"), event("note", ALICE, 2, project="p2")],
        members=MEMBERS,
        window=WEEK,
        project_ids=["p1", "p2"],
        project_names={"p1": "Shared Board"},
    )
    bob_item, alice_item = summary.recent
    assert (bob_item.actor_name, bob_item.actor_avatar_url) == ("Bob", "https://avatars.test/bob")
    assert bob_item.project_name == "Shared Board"
    assert (alice_item.actor_name, alice_item.actor_avatar_url) == ("Alice", None)
    # p2 is absent from the readable names, so its name never appears.
    assert alice_item.project_name is None
    # Several projects requested: no single project_id, every id echoed.
    assert (summary.project_id, summary.project_ids) == (None, ["p1", "p2"])


def test_actor_filter_narrows_recent_but_not_the_counts() -> None:
    feed = [event("capture", ALICE, hour) for hour in range(3)]
    feed += [event("decision", BOB, hour + 0.5) for hour in range(rollup.RECENT_LIMIT + 1)]
    summary = rollup.summarize(feed, members=MEMBERS, window=WEEK, actor_filter=ALICE.upper())
    assert {item.actor_id for item in summary.recent} == {ALICE}
    assert len(summary.recent) == 3
    assert summary.truncated is False
    assert summary.actor_id == ALICE.upper()
    counts = {person.user_id: person.counts for person in summary.people}
    assert counts[BOB].decisions == rollup.RECENT_LIMIT + 1
    assert counts[ALICE].captures == 3
    nobody = rollup.summarize(feed, members=MEMBERS, window=WEEK, actor_filter="not-a-member")
    assert nobody.recent == []
    assert nobody.truncated is False
    flood = rollup.summarize(feed, members=MEMBERS, window=WEEK, actor_filter=BOB)
    assert len(flood.recent) == rollup.RECENT_LIMIT
    assert flood.truncated is True


def test_other_is_documented_as_the_entity_kind() -> None:
    description = rollup.ActivityCounts.model_json_schema()["properties"]["other"]["description"]
    assert "`entity`" in description


def test_members_read_from_the_member_listing() -> None:
    members = rollup.members_from_rows(
        [
            {
                "user": {"id": ALICE, "name": "", "email": "a@x.test", "avatar_url": None},
                "role": "Admin",
            },
            {"user": {"id": "not-a-uuid"}, "role": "member"},
            {"user": None},
            {"user": {"id": BOB}, "role": "warlock"},
        ]
    )
    assert set(members) == {ALICE, BOB}
    assert members[ALICE].name == "a@x.test"
    assert members[ALICE].role == "admin"
    assert members[BOB].name == BOB
    assert members[BOB].role == "member"


@pytest.mark.parametrize("user", [None, SimpleNamespace(id="not-a-user-id")])
def test_a_credential_naming_no_user_is_refused_not_crashed(user) -> None:
    from fastapi import HTTPException

    from sibyl.api.routes.activity import _caller_uuid

    with pytest.raises(HTTPException) as refused:
        _caller_uuid(SimpleNamespace(user=user))
    assert refused.value.status_code == 401
