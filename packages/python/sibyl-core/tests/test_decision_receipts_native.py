"""Optional server tests for overlapping shadow and source transactions.

Set SIBYL_TEST_SURREAL_URL to an isolated WebSocket server. Every test uses a
unique namespace; no production namespace or external model is accessed.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.services import decision_receipts as d
from tests.test_decision_receipts import invocation as invocation
from tests.test_decision_receipts import observation
from tests.test_source_support import case as case


@pytest.fixture
async def content_store(monkeypatch):
    url = os.getenv("SIBYL_TEST_SURREAL_URL", "")
    if not url.startswith(("ws://", "wss://")):
        pytest.skip("Requires independent native server transactions")
    client = SurrealContentClient(
        url=url,
        username=os.getenv("SIBYL_TEST_SURREAL_USERNAME", ""),
        password=os.getenv("SIBYL_TEST_SURREAL_PASSWORD", ""),
        namespace="jev_" + uuid4().hex,
        pool_size=8,
    )
    await bootstrap_content_schema(client, reset=True)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
    try:
        yield client
    finally:
        await client.close()


async def test_jev_native_delete_snapshot_before_new_receipt(invocation, content_store):
    _old, request, policy = invocation
    raw_id = request.source_refs[0].source.id
    deleting = asyncio.create_task(
        content_store.execute_query(
            "RETURN { LET $raw=(SELECT * FROM raw_captures WHERE uuid=$source); SLEEP 1s; "
            "DELETE raw_captures WHERE uuid=$source; RETURN {deleted:true}; };",
            source=raw_id,
        )
    )
    await asyncio.sleep(0.2)
    fresh_request = request.model_copy(update={"request_id": uuid4().hex})
    fresh = d.DecisionReceipt("org", "owner", authorize=AsyncMock(), dispatch_guard="LET $ok=true;")
    await fresh.begin(fresh_request, parent_id=raw_id, source_ids=[raw_id], policy=policy)
    await fresh.before_dispatch()
    assert await fresh.finish(observation(fresh_request))
    assert not deleting.done()
    # The dedicated client retries the transaction conflict with a new snapshot.
    # A successful deletion must include the receipt inserted during its first try.
    await deleting
    rows = await content_store.execute_query("SELECT * FROM semantic_decision_receipts;")
    assert len(rows) == 2
    assert all(row["purged"] and row.get("observation_json") is None for row in rows)


async def test_jev_native_delete_overlaps_completion(invocation, content_store):
    receipt, request, _ = invocation
    await receipt.before_dispatch()
    source = request.source_refs[0].source.id
    receipt.dispatch_guard = (
        "LET $snapshot=(SELECT * FROM raw_captures WHERE uuid=$native_source); SLEEP 1s;"
    )
    receipt.guard_params["native_source"] = source
    finishing = asyncio.create_task(receipt.finish(observation(request)))
    await asyncio.sleep(0.2)
    await content_store.execute_query("DELETE raw_captures WHERE uuid=$source;", source=source)
    assert not finishing.done()
    assert not await finishing
    row = (await content_store.execute_query("SELECT * FROM semantic_decision_receipts;"))[0]
    assert row["purged"] and row.get("observation_json") is None
    assert row.get("usage_json")


async def test_jev_native_shadow_creation_does_not_conflict_with_critic(invocation, content_store):
    _old, request, policy = invocation
    raw_id = request.source_refs[0].source.id
    fresh = d.DecisionReceipt(
        "org",
        "owner",
        authorize=AsyncMock(),
        dispatch_guard="LET $snapshot=(SELECT * FROM source_states WHERE organization_id='org'); SLEEP 1s;",
    )
    request = request.model_copy(update={"request_id": uuid4().hex})
    beginning = asyncio.create_task(
        fresh.begin(request, parent_id=raw_id, source_ids=[raw_id], policy=policy)
    )
    await asyncio.sleep(0.2)
    await content_store.execute_query(
        "UPDATE source_states SET validation_write_witness='critic-native-proof' "
        "WHERE organization_id='org' AND source_id=$source;",
        source=raw_id,
    )
    assert not beginning.done()
    assert await beginning == request.request_id
