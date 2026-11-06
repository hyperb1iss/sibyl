"""sibylnet history is kept forever: backups carry it and restores put it back verbatim.

A content backup carries every message and thread row of the organization. A
restore into a fresh store brings them back with the same ids, instants to
the nanosecond, thread pins, distillation watermarks and decision links, so
the feed pages exactly as before, cursors issued before the backup keep
working, and distillation picks up where it stopped instead of reflecting
settled windows again. Nothing but organization deletion removes them: no
retention or privacy purge reaches the tables. Runs on the embedded engine
and, with SIBYL_LIVE_SURREAL_TESTS=1, on a live server.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl.persistence.auth_runtime import request_user_deletion
from sibyl.persistence.content_runtime import purge_due_deleted_raw_captures
from sibyl.persistence.surreal import content as surreal_content
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.config import settings
from sibyl_core.services import content_client
from sibyl_core.services.net import (
    NetCursor,
    claim_net_thread,
    idle_net_threads,
    list_net_threads,
    net_thread_window,
    settle_net_thread,
)
from tests.test_routes_net import (
    CREDENTIALS,
    feed,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
    settle,
)

THREAD = "gh:acme/app#42"
_DATETIMES = {
    "net_messages": ("created_at",),
    "net_threads": (
        "first_message_at",
        "last_message_at",
        "reflected_through",
        "reflection_claimed_at",
        "updated_at",
    ),
}


async def history(org: SimpleNamespace) -> dict[str, str]:
    """A small organization history: two channels, a pinned thread, a decision."""
    posted: dict[str, str] = {}
    for name, user, body in (
        ("first", org.ada, {"channel": "#reviews", "thread": THREAD, "project_id": org.shared}),
        ("second", org.grace, {"channel": "#reviews", "thread": THREAD}),
        (
            "decision",
            org.ada,
            {"channel": "#reviews", "thread": THREAD, "kind": "decision", "project_id": org.shared},
        ),
        ("green", org.grace, {"channel": "#builds"}),
        ("red", org.ada, {"channel": "#builds"}),
        ("later", org.grace, {"channel": "#reviews", "thread": "other"}),
    ):
        response = await post(org, user, body=f"{name} message", **body)
        assert response.status_code == 201, response.text
        posted[name] = response.json()["message"]["id"]
    await settle(org)
    return posted


async def distill_thread(org: SimpleNamespace, thread: str) -> None:
    """Settle one thread through its last message, as a finished distillation does."""
    state = next(
        t
        for t in await list_net_threads(organization_id=org.id, channel="#reviews")
        if t.thread == thread
    )
    claimed = await claim_net_thread(state)
    assert claimed is not None
    window = await net_thread_window(claimed)
    assert window
    await settle_net_thread(claimed, through=window[-1].cursor, source_id="session_fixture")


def _exact(table: str) -> str:
    texts = ", ".join(
        f"IF {field} != NONE {{ type::string({field}) }} ELSE {{ NONE }} AS {field}"
        for field in _DATETIMES[table]
    )
    return f"SELECT *, {texts}, record::id(id) AS record_key FROM {table} "  # noqa: S608


async def rows(content: SurrealContentClient, organization_id: str) -> dict[str, list[dict]]:
    """Every net row of the organization with datetimes as the server's own text."""
    messages = content_client.normalize_records(
        await content.execute_query(
            _exact("net_messages") + "WHERE organization_id = $org ORDER BY created_ns, uuid;",
            org=organization_id,
        )
    )
    threads = content_client.normalize_records(
        await content.execute_query(
            _exact("net_threads") + "WHERE organization_id = $org ORDER BY channel, thread;",
            org=organization_id,
        )
    )
    # A message lands at a fresh record id; a thread at the key every post upserts.
    for message in messages:
        message.pop("record_key")
    return {"messages": messages, "threads": threads}


async def pages(org: SimpleNamespace, **params: object) -> list[dict[str, object]]:
    """The whole feed read in pages of two from the start of history."""
    since = NetCursor(created_ns=0, uuid="").encode()
    out: list[dict[str, object]] = []
    for _ in range(20):
        response = await feed(org, org.grace, since=since, limit=2, **params)
        assert response.status_code == 200, response.text
        page = response.json()
        out.append(page)
        if not page["messages"]:
            break
        since = page["next_since"]
    return out


