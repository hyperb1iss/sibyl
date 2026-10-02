"""Lifecycle, authorization, and inert executor boundaries for the native leaf."""

from __future__ import annotations

import asyncio
import dataclasses
from uuid import uuid4

import pytest
from surrealdb.errors import InternalError, QueryError, SurrealError, ThrownError

from sibyl_core.backends.surreal import native_transaction as leaf

ORG = "00000000-0000-4000-8000-000000000001"


def binding():
    return leaf.NativeTransactionBinding(
        "ws://localhost:23108/rpc",
        "runtime-provider",
        "generation-1",
        "service",
        (
            leaf.NativeStoreScope(
                "content", "content_ns", "content", ORG, ("raw_captures", "source_states")
            ),
        ),
    )


def envelope(*results):
    return {"result": [{"status": "OK", "result": result} for result in results]}


class Socket:
    def __init__(self):
        self.close_error = None
        self.closed = False

    async def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class Connection:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.socket = Socket()
        self.txn = uuid4()
        self.calls = []
        self.response = envelope({"namespace": "content_ns", "database": "content"}, {"value": 1})
        self.commit_error = None
        self.cancel_error = None
        self.pending_cancel = None
        self.cancel_started = asyncio.Event()
        self.close_error = None
        self.closed = False
        self.close_started = asyncio.Event()
        self.close_release = None
        self.pending_query = None
        self.query_started = asyncio.Event()

    async def connect(self):
        self.calls.append(("connect",))

    async def signin(self, credentials):
        self.calls.append(("signin",))

    async def begin(self):
        self.calls.append(("begin",))
        return self.txn

    async def query_raw(self, query, params, *, txn_id):
        assert txn_id == self.txn
        self.calls.append(("query", query, params, txn_id))
        if query == "INFO FOR ROOT;":
            return envelope({"namespaces": {"content_ns": "definition"}})
        if query.endswith("INFO FOR NS;"):
            return envelope(
                {"namespace": "content_ns", "database": None},
                {"databases": {"content": "definition"}},
            )
        if query.endswith("INFO FOR DB;"):
            return envelope(
                {"namespace": "content_ns", "database": "content"},
                {"tables": {"raw_captures": "definition", "source_states": "definition"}},
            )
        if self.pending_query is not None:
            self.query_started.set()
            await self.pending_query
        return self.response

    async def commit(self, txn):
        assert txn == self.txn
        self.calls.append(("commit", txn))
        if self.commit_error:
            raise self.commit_error

    async def cancel(self, txn):
        assert txn == self.txn
        self.calls.append(("cancel", txn))
        self.cancel_started.set()
        if self.pending_cancel is not None:
            await self.pending_cancel
        if self.cancel_error:
            raise self.cancel_error

    async def close(self):
        self.calls.append(("close",))
        self.close_started.set()
        if self.close_release:
            await self.close_release.wait()
        self.closed = True
        if self.pending_query is not None and not self.pending_query.done():
            self.pending_query.cancel()
        if self.pending_cancel is not None and not self.pending_cancel.done():
            self.pending_cancel.cancel()
        if self.close_error:
            raise self.close_error


@pytest.fixture
def fixture(monkeypatch):
    made = []

    def factory(endpoint):
        conn = Connection(endpoint)
        made.append(conn)
        return conn

    monkeypatch.setattr(leaf, "AsyncWsSurrealConnection", factory)
    decisions = []

    async def authorize():
        decisions.append("actor")
        return leaf.NativeTransactionAuthorization(binding(), "actor", "decision")

    async def credentials(profile):
        decisions.append("credentials")
        return leaf.NativeCredentialProfile(profile, "root", "secret")

    return made, decisions, authorize, credentials


@pytest.mark.asyncio
async def test_native_transaction_authorization_catalog_and_explicit_commit(fixture):
    made, decisions, authorize, credentials = fixture
    async with leaf.open_native_transaction(
        binding(), authorize=authorize, credentials=credentials
    ) as tx:
        execute = tx.executor(binding().scopes[0])
        assert await execute("RETURN {value: 1};", org=ORG) == {"value": 1}
        with pytest.raises(dataclasses.FrozenInstanceError):
            execute.scope = None
        assert tx.commit_outcome == leaf.NativeCommitOutcome.NOT_REQUESTED
        await tx.commit()
        assert tx.commit_outcome == leaf.NativeCommitOutcome.ACKNOWLEDGED
        with pytest.raises(leaf.NativeTransactionError):
            await execute("RETURN true;")
    assert decisions == ["actor", "credentials"]
    assert made[0].closed
    assert [c[0] for c in made[0].calls] == [
        "connect",
        "signin",
        "begin",
        "query",
        "query",
        "query",
        "query",
        "commit",
        "close",
    ]
    assert made[0].calls[6][1].startswith("USE NS content_ns DB content; ")


