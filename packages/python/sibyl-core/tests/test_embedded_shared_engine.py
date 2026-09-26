"""File-backed embedded stores run every client through one engine.

Two engines opened on one SurrealKV path in one process each keep a private
index over the same log files, so each reads the other's bytes back as corrupt
values ("Invalid revision `N` for type `Value`") and neither sees the other's
writes. The embedded daemon opens separate auth, content, and graph clients on
one data directory, which is how a fresh signup followed by /auth/me failed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sibyl_core.backends.surreal import dedicated_client as dedicated_client_module
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient

pytest.importorskip("surrealdb")

# Past SurrealKV's inline-value threshold, so reads go through the log offsets
# that a second engine on the same files corrupts.
_PAD = "x" * 300


def _client(url: str, namespace: str, database: str = "db") -> DedicatedSurrealClient:
    return DedicatedSurrealClient(url=url, namespace=namespace, database=database)


@pytest.fixture(autouse=True)
def no_leaked_engines():
    yield
    assert dedicated_client_module._shared_embedded_engines == {}


async def test_clients_in_different_namespaces_read_back_what_they_wrote(tmp_path) -> None:
    url = f"surrealkv://{tmp_path / 'store'}"
    auth = _client(url, "sibyl_auth", "auth")
    content = _client(url, "sibyl_content", "content")
    try:
        # The order the daemon writes in: content bootstraps, auth signs a
        # user up, content persists telemetry, auth opens a session.
        await content.execute_query("CREATE settings SET pad = $pad;", pad=_PAD)
        await auth.execute_query("CREATE users:nova SET name = 'Nova', bio = $pad;", pad=_PAD)
        await content.execute_query("CREATE telemetry SET pad = $pad;", pad=_PAD)
        await auth.execute_query("CREATE sessions:one SET token = $pad;", pad=_PAD)

        assert await auth.execute_query("SELECT VALUE name FROM users;") == ["Nova"]
        assert await auth.execute_query("SELECT VALUE string::len(token) FROM sessions;") == [300]
        assert await content.execute_query("SELECT VALUE string::len(pad) FROM telemetry;") == [300]
        # Namespaces stay separate on the shared engine.
        assert await content.execute_query("SELECT VALUE name FROM users;") == []
    finally:
        await asyncio.gather(auth.close(), content.close())


async def test_a_second_client_sees_the_first_clients_writes(tmp_path) -> None:
    url = f"surrealkv://{tmp_path / 'store'}"
    shared = _client(url, "sibyl_auth", "auth")
    # A short-lived client on the same namespace, as a readiness probe builds.
    probe = _client(url, "sibyl_auth", "auth")
    try:
        # Both are open before the write, so a private engine behind the probe
        # would hold a snapshot that never learns about it.
        await asyncio.gather(
            shared.execute_query("RETURN true;"), probe.execute_query("RETURN true;")
        )
        await shared.execute_query("CREATE users:nova SET name = 'Nova', bio = $pad;", pad=_PAD)
        assert await probe.execute_query("SELECT VALUE name FROM users;") == ["Nova"]
        await probe.close()
        # Closing one client leaves the engine open for the others.
        assert await shared.execute_query("SELECT VALUE name FROM users;") == ["Nova"]
    finally:
        await asyncio.gather(shared.close(), probe.close())


async def test_the_store_reopens_with_its_data_after_every_client_closes(tmp_path) -> None:
    url = f"surrealkv://{tmp_path / 'store'}"
    writer = _client(url, "sibyl_auth", "auth")
    await writer.execute_query("CREATE users:nova SET name = 'Nova', bio = $pad;", pad=_PAD)
    await writer.close()
    assert dedicated_client_module._shared_embedded_engines == {}

    # A different spelling of the same directory still maps to one engine.
    reader = _client(f"surrealkv://{tmp_path / 'store'}/", "sibyl_auth", "auth")
    other = _client(url, "sibyl_content", "content")
    try:
        assert await reader.execute_query("SELECT VALUE name FROM users;") == ["Nova"]
        await other.execute_query("RETURN true;")
        assert len(dedicated_client_module._shared_embedded_engines) == 1
    finally:
        await asyncio.gather(reader.close(), other.close())


async def test_statement_results_exclude_the_namespace_scope(tmp_path) -> None:
    url = f"surrealkv://{tmp_path / 'store'}"
    client = _client(url, "sibyl_auth", "auth")
    try:
        results = await client.execute_query_batch("RETURN 1; RETURN 2;")
        assert results == [1, 2]
        raw = await client.execute_query_raw("RETURN session::ns();")
        assert isinstance(raw, dict)
        assert [statement["result"] for statement in raw["result"]] == ["sibyl_auth"]
        # A transaction's BEGIN and COMMIT add no envelopes, so its one write
        # is still the first and only statement result the caller sees.
        created = await client.execute_query("BEGIN; CREATE users:one SET name = 'Nova'; COMMIT;")
        assert isinstance(created, list)
        assert [record["name"] for record in created] == ["Nova"]
    finally:
        await client.close()


async def test_namespaces_stay_isolated_under_concurrent_mixed_traffic(tmp_path) -> None:
    """The multi-tenant guarantee: on one shared engine, no row crosses namespaces.

    Every row records the namespace its writer believed it was in. Writers run
    single statements, multi-statement batches, and transactions concurrently,
    with sleeps inside so the executions interleave on the engine. Each read
    must see only its own namespace, and a fresh engine must find each
    namespace holding exactly the rows its writer was told succeeded.
    """
    url = f"surrealkv://{tmp_path / 'store'}"
    namespaces = [f"org_{index:02d}" for index in range(6)] + ["sibyl_auth", "sibyl_content"]
    clients = {namespace: _client(url, namespace, "graph") for namespace in namespaces}
    rounds = 20

    async def tenant(namespace: str, client: DedicatedSurrealClient) -> int:
        written = 0
        for step in range(rounds):
            tag = f"{namespace}:{step}"
            await client.execute_query(
                "CREATE probe SET ns = $ns, tag = $tag, pad = $pad;",
                ns=namespace,
                tag=tag,
                pad=_PAD,
            )
            seen = await client.execute_query("SELECT VALUE ns FROM probe;")
            assert set(seen) == {namespace}
            batch = await client.execute_query_batch(
                "CREATE probe SET ns = $ns, tag = $tag, pad = $pad;"
                " RETURN sleep(2ms);"
                " RETURN [session::ns(), session::db()];"
                " SELECT VALUE ns FROM probe WHERE tag = $tag;",
                ns=namespace,
                tag=tag,
                pad=_PAD,
            )
            assert len(batch) == 4
            assert batch[2] == [namespace, "graph"]
            assert batch[3] == [namespace, namespace]
            committed = await client.execute_query(
                "BEGIN;"
                " CREATE probe SET ns = $ns, tag = $tag, phase = 'tx1';"
                " LET $pause = sleep(1ms);"
                " CREATE probe SET ns = $ns, tag = $tag, phase = 'tx2';"
                " COMMIT;",
                ns=namespace,
                tag=tag,
            )
            assert isinstance(committed, list)
            assert [row["ns"] for row in committed] == [namespace]
            written += 4
        return written

    try:
        written = await asyncio.gather(
            *(tenant(namespace, client) for namespace, client in clients.items())
        )
    finally:
        await asyncio.gather(*(client.close() for client in clients.values()))
    assert dedicated_client_module._shared_embedded_engines == {}

    from surrealdb import AsyncSurreal

    reader = AsyncSurreal(url)
    await reader.connect()
    try:
        for namespace, expected in zip(clients, written, strict=True):
            await reader.use(namespace, "graph")
            rows = await reader.query("SELECT ns, count() AS n FROM probe GROUP BY ns;")
            assert rows == [{"ns": namespace, "n": expected}]
    finally:
        await reader.close()


async def test_same_namespace_writers_survive_commit_conflicts(monkeypatch, tmp_path) -> None:
    """Writers racing on one record retry the engine's conflict instead of failing.

    The embedded engine words a lost commit race differently from a server
    ("read or write conflict"), and the retry used to miss it, so the losing
    write surfaced as an InternalError.
    """
    retries = 0
    retry_delay = dedicated_client_module._transaction_conflict_retry_delay

    def counted_delay(retry_count: int) -> float:
        nonlocal retries
        retries += 1
        return retry_delay(retry_count)

    monkeypatch.setattr(dedicated_client_module, "_transaction_conflict_retry_delay", counted_delay)
    url = f"surrealkv://{tmp_path / 'store'}"
    writers = [_client(url, "org_shared", "graph") for _ in range(4)]
    rounds = 60
    attempts = 0

    async def write(worker: int, offset: int) -> None:
        for step in range(offset, offset + rounds):
            await writers[worker].execute_query(
                "BEGIN; CREATE probe SET worker = $worker, step = $step;"
                " UPSERT counter:hot SET n += 1; COMMIT;",
                worker=worker,
                step=step,
            )

    try:
        # Conflicts are a race, so keep writing until one has been retried.
        while retries == 0 and attempts < 10:
            await asyncio.gather(
                *(write(worker, attempts * rounds) for worker in range(len(writers)))
            )
            attempts += 1
        assert retries > 0, "no commit conflict occurred to exercise the retry"
        rows = await writers[0].execute_query(
            "SELECT worker, step, count() AS n FROM probe GROUP BY worker, step;"
        )
        # Every write landed exactly once: none surfaced, none was doubled.
        assert len(rows) == len(writers) * rounds * attempts
        assert {row["n"] for row in rows} == {1}
    finally:
        await asyncio.gather(*(writer.close() for writer in writers))


def _install_counting_surreal(monkeypatch) -> list[Any]:
    engines: list[Any] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.url = url
            self.connects = 0
            self.closed = False
            self.used: list[tuple[str, str]] = []
            self.queries: list[str] = []
            engines.append(self)

        async def connect(self) -> None:
            self.connects += 1
            await asyncio.sleep(0)

        async def use(self, namespace: str, database: str) -> None:
            self.used.append((namespace, database))

        async def query_raw(self, query: str, params: object | None = None) -> dict[str, Any]:
            self.queries.append(query)
            statements = [part for part in query.split(";") if part.strip()]
            return {
                "result": [{"status": "OK", "result": None}]
                + [{"status": "OK", "result": [part.strip()]} for part in statements[1:]]
            }

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    return engines


async def test_concurrent_clients_open_one_engine_per_path(monkeypatch, tmp_path) -> None:
    engines = _install_counting_surreal(monkeypatch)
    url = f"surrealkv://{tmp_path / 'store'}"
    clients = [_client(url, f"org_{index}") for index in range(5)]
    try:
        results = await asyncio.gather(
            *(client.execute_query("SELECT * FROM entity") for client in clients)
        )
        assert results == [["SELECT * FROM entity"]] * 5
        assert len(engines) == 1
        engine = engines[0]
        assert engine.connects == 1
        # The engine's own session never selects a namespace; every query
        # carries its client's scope instead.
        assert engine.used == []
        assert sorted(query.split(";")[0] for query in engine.queries if "entity" in query) == [
            f"USE NS `org_{index}` DB `db`" for index in range(5)
        ]
    finally:
        await asyncio.gather(*(client.close() for client in clients))
    assert engines[0].closed


async def test_a_cancelled_last_close_still_finishes_before_a_reopen(monkeypatch, tmp_path) -> None:
    engines = _install_counting_surreal(monkeypatch)
    import surrealdb

    closing_started = asyncio.Event()
    finish_close = asyncio.Event()

    async def slow_close(self) -> None:
        closing_started.set()
        await finish_close.wait()
        self.closed = True

    monkeypatch.setattr(surrealdb.AsyncSurreal, "close", slow_close)
    url = f"surrealkv://{tmp_path / 'store'}"
    first = _client(url, "sibyl_auth")
    await first.execute_query("RETURN 1")
    releasing = asyncio.create_task(first.close())
    await closing_started.wait()
    releasing.cancel()
    await asyncio.sleep(0)

    second = _client(url, "sibyl_content")
    reopening = asyncio.create_task(second.execute_query("RETURN 2"))
    await asyncio.sleep(0.05)
    # The old engine is still closing, so the new lease waits instead of
    # opening a second engine beside it.
    assert len(engines) == 1
    assert not reopening.done()

    finish_close.set()
    with pytest.raises(asyncio.CancelledError):
        await releasing
    assert await reopening == ["RETURN 2"]
    assert len(engines) == 2
    assert engines[0].closed
    await second.close()


async def test_a_cancelled_client_close_still_releases_its_lease(monkeypatch, tmp_path) -> None:
    engines = _install_counting_surreal(monkeypatch)
    client = _client(f"surrealkv://{tmp_path / 'store'}", "sibyl_auth")
    await client.execute_query("RETURN 1")
    closing = asyncio.create_task(client.close())
    # Let close() schedule the per-connection closes, then cancel it before
    # they get to run.
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert engines[0].closed
    assert dedicated_client_module._shared_embedded_engines == {}


async def test_memory_urls_keep_one_store_per_connection(monkeypatch) -> None:
    engines = _install_counting_surreal(monkeypatch)
    first = _client("memory://", "one")
    second = _client("memory://", "two")
    try:
        await first.execute_query("RETURN 1")
        await second.execute_query("RETURN 2")
        assert len(engines) == 2
        assert [engine.used for engine in engines] == [[("one", "db")], [("two", "db")]]
    finally:
        await asyncio.gather(first.close(), second.close())


async def test_a_failed_open_releases_its_lease(monkeypatch, tmp_path) -> None:
    engines = _install_counting_surreal(monkeypatch)
    import surrealdb

    async def refuse(self) -> None:
        raise OSError("store is unreadable")

    monkeypatch.setattr(surrealdb.AsyncSurreal, "connect", refuse)
    client = _client(f"surrealkv://{tmp_path / 'store'}", "sibyl_auth")
    with pytest.raises(OSError, match="unreadable"):
        await client.execute_query("RETURN 1")
    await client.close()
    assert [engine.closed for engine in engines] == [True]


def test_namespace_identifiers_are_escaped() -> None:
    assert dedicated_client_module._surreal_ident("org_ab12") == "`org_ab12`"
    assert dedicated_client_module._surreal_ident("we`ird") == "`we\\`ird`"
