"""Paged archive reads keep every datetime at the server's own nanosecond text.

The SDK decodes datetimes to microseconds, so each page is read a second time
for the server's text of each datetime path. That re-read keeps only the
columns holding a datetime: SurrealDB 4 charges each `type::field` in
proportion to the document it walks, and a page still carrying its vectors
cost seconds on 4.0.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.migrate.archive_stream import PageReader
from sibyl_core.migrate.source_integrity import ArchiveDatetime

ROWS = [
    # A top-level datetime, one nested in an object, one in an array, a vector,
    # an alias, a column that holds a datetime only on some rows, and a column
    # name that needs escaping.
    "{uuid: 'r0', at: d'2026-09-10T02:47:11.572211763Z', vector: $vector, "
    "meta: {seen: d'2026-01-02T03:04:05.000000001Z', note: 'kept'}, "
    "hist: [d'2025-12-31T23:59:59.999999999Z', 'literal'], `odd-name`: d'2024-02-29T00:00:00Z'}",
    "{uuid: 'r1', at: d'2026-09-10T02:47:11.5Z', vector: $vector, meta: {note: 'no date'}}",
    "{uuid: 'r2', at: d'1969-07-20T20:17:40.123456789Z', vector: $vector, sometimes: d'2030-01-01T00:00:00.000000123Z'}",
    "{uuid: 'r3', at: d'2026-09-10T02:47:11Z', vector: $vector, hist: []}",
    "{uuid: 'r4', at: d'2026-09-10T02:47:11.000999999Z', vector: $vector, meta: {seen: d'2026-09-10T02:47:11.000000999Z'}}",
]


@pytest.fixture(params=["embedded", "live"])
async def client(request) -> AsyncIterator[DedicatedSurrealClient]:
    namespace = f"datetime_capture_{uuid4().hex[:10]}"
    if request.param == "embedded":
        client = DedicatedSurrealClient(url="memory://", namespace=namespace, database="db")
    else:
        if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
            pytest.skip("live SurrealDB tests are disabled")
        url = os.environ.get("SIBYL_SURREAL_URL", "")
        if not url or is_embedded_surreal_url(url):
            pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
        client = DedicatedSurrealClient(
            url=url,
            username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            namespace=namespace,
            database="db",
        )
    try:
        yield client
    finally:
        if request.param == "live":
            await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
        await client.close()


def _texts(value: Any) -> Any:
    """Every datetime as its kept server text; everything else as is."""
    if isinstance(value, datetime):
        assert isinstance(value, ArchiveDatetime), value
        return value.native_text
    if isinstance(value, dict):
        return {key: _texts(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_texts(item) for item in value]
    return value


async def test_paged_rows_keep_each_datetime_at_the_server_text(
    client: DedicatedSurrealClient,
) -> None:
    await client.execute_query(
        "DEFINE TABLE capture_probe SCHEMALESS;"
        + "".join(f"CREATE capture_probe CONTENT {row};" for row in ROWS),
        vector=[0.25] * 1024,
    )
    # The server's own text for every datetime, read without the SDK's decoder in the way.
    expected = {
        row["uuid"]: row
        for row in await client.execute_query(
            "SELECT uuid, <string> at AS at, <string> (meta.seen ?? '') AS seen,"
            " <string> (hist[0] ?? '') AS hist0, <string> (sometimes ?? '') AS sometimes,"
            " <string> (`odd-name` ?? '') AS `odd-name` FROM capture_probe;"
        )
    }
    sent: list[str] = []

    async def execute(query: str, **params: object) -> object:
        sent.append(query)
        return await client.execute_query(query, **params)

    rows = await PageReader(execute, page_size=2).read_all(
        "capture_probe",
        fields="*, string::concat('key-', uuid) AS alias",
        omit=("id",),
        where="true",
        params={},
        precise_datetimes=True,
    )

    assert sorted(row["uuid"] for row in rows) == sorted(expected)
    for row in rows:
        want = expected[row["uuid"]]
        got = _texts(row)
        assert got["at"] == want["at"]
        assert got["alias"] == f"key-{row['uuid']}"
        assert row["vector"] == [0.25] * 1024
        if "seen" in (row.get("meta") or {}):
            assert got["meta"]["seen"] == want["seen"]
        if row.get("hist"):
            assert got["hist"] == [want["hist0"], "literal"]
        if "sometimes" in row:
            assert got["sometimes"] == want["sometimes"]
        if "odd-name" in row:
            assert got["odd-name"] == want["odd-name"]
    by_uuid = {row["uuid"]: _texts(row) for row in rows}
    assert by_uuid["r0"]["at"] == "2026-09-10T02:47:11.572211763Z"
    assert by_uuid["r0"]["meta"]["seen"] == "2026-01-02T03:04:05.000000001Z"
    assert by_uuid["r2"]["at"] == "1969-07-20T20:17:40.123456789Z"
    # Each re-read projects the datetime columns alone, never the vector.
    rereads = [query for query in sent if "type::field" in query]
    assert len(rereads) == 3
    for query in rereads:
        projection = query.split("page: (SELECT ", 1)[1].split(" FROM (", 1)[0]
        assert "vector" not in projection
        assert "`at`" in projection