@pytest.mark.asyncio
async def test_native_transaction_binding_denial_precedes_credentials_and_socket(fixture):
    made, decisions, _, credentials = fixture

    async def deny():
        decisions.append("actor")
        return leaf.NativeTransactionAuthorization(
            dataclasses.replace(binding(), provider_id="foreign"), "actor", "decision"
        )

    with pytest.raises(leaf.NativeTransactionError, match="authorization"):
        async with leaf.open_native_transaction(binding(), authorize=deny, credentials=credentials):
            pytest.fail("denied transaction yielded")
    assert made == [] and decisions == ["actor"]


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "memory://"},
        {"endpoint": "ws://root:secret@localhost/rpc"},
        {"endpoint": "ws://localhost/rpc?token=secret"},
        {"scopes": []},
        {"scopes": binding().scopes * 2},
    ],
)
def test_native_transaction_immutable_binding_rejects_unsupported_topology(change):
    with pytest.raises(ValueError):
        dataclasses.replace(binding(), **change)


@pytest.mark.parametrize(
    "query",
    [
        "USE NS foreign;",
        "BEGIN TRANSACTION; RETURN true;",
        "COMMIT;",
        "DEFINE TABLE bad;",
        "REMOVE TABLE raw_captures;",
    ],
)
@pytest.mark.asyncio
async def test_native_transaction_control_overrides_fail_before_query(fixture, query):
    made, _, authorize, credentials = fixture
    async with leaf.open_native_transaction(
        binding(), authorize=authorize, credentials=credentials
    ) as tx:
        conn = made[0]
        before = len(conn.calls)
        with pytest.raises(leaf.NativeTransactionError):
            await tx.executor(binding().scopes[0])(query)
        assert len(conn.calls) == before
        with pytest.raises(leaf.NativeTransactionError):
            await tx.commit()
    assert made[0].calls[-2][0] == "cancel"


@pytest.mark.parametrize(
    "response",
    [
        {"error": {"kind": "NotAllowed", "message": "denied"}},
        {
            "result": [
                {"status": "ERR", "kind": "Thrown", "result": "scope failed"},
                {"status": "OK", "result": True},
            ]
        },
        envelope(True),
        envelope(None, True, False),
        {"result": [{"status": "OK"}, {"status": "OK", "result": True}]},
    ],
)
@pytest.mark.asyncio
async def test_native_transaction_rejects_every_bad_frame_and_cardinality(fixture, response):
    made, _, authorize, credentials = fixture
    async with leaf.open_native_transaction(
        binding(), authorize=authorize, credentials=credentials
    ) as tx:
        made[0].response = response
        with pytest.raises((leaf.NativeTransactionError, SurrealError)):
            await tx.executor(binding().scopes[0])("RETURN true;")
        with pytest.raises(leaf.NativeTransactionError):
            await tx.commit()


@pytest.mark.asyncio
async def test_native_transaction_foreign_params_scope_and_socket(fixture):
    made, _, authorize, credentials = fixture
    async with leaf.open_native_transaction(
        binding(), authorize=authorize, credentials=credentials
    ) as tx:
        with pytest.raises(leaf.NativeTransactionError, match="approved"):
            tx.executor(dataclasses.replace(binding().scopes[0], namespace="foreign"))
        execute = tx.executor(binding().scopes[0])
        with pytest.raises(leaf.NativeTransactionError, match="organization"):
            await execute("RETURN true;", org=str(uuid4()))
    with pytest.raises(BaseExceptionGroup) as cleanup:
        async with leaf.open_native_transaction(
            binding(), authorize=authorize, credentials=credentials
        ) as tx:
            original_socket = made[-1].socket
            made[-1].socket = object()
            with pytest.raises(leaf.NativeTransactionError, match="affinity"):
                tx.executor(binding().scopes[0])
    assert "affinity" in str(cleanup.value.exceptions[0])
    assert original_socket.closed and not made[-1].closed
    assert "unbound replacement" in str(cleanup.value.exceptions[1])
    assert not any(call[0] == "cancel" for call in made[-1].calls)


