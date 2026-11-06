"""The feed answers every cursor with a page or a 422, never a server error.

A cursor is opaque, but a client can hand back anything. Only a token in
exactly the form the server issues reads a page; anything else, a cursor in an
older format (an ISO instant, on the calendar or off it) included, is refused
with 422 before it reaches the engine. Runs on the embedded engine and, with
SIBYL_LIVE_SURREAL_TESTS=1, on a live server.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl_core.services.net import NetCursor
from tests.test_routes_net import (
    client_for,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
    settle,
)


def token(instant: str, message_id: str = "") -> str:
    raw = f"{instant}|{message_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


async def feed_status(org: SimpleNamespace, since: str) -> str:
    """The status, or the exception the app raised instead of answering."""
    try:
        async with client_for(org, org.grace) as client:
            response = await client.get("/api/net/feed", params={"since": since, "limit": 5})
    except Exception as exc:
        return f"raised {type(exc).__name__}"
    return str(response.status_code)


ON_THE_CALENDAR = [
    "2026-11-02T10:00:00Z",
    "2026-11-02T10:00:00.123456789Z",
    "2026-11-02T10:00:00.1Z",
    "2026-11-02T10:00:00-00:00",
    "2026-11-02T10:00:00+05:30",
    "2024-02-29T23:59:59.999999999Z",
]
OFF_THE_CALENDAR = [
    "2026-02-29T10:00:00Z",
    "2026-11-02T10:00:60Z",
    "2026-11-02T10:00:00+24:00",
    # Offset minutes are not on any clock past 59.
    "2026-11-02T10:00:00+05:60",
    "2026-11-02T10:00:00+00:99",
    "2026-11-02T10:00:00.5-03:75",
]
# On the calendar locally, but a full day from UTC at the ends of the range.
AT_THE_EDGES = [
    "0001-01-01T00:00:00+23:59",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59.999999999-23:59",
    "9999-12-31T23:59:59.999999999Z",
]


@pytest.mark.parametrize(
    "instant", ["0", "1762077600000000000", "1762077600123456789", str(2**63 - 1)]
)
async def test_a_cursor_in_the_issued_form_reads_a_page(org: SimpleNamespace, instant: str) -> None:
    assert await feed_status(org, token(instant, str(uuid4()))) == "200"
    assert await feed_status(org, token(instant)) == "200"


@pytest.mark.parametrize("created_at", ON_THE_CALENDAR)
async def test_a_cursor_in_the_older_instant_format_is_a_422(
    org: SimpleNamespace, created_at: str
) -> None:
    """Pre-launch cursors carried an ISO instant; they are refused, not reinterpreted.

    A 422 sends the CLI back to starting from now, its documented behaviour.
    """
    assert await feed_status(org, token(created_at, str(uuid4()))) == "422"


@pytest.mark.parametrize(
    "instant",
    [str(2**63), "-1", "+1", "01", " 1", "1 ", "1.0", "1e9", "\uff11", "0x10", ""],
)
async def test_an_instant_off_the_issued_form_is_a_422(org: SimpleNamespace, instant: str) -> None:
    assert await feed_status(org, token(instant)) == "422"


@pytest.mark.parametrize("created_at", OFF_THE_CALENDAR)
async def test_a_cursor_off_the_calendar_is_a_422(org: SimpleNamespace, created_at: str) -> None:
    assert await feed_status(org, token(created_at)) == "422"


@pytest.mark.parametrize("created_at", AT_THE_EDGES)
async def test_a_cursor_at_the_ends_of_time_reads_a_page_or_is_refused(
    org: SimpleNamespace, created_at: str
) -> None:
    assert await feed_status(org, token(created_at)) in {"200", "422"}


@pytest.mark.parametrize(
    "spell",
    [str.upper, "{{{}}}".format, "urn:uuid:{}".format, lambda value: value.replace("-", "")],
    ids=["uppercase", "braced", "urn", "unhyphenated"],
)
async def test_a_respelled_cursor_id_never_changes_what_comes_next(
    org: SimpleNamespace, spell
) -> None:
    """Messages sharing one timestamp are ordered by id after a cursor.

    Re-spelling the cursor's id (the same UUID) must either be refused or read
    exactly what the server's own cursor reads; anything else replays or skips
    the messages that share the cursor's timestamp.
    """
    for index in range(6):
        posted = await post(org, org.ada, channel="#ties", body=f"tie {index}")
        assert posted.status_code == 201, posted.text
    await org.content.execute_query(
        "LET $tie = time::now() - 30s; "
        "UPDATE net_messages SET created_at = $tie WHERE organization_id = $org;",
        org=org.id,
    )
    await settle(org)
    async with client_for(org, org.grace) as client:
        everything = (await client.get("/api/net/feed", params={"limit": 10})).json()["messages"]
        assert len(everything) == 6
        middle = everything[2]
        canonical = NetCursor.decode(middle["cursor"])
        respelled = token(str(canonical.created_ns), spell(canonical.uuid))
        expected = await client.get("/api/net/feed", params={"since": middle["cursor"]})
        actual = await client.get("/api/net/feed", params={"since": respelled})

    assert [m["id"] for m in expected.json()["messages"]] == [m["id"] for m in everything[3:]]
    if actual.status_code == 422:
        return
    assert actual.status_code == 200, actual.text
    assert [m["id"] for m in actual.json()["messages"]] == [m["id"] for m in everything[3:]]


async def test_garbage_cursors_are_422_not_500(org: SimpleNamespace) -> None:
    garbage = [
        "",
        "%%%",
        "\u00e9t\u00e9",
        token("2026-11-02T10:00:00Z", "not-a-uuid"),
        token("2026-11-02T10:00:00Z|x", str(uuid4())),
        token("\uff12\uff10\uff12\uff16-11-02T10:00:00Z"),
        token("2026-11-02T10:00:00.1234567890Z"),
    ]
    statuses = {since: await feed_status(org, since) for since in garbage}

    assert all(status == "422" for status in statuses.values()), statuses
