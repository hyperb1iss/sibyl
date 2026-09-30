from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from surrealdb.connections.async_embedded import AsyncEmbeddedSurrealConnection

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.query_guard import (
    check_native_query_request,
    guard_native_query_requests,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("file_backed", [False, True])
async def test_native_query_guard_receives_exact_sdk_query_before_dispatch(
    tmp_path, monkeypatch, file_backed
):
    native = SurrealContentClient(
        url="surrealkv://" + str(tmp_path / "owned.db") if file_backed else "memory://",
        namespace="guard_proof_" + uuid4().hex,
    )
    wire, guarded = [], []
    actual_send = AsyncEmbeddedSurrealConnection._send

    async def observed_send(self, message, *args, **kwargs):
        if "CREATE guard_probe" in message.kwargs.get("query", ""):
            wire.append(message.kwargs["query"])
        return await actual_send(self, message, *args, **kwargs)

    async def inspect_prepared_query(query, params):
        if "CREATE guard_probe" in query:
            guarded.append(query)
            assert params == {"body": "Synthetic owned evidence"}

    async def deny_prepared_query(query, _params):
        if "CREATE guard_probe" in query:
            raise ValueError("owned native request rejected")

    monkeypatch.setattr(AsyncEmbeddedSurrealConnection, "_send", observed_send)
    query = "CREATE guard_probe CONTENT {body:$body} RETURN NONE;"
    try:
        with guard_native_query_requests(inspect_prepared_query):
            await native.execute_query(query, body="Synthetic owned evidence")
        assert guarded == wire
        assert len(wire) == 1
        assert wire[0].startswith("USE NS ") is file_backed
        with (
            guard_native_query_requests(deny_prepared_query),
            pytest.raises(ValueError, match="owned native request rejected"),
        ):
            await native.execute_query(query, body="Rejected owned evidence")
        assert len(wire) == 1
        assert len(await native.execute_query("SELECT * FROM guard_probe;")) == 1
        # Scope exit restores ordinary clients, including after a rejected query.
        await native.execute_query(query, body="Another healthy owned row")
        assert len(await native.execute_query("SELECT * FROM guard_probe;")) == 2
    finally:
        try:
            await native.execute_query("REMOVE NAMESPACE " + native.namespace + ";")
        finally:
            await native.close()


async def test_native_query_guards_remain_private_to_concurrent_tasks():
    entered, arrivals = asyncio.Event(), 0
    observed = []

    async def own_operation(label):
        nonlocal arrivals

        async def own_guard(query, params):
            await asyncio.sleep(0)
            assert query == label
            assert params == {"actor": label}
            observed.append(label)

        with guard_native_query_requests(own_guard):
            arrivals += 1
            if arrivals == 2:
                entered.set()
            await asyncio.wait_for(entered.wait(), timeout=5)
            await check_native_query_request(label, {"actor": label})

    await asyncio.gather(own_operation("first"), own_operation("second"))
    assert sorted(observed) == ["first", "second"]
    await check_native_query_request("outside", None)
    assert sorted(observed) == ["first", "second"]


async def test_native_query_guard_restores_outer_operation_after_inner_failure():
    observed = []

    async def outer_guard(query, _params):
        observed.append(("outer", query))

    async def inner_guard(query, _params):
        observed.append(("inner", query))
        raise ValueError("owned inner rejection")

    with guard_native_query_requests(outer_guard):
        await check_native_query_request("first", None)
        with (
            pytest.raises(ValueError, match="owned inner rejection"),
            guard_native_query_requests(inner_guard),
        ):
            await check_native_query_request("second", None)
        await check_native_query_request("third", None)
    await check_native_query_request("outside", None)
    assert observed == [("outer", "first"), ("inner", "second"), ("outer", "third")]