@pytest.mark.parametrize(
    "cause, outcome",
    [
        (ConnectionError("response lost"), leaf.NativeCommitOutcome.UNKNOWN),
        (asyncio.CancelledError(), leaf.NativeCommitOutcome.UNKNOWN),
        (
            ThrownError("Thrown", "Transaction conflict: stale. This transaction can be retried"),
            leaf.NativeCommitOutcome.UNKNOWN,
        ),
        (InternalError("Internal", "unfamiliar engine failure"), leaf.NativeCommitOutcome.UNKNOWN),
        (
            QueryError(
                "Query", "native conflict", code=-32009, details={"kind": "TransactionConflict"}
            ),
            leaf.NativeCommitOutcome.REJECTED,
        ),
    ],
    ids=["transport", "cancelled", "thrown", "internal", "native-conflict"],
)
@pytest.mark.asyncio
async def test_native_transaction_commit_outcome_does_not_follow_cancel_ack(
    fixture, cause, outcome
):
    made, _, authorize, credentials = fixture
    tx = None
    with pytest.raises(type(cause)):
        async with leaf.open_native_transaction(
            binding(), authorize=authorize, credentials=credentials
        ) as tx:
            made[0].commit_error = cause
            await tx.commit()
    assert tx.commit_outcome == outcome
    assert made[0].closed
    if outcome != leaf.NativeCommitOutcome.REJECTED:
        assert made[0].calls[-2][0] == "cancel"
    else:
        assert not any(call[0] == "cancel" for call in made[0].calls)


@pytest.mark.asyncio
async def test_native_transaction_primary_cancel_and_close_errors_all_survive(fixture):
    made, _, authorize, credentials = fixture
    with pytest.raises(BaseExceptionGroup) as result:
        async with leaf.open_native_transaction(
            binding(), authorize=authorize, credentials=credentials
        ):
            made[0].cancel_error = ValueError("cancel failed")
            made[0].close_error = RuntimeError("close failed")
            raise asyncio.CancelledError("primary cancelled")
    assert [str(e) for e in result.value.exceptions] == [
        "primary cancelled",
        "cancel failed",
        "close failed",
    ]
    assert made[0].closed


@pytest.mark.asyncio
async def test_native_transaction_repeated_cancellation_finishes_cleanup(fixture):
    made, _, authorize, credentials = fixture
    ready = asyncio.Event()

    async def work():
        async with leaf.open_native_transaction(
            binding(), authorize=authorize, credentials=credentials
        ):
            made[0].close_release = asyncio.Event()
            ready.set()
            await asyncio.Future()

    task = asyncio.create_task(work())
    await ready.wait()
    task.cancel("first")
    await made[0].close_started.wait()
    task.cancel("second")
    await asyncio.sleep(0)
    assert not made[0].closed
    made[0].close_release.set()
    with pytest.raises(BaseExceptionGroup) as result:
        await task
    assert all(isinstance(e, asyncio.CancelledError) for e in result.value.exceptions)
    assert len(result.value.exceptions) == 2 and made[0].closed


@pytest.mark.parametrize(
    "query,count",
    [
        ("RETURN { LET $v='COMMIT; USE'; RETURN $v; };", 1),
        ("LET $v=1; RETURN $v;", 2),
        ("-- USE NS foreign;\nRETURN true;", 1),
        ("RETURN [1,2,{x:';'}];", 1),
    ],
)
def test_native_transaction_trusted_statement_frames_respect_blocks(query, count):
    frames, tokens = leaf._domain_frames(query)
    assert frames == count
    assert not leaf._CONTROLS.intersection(tokens)


@pytest.mark.asyncio
async def test_native_transaction_profile_denial_precedes_socket(fixture):
    made, decisions, authorize, _ = fixture

    async def wrong_profile(profile):
        decisions.append("credentials")
        return leaf.NativeCredentialProfile("foreign", "root", "secret")

    with pytest.raises(leaf.NativeTransactionError, match="profile"):
        async with leaf.open_native_transaction(
            binding(), authorize=authorize, credentials=wrong_profile
        ):
            pytest.fail("foreign profile yielded")
    assert made == [] and decisions == ["actor", "credentials"]


@pytest.mark.parametrize(
    "scope_result",
    [
        None,
        {"namespace": "foreign", "database": "content"},
        {"namespace": "content_ns"},
        {"namespace": "content_ns", "database": "content", "extra": True},
    ],
)
@pytest.mark.asyncio
async def test_native_transaction_scope_frame_must_match_exact_native_result(fixture, scope_result):
    made, _, authorize, credentials = fixture
    async with leaf.open_native_transaction(
        binding(), authorize=authorize, credentials=credentials
    ) as tx:
        made[0].response = envelope(scope_result, True)
        with pytest.raises(leaf.NativeTransactionError, match="USE result"):
            await tx.executor(binding().scopes[0])("RETURN true;")