@pytest.fixture
async def fresh_store(
    org: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SurrealContentClient]:
    """An empty content store on the same engine as ``org``."""
    url = str(settings.surreal_url)
    credentials = {} if is_embedded_surreal_url(url) else CREDENTIALS
    store = SurrealContentClient(
        url=url, namespace=f"verify_net_restore_{uuid4().hex}", **credentials
    )
    await bootstrap_content_schema(store)
    close = store.close
    monkeypatch.setattr(store, "close", AsyncMock())
    try:
        yield store
    finally:
        if not is_embedded_surreal_url(url):
            await store.execute_query(f"REMOVE NAMESPACE IF EXISTS {store.namespace};")
        await close()


def _serve_from(monkeypatch: pytest.MonkeyPatch, store: SurrealContentClient) -> None:
    async def scope() -> SurrealContentClient:
        return store

    monkeypatch.setattr(content_client, "get_shared_surreal_content_client", scope)
    monkeypatch.setattr(surreal_content, "get_shared_surreal_content_client", scope)


async def _backup(
    org: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, way: str, tmp_path: Path
) -> dict:
    """The organization's content archive, as the backup job or the migrate CLI writes it."""
    source = org.content
    close = source.close
    monkeypatch.setattr(source, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: source)
    try:
        if way == "payload":
            return json.loads(
                json.dumps(await content_archive.export_content_archive_payload(org.id))
            )
        capture = await content_archive.capture_content_archive(
            organization_id=org.id, workdir=tmp_path
        )
        try:
            capture.write(tmp_path / "content.json", [])
        finally:
            capture.close()
        return json.loads((tmp_path / "content.json").read_text(encoding="utf-8"))
    finally:
        monkeypatch.setattr(source, "close", close)


@pytest.mark.parametrize("way", ["backup_capture", "payload"])
async def test_a_restore_brings_channel_history_back_verbatim(
    org: SimpleNamespace,
    fresh_store: SurrealContentClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    way: str,
) -> None:
    posted = await history(org)
    await distill_thread(org, THREAD)
    before = await rows(org.content, org.id)
    before_pages = await pages(org)
    before_reviews = await pages(org, channel="#reviews")
    middle = before_pages[1]["next_since"]
    before_from_middle = (await feed(org, org.grace, since=middle, limit=10)).json()
    pending_before = sorted(
        (t.channel, t.thread)
        for t in await idle_net_threads(idle_minutes=0, limit=50)
        if t.organization_id == org.id
    )

    archive = await _backup(org, monkeypatch, way, tmp_path)
    assert archive["row_counts"]["net_messages"] == len(posted)
    assert archive["row_counts"]["net_threads"] == 3

    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: fresh_store)
    result = await content_archive.restore_content_archive_payload(archive)
    assert result.success, result.errors
    assert "net_messages" not in result.dropped_fields
    assert "net_threads" not in result.dropped_fields
    _serve_from(monkeypatch, fresh_store)

    # Every row, every field: ids, nanosecond instants and created_ns, the
    # thread pin, the watermark, the decision link, the thread record keys.
    assert await rows(fresh_store, org.id) == before
    decision = next(m for m in before["messages"] if m["uuid"] == posted["decision"])
    assert decision["decision_memory_id"]
    restored_memory = content_client.normalize_records(
        await fresh_store.execute_query(
            "SELECT uuid FROM raw_captures WHERE uuid = $id;", id=decision["decision_memory_id"]
        )
    )
    assert restored_memory == [{"uuid": decision["decision_memory_id"]}]
    # The feed pages identically, and a cursor from before the backup reads on.
    assert await pages(org) == before_pages
    assert await pages(org, channel="#reviews") == before_reviews
    assert (await feed(org, org.grace, since=middle, limit=10)).json() == before_from_middle

    # Distillation resumes where it stopped: the settled thread is not a
    # candidate and has nothing after its watermark.
    pending_after = sorted(
        (t.channel, t.thread)
        for t in await idle_net_threads(idle_minutes=0, limit=50)
        if t.organization_id == org.id
    )
    assert pending_after == pending_before
    assert ("#reviews", THREAD) not in pending_after
    settled = next(
        t
        for t in await list_net_threads(organization_id=org.id, channel="#reviews")
        if t.thread == THREAD
    )
    assert await net_thread_window(settled) == []

    # The next post lands on the restored thread row, pin and all.
    response = await post(org, org.grace, channel="#reviews", thread=THREAD, body="after restore")
    assert response.status_code == 201, response.text
    threads = (await rows(fresh_store, org.id))["threads"]
    restored = [t for t in threads if t["thread"] == THREAD]
    assert len(restored) == 1
    assert restored[0]["message_count"] == 4
    assert restored[0]["project_id"] == org.shared
    assert restored[0]["reflection_pending"] is True


