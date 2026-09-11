"""Normal queries must surface failures anywhere in a statement batch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from surrealdb.errors import SurrealError

from sibyl_core.backends.surreal.dedicated_client import (
    DedicatedSurrealClient,
    _can_replay_query,
    _checked_query_result,
)


def test_query_response_preserves_first_result_and_ignores_data_error_shapes():
    payload = [{"status": "ERR", "result": "ordinary stored data"}]
    assert (
        _checked_query_result(
            {
                "result": [
                    {"status": "OK", "result": payload},
                    {"status": "OK", "result": "later result"},
                ]
            }
        )
        is payload
    )


def test_query_response_raises_causal_error_after_success_and_abort_markers():
    response = {
        "result": [
            {"status": "OK", "result": None},
            {
                "status": "ERR",
                "kind": "Query",
                "details": {"kind": "NotExecuted"},
                "result": "The query was not executed due to a failed transaction",
            },
            {"status": "ERR", "kind": "Thrown", "result": "ownership lost"},
            {
                "status": "ERR",
                "kind": "Query",
                "details": {"kind": "Cancelled"},
                "result": "The query was not executed due to a cancelled transaction",
            },
        ]
    }
    with pytest.raises(SurrealError, match="ownership lost") as raised:
        _checked_query_result(response)
    assert raised.value.kind == "Thrown"


def test_query_response_surfaces_rpc_error():
    with pytest.raises(SurrealError, match="denied"):
        _checked_query_result({"error": {"kind": "NotAllowed", "message": "denied"}})


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"result": []},
        {"result": [None]},
        {"result": [{"status": "OK"}]},
        {"result": [{"status": "unknown", "result": None}]},
    ],
)
def test_query_response_rejects_missing_or_malformed_results(response):
    with pytest.raises(SurrealError):
        _checked_query_result(response)


async def test_normal_query_checks_raw_response_once_and_raw_query_preserves_envelope():
    response = {
        "result": [
            {"status": "OK", "result": None},
            {"status": "ERR", "kind": "Thrown", "result": "rejected"},
        ]
    }
    transport = SimpleNamespace(query_raw=AsyncMock(return_value=response))
    client = DedicatedSurrealClient(
        url="memory://", username="", password="", namespace="response_contract", database="graph"
    )
    with pytest.raises(SurrealError, match="rejected"):
        await client._send_query(
            transport, "RETURN 1; THROW 'rejected';", params={"x": 1}, raw=False
        )
    transport.query_raw.assert_awaited_once_with("RETURN 1; THROW 'rejected';", {"x": 1})
    assert await client._send_query(transport, "RETURN 1;", params={}, raw=True) is response


async def test_embedded_transaction_failure_raises_and_rolls_back():
    client = DedicatedSurrealClient(
        url="memory://", username="", password="", namespace="response_rollback", database="graph"
    )
    try:
        await client.execute_query("DEFINE TABLE response_probe SCHEMALESS;")
        with pytest.raises(SurrealError, match="rejected"):
            await client.execute_query(
                "BEGIN TRANSACTION; CREATE response_probe:one; "
                "THROW 'rejected'; COMMIT TRANSACTION;"
            )
        assert await client.execute_query("SELECT * FROM response_probe;") == []
        await client.execute_query(
            "BEGIN TRANSACTION; CREATE response_probe:two; COMMIT TRANSACTION;"
        )
        assert len(await client.execute_query("SELECT * FROM response_probe;")) == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE counter:one SET value += 1; UPDATE other:one SET value += 1;",
        "SELECT * FROM fn::increment_counter(); SELECT * FROM fn::update_other();",
    ],
)
async def test_partial_batch_must_not_replay_successful_write(query):
    writes = 0

    async def query_raw(query, params):
        nonlocal writes
        if query == "RETURN true;":
            return {"result": [{"status": "OK", "result": True}]}
        writes += 1
        return {
            "result": [
                {"status": "OK", "result": [{"counter": writes}]},
                {
                    "status": "ERR",
                    "kind": "Query",
                    "result": "Transaction conflict: Resource busy. This transaction can be retried",
                }
                if writes == 1
                else {"status": "OK", "result": []},
            ]
        }

    client = DedicatedSurrealClient(
        url="memory://", username="", password="", namespace="retry_probe", database="graph"
    )
    client._pool[0].connect = AsyncMock(return_value=SimpleNamespace(query_raw=query_raw))
    with pytest.raises(SurrealError, match="Transaction conflict"):
        await client.execute_query(query)
    assert writes == 1


@pytest.mark.parametrize(
    ("query", "allowed"),
    [
        ("UPDATE counter:one SET value += 1;", True),
        ("SELECT * FROM counter; SELECT * FROM other;", False),
        ("BEGIN; UPDATE counter:one SET value += 1; COMMIT;", True),
        ("BEGIN TRANSACTION; LET $x = (UPDATE counter:one); COMMIT TRANSACTION;", True),
        ("UPDATE counter:one; UPDATE other:one;", False),
        ("BEGIN; UPDATE counter:one; COMMIT; UPDATE other:one;", False),
        ("BEGIN; UPDATE counter:one; COMMIT; BEGIN; UPDATE other:one; COMMIT;", False),
        ("BEGIN; UPDATE counter:one; CANCEL; COMMIT;", False),
    ],
)
def test_conflict_replay_requires_one_atomic_unit(query, allowed):
    assert _can_replay_query(query) is allowed


@pytest.mark.parametrize("commit_conflict", [False, True])
async def test_atomic_transaction_conflict_retries_rolled_back_batch(commit_conflict):
    attempts = 0

    async def query_raw(query, params):
        nonlocal attempts
        if query == "RETURN true;":
            return {"result": [{"status": "OK", "result": True}]}
        attempts += 1
        if attempts == 1:
            conflict = "Transaction conflict: Resource busy. This transaction can be retried"
            return {
                "result": [
                    {"status": "OK", "result": None},
                    {
                        "status": "ERR",
                        "kind": "Query",
                        "result": (
                            "The query was not executed due to a failed transaction"
                            if commit_conflict
                            else conflict
                        ),
                    },
                    {
                        "status": "ERR",
                        "kind": "Query",
                        "details": {"kind": "NotExecuted"},
                        "result": (
                            conflict
                            if commit_conflict
                            else "Cannot COMMIT: the transaction was aborted due to a prior error"
                        ),
                    },
                ]
            }
        return {
            "result": [
                {"status": "OK", "result": None},
                {"status": "OK", "result": [{"counter": 1}]},
            ]
        }

    client = DedicatedSurrealClient(
        url="memory://", username="", password="", namespace="atomic_retry", database="graph"
    )
    client._pool[0].connect = AsyncMock(return_value=SimpleNamespace(query_raw=query_raw))
    assert await client.execute_query("BEGIN; UPDATE counter:one SET value += 1; COMMIT;") is None
    assert attempts == 2


class ConnectionClosedError(RuntimeError):
    pass


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize(
    "query",
    ["SELECT * FROM fn::increment_counter();", "RETURN fn /* note */ ::increment_counter();"],
)
async def test_function_query_does_not_replay_after_lost_response(raw, query):
    writes = 0

    async def query_raw(query, params):
        nonlocal writes
        if query == "RETURN true;":
            return {"result": [{"status": "OK", "result": True}]}
        writes += 1
        if writes == 1:
            raise ConnectionClosedError("connection closed after commit")
        return {"result": [{"status": "OK", "result": [{"value": writes}]}]}

    client = DedicatedSurrealClient(
        url="memory://", username="", password="", namespace="reconnect_probe", database="graph"
    )
    client._pool[0].connect = AsyncMock(return_value=SimpleNamespace(query_raw=query_raw))
    with pytest.raises(ConnectionClosedError):
        await (client.execute_query_raw(query) if raw else client.execute_query(query))
    assert writes == 1


@pytest.mark.parametrize(
    "response",
    [
        {"error": {"kind": "NotAllowed", "message": "denied"}},
        {
            "result": [
                {"status": "OK", "result": []},
                {"status": "ERR", "kind": "Thrown", "result": "second seed failed"},
            ]
        },
        {"result": [{"status": "OK", "result": []}, None]},
    ],
)
async def test_checked_batch_rejects_rpc_and_later_statement_failures(response, monkeypatch):
    client = DedicatedSurrealClient(
        url="memory://",
        username="",
        password="",
        namespace="batch_error",
        database="graph",
    )
    transport = SimpleNamespace(query_raw=AsyncMock(return_value=response))
    monkeypatch.setattr(client._pool[0], "connect", AsyncMock(return_value=transport))
    try:
        with pytest.raises(SurrealError):
            await client.execute_query_batch("SELECT * FROM entity; SELECT * FROM entity;")
        transport.query_raw.assert_awaited_once()
    finally:
        await client.close()


async def test_checked_batch_returns_all_results_without_decoding_data_as_errors(monkeypatch):
    payload = [{"status": "ERR", "result": "ordinary stored data"}]
    response = {"result": [{"status": "OK", "result": []}, {"status": "OK", "result": payload}]}
    client = DedicatedSurrealClient(
        url="memory://",
        username="",
        password="",
        namespace="batch_success",
        database="graph",
    )
    transport = SimpleNamespace(query_raw=AsyncMock(return_value=response))
    monkeypatch.setattr(client._pool[0], "connect", AsyncMock(return_value=transport))
    try:
        assert await client.execute_query_batch("SELECT * FROM entity; SELECT * FROM entity;") == [
            [],
            payload,
        ]
        assert await client.execute_query("SELECT * FROM entity; SELECT * FROM entity;") == []
    finally:
        await client.close()