@pytest.mark.asyncio
async def test_native_transaction_unrecognized_conflict_code_stays_unknown(fixture):
    made, _, authorize, credentials = fixture
    with pytest.raises(QueryError):
        async with leaf.open_native_transaction(
            binding(), authorize=authorize, credentials=credentials
        ) as tx:
            made[0].commit_error = QueryError(
                "Query", "looks like conflict", code=-32000, details={"kind": "TransactionConflict"}
            )
            await tx.commit()
    assert tx.commit_outcome == leaf.NativeCommitOutcome.UNKNOWN
    assert made[0].calls[-2][0] == "cancel"


@pytest.mark.asyncio
async def test_native_transaction_socket_close_error_survives_sdk_cleanup(fixture):
    made, _, authorize, credentials = fixture
    with pytest.raises(BaseExceptionGroup) as errors:
        async with leaf.open_native_transaction(
            binding(), authorize=authorize, credentials=credentials
        ):
            made[0].socket.close_error = ConnectionError("socket close failed")
    assert [str(error) for error in errors.value.exceptions] == ["socket close failed"]
    assert made[0].socket.closed and made[0].closed


@pytest.mark.asyncio
async def test_native_transaction_inflight_commit_rejects_and_teardown_drains(fixture):
    made, _, authorize, credentials = fixture
    query = None
    async with leaf.open_native_transaction(
        binding(), authorize=authorize, credentials=credentials
    ) as tx:
        conn = made[0]
        conn.pending_query = asyncio.get_running_loop().create_future()
        query = asyncio.create_task(tx.executor(binding().scopes[0])("RETURN true;"))
        await conn.query_started.wait()
        with pytest.raises(leaf.NativeTransactionError, match="finish before COMMIT"):
            await tx.commit()
        assert tx.commit_outcome == leaf.NativeCommitOutcome.NOT_REQUESTED
        assert not any(call[0] == "commit" for call in conn.calls)
    assert query.done() and tx._inflight == 0 and conn.closed
    with pytest.raises(asyncio.CancelledError):
        await query


@pytest.mark.parametrize("stage", ["connect", "signin", "begin"])
@pytest.mark.asyncio
async def test_native_transaction_startup_failure_closes_owned_socket(fixture, monkeypatch, stage):
    made, _, authorize, credentials = fixture

    def factory(endpoint):
        conn = Connection(endpoint)

        async def fail(*args, **kwargs):
            raise ConnectionError("startup " + stage)

        setattr(conn, stage, fail)
        made.append(conn)
        return conn

    monkeypatch.setattr(leaf, "AsyncWsSurrealConnection", factory)
    with pytest.raises(ConnectionError, match="startup"):
        async with leaf.open_native_transaction(
            binding(), authorize=authorize, credentials=credentials
        ):
            pytest.fail("failed startup yielded")
    assert made[0].socket.closed and made[0].closed
    assert not any(call[0] == "cancel" for call in made[0].calls)


@pytest.mark.parametrize("grace", [False, True, 0, -1, float("nan"), float("inf"), "5", None])
@pytest.mark.asyncio
async def test_native_transaction_invalid_cancel_grace_precedes_callbacks_and_io(fixture, grace):
    made, decisions, authorize, credentials = fixture
    with pytest.raises(ValueError, match="positive and finite"):
        async with leaf.open_native_transaction(
            binding(),
            authorize=authorize,
            credentials=credentials,
            cancel_ack_timeout_seconds=grace,
        ):
            pytest.fail("invalid host grace yielded")
    assert not made and not decisions