async def test_an_archive_from_before_sibylnet_never_wipes_its_history(
    org: SimpleNamespace,
    fresh_store: SurrealContentClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await history(org)
    before = await rows(org.content, org.id)
    archive = await _backup(org, monkeypatch, "payload", tmp_path)
    # Restored in place: the restore's close must not drop the store.
    monkeypatch.setattr(org.content, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: org.content)

    # An older archive cannot carry channel rows...
    older = {**archive, "version": "2.3", "tables": dict(archive["tables"])}
    with pytest.raises(ValueError, match=r"sibylnet history requires content archive 2\.4"):
        await content_archive.restore_content_archive_payload(older, clean=True)

    # ...and a clean restore of one that predates them leaves them in place.
    for table in ("net_messages", "net_threads"):
        older["tables"].pop(table)
        older["row_counts"].pop(table)
    older["total_rows"] = sum(older["row_counts"].values())
    result = await content_archive.restore_content_archive_payload(older, clean=True)
    assert result.success, result.errors
    assert await rows(org.content, org.id) == before


async def test_deleting_a_user_and_purging_their_memories_keeps_their_messages(
    org: SimpleNamespace,
) -> None:
    response = await post(
        org,
        org.ada,
        channel="#reviews",
        thread=THREAD,
        kind="decision",
        project_id=org.shared,
        body="keyset paging",
    )
    assert response.status_code == 201, response.text
    await post(org, org.ada, channel="#builds", body="plain message")
    private = str(uuid4())
    await org.content.execute_query(
        "CREATE raw_captures CONTENT $record;",
        record={
            "uuid": private,
            "organization_id": org.id,
            "principal_id": org.ada,
            "memory_scope": "private",
            "raw_content": "a private note",
        },
    )
    before = await rows(org.content, org.id)

    await request_user_deletion(user_id=UUID(org.ada), organization_id=UUID(org.id), request=None)
    await purge_due_deleted_raw_captures(now=datetime(2100, 1, 1, tzinfo=UTC))

    # The purge took the deleted user's private memories; the channels kept
    # every message they wrote and every thread row.
    remaining = await org.content.execute_query(
        "SELECT uuid FROM raw_captures WHERE uuid = $uuid;", uuid=private
    )
    assert content_client.normalize_records(remaining) == []
    assert await rows(org.content, org.id) == before


_DELETES_NET_ROWS = re.compile(
    r"\b(DELETE|REMOVE)\b[^;\"']{0,80}\bnet_(messages|threads)\b", re.IGNORECASE
)
# The only code that deletes sibylnet rows: organization deletion, and a
# restore replacing an organization's rows with the archive's.
_MAY_DELETE_NET_ROWS = {
    "apps/api/src/sibyl/persistence/surreal/organization_runtime.py",
    "apps/api/src/sibyl/persistence/content_archive.py",
}


def test_only_organization_deletion_and_restore_ever_delete_net_rows() -> None:
    root = Path(__file__).resolve().parents[3]
    offenders = sorted(
        str(path.relative_to(root))
        for base in ("apps/api/src", "packages/python/sibyl-core/src", "apps/cli/src")
        for path in (root / base).rglob("*.py")
        if _DELETES_NET_ROWS.search(path.read_text(encoding="utf-8"))
        and str(path.relative_to(root)) not in _MAY_DELETE_NET_ROWS
    )
    assert offenders == []
    for allowed in _MAY_DELETE_NET_ROWS:
        assert _DELETES_NET_ROWS.search((root / allowed).read_text(encoding="utf-8")), allowed