@pytest.mark.parametrize("first_cancellation", ["body", "deadline"])
@pytest.mark.asyncio
async def test_native_transaction_lost_cancel_deadline_retains_owner_and_drains(
    fixture, first_cancellation
):
    made, _, authorize, credentials = fixture
    ready = asyncio.Event()
    handles, queries = [], []

    async def work():
        async with leaf.open_native_transaction(
            binding(),
            authorize=authorize,
            credentials=credentials,
            cancel_ack_timeout_seconds=0.03,
        ) as tx:
            conn = made[0]
            handles.append(tx)
            conn.pending_cancel = asyncio.get_running_loop().create_future()
            conn.pending_query = asyncio.get_running_loop().create_future()
            queries.append(asyncio.create_task(tx.executor(binding().scopes[0])("RETURN true;")))
            await conn.query_started.wait()
            ready.set()
            if first_cancellation == "body":
                await asyncio.Future()

    owner = asyncio.create_task(work())
    await ready.wait()
    conn = made[0]
    if first_cancellation == "body":
        owner.cancel("first owner cancellation")
    await conn.cancel_started.wait()
    if first_cancellation == "deadline":
        owner.cancel("first owner cancellation")
    await asyncio.sleep(0)
    owner.cancel("second owner cancellation")
    with pytest.raises(BaseExceptionGroup) as errors:
        await asyncio.wait_for(owner, 1)
    flat = errors.value.exceptions
    assert sum(isinstance(e, TimeoutError) for e in flat) == 1
    owner_errors = [e for e in flat if str(e).endswith("owner cancellation")]
    assert [str(e) for e in owner_errors] == [
        "first owner cancellation",
        "second owner cancellation",
    ]
    owned = [e for e in flat if getattr(e, "__notes__", [])]
    assert len(owned) == 1 and "owned acknowledgment cleanup" in owned[0].__notes__[0]
    assert conn.socket.closed and conn.closed and handles[0]._inflight == 0
    assert handles[0].commit_outcome == leaf.NativeCommitOutcome.NOT_REQUESTED
    assert queries[0].done()
    with pytest.raises(asyncio.CancelledError):
        await queries[0]


@pytest.mark.asyncio
async def test_native_transaction_lost_cancel_deadline_preserves_unknown_commit(fixture):
    made, _, authorize, credentials = fixture
    with pytest.raises(BaseExceptionGroup) as errors:
        async with leaf.open_native_transaction(
            binding(),
            authorize=authorize,
            credentials=credentials,
            cancel_ack_timeout_seconds=0.01,
        ) as tx:
            made[0].pending_cancel = asyncio.get_running_loop().create_future()
            made[0].commit_error = ConnectionError("commit response lost")
            await tx.commit()
    assert isinstance(errors.value.exceptions[0], ConnectionError)
    assert any(isinstance(e, TimeoutError) for e in errors.value.exceptions)
    assert tx.commit_outcome == leaf.NativeCommitOutcome.UNKNOWN
    assert made[0].socket.closed and made[0].closed


@pytest.mark.asyncio
async def test_native_transaction_cancel_grace_does_not_limit_domain_rpc(fixture):
    made, _, authorize, credentials = fixture
    async with leaf.open_native_transaction(
        binding(),
        authorize=authorize,
        credentials=credentials,
        cancel_ack_timeout_seconds=0.01,
    ) as tx:
        conn = made[0]
        conn.pending_query = asyncio.get_running_loop().create_future()
        query = asyncio.create_task(tx.executor(binding().scopes[0])("RETURN true;"))
        await conn.query_started.wait()
        await asyncio.sleep(0.03)
        assert not query.done()
        conn.pending_query.set_result(None)
        assert await query == {"value": 1}
        await tx.commit()
    assert tx.commit_outcome == leaf.NativeCommitOutcome.ACKNOWLEDGED
    assert not any(c[0] == "cancel" for c in conn.calls)


@pytest.mark.parametrize("transport", ["replacement", "lost"])
@pytest.mark.asyncio
async def test_native_transaction_cancel_task_rechecks_socket_at_dispatch(monkeypatch, transport):
    conn = Connection(binding().endpoint)
    original = conn.socket
    replacement = Socket()
    tx = leaf.NativeTransaction(binding(), cancel_ack_timeout_seconds=1.0)
    tx._client, tx._socket, tx._txn = conn, original, conn.txn
    create_task = asyncio.create_task
    scheduled = []

    def change_socket_before_task_starts(coro, *args, **kwargs):
        if coro.cr_code.co_name in {"cancel", "_cancel_owned"}:
            scheduled.append(coro.cr_code.co_name)
            conn.socket = replacement if transport == "replacement" else None
        return create_task(coro, *args, **kwargs)

    monkeypatch.setattr(leaf.asyncio, "create_task", change_socket_before_task_starts)
    errors = await tx._cleanup()

    assert len(scheduled) == 1
    assert not any(call[0] == "cancel" for call in conn.calls)
    assert any(isinstance(error, leaf.NativeTransactionError) for error in errors)
    assert original.closed and not replacement.closed
    assert conn.closed == (transport == "lost")
    assert tx.commit_outcome == leaf.NativeCommitOutcome.NOT_REQUESTED
    assert tx._state == "closed"
