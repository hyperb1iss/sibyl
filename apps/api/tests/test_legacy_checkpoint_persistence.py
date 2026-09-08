"""Archive and review writes retain storage-owned legacy content epochs."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl.persistence.surreal import content as api_content
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema


@pytest.fixture
async def checkpoint_client(monkeypatch):
    client = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(client, reset=True)

    @asynccontextmanager
    async def session():
        yield client

    monkeypatch.setattr(api_content, "surreal_content_client", session)
    try:
        yield client
    finally:
        await client.close()


async def test_checkpoint_archive_restore_after_bootstrap(checkpoint_client):
    history = [{"action": "revise", "reason": "legacy corrected text"}]
    record = {
        "id": str(uuid4()),
        "organization_id": str(uuid4()),
        "revision": 9,
        "raw_content": "restored evidence",
        "metadata": {"correction_history": history},
    }
    with (
        patch.object(
            content_archive, "build_surreal_content_client", return_value=checkpoint_client
        ),
        patch.object(checkpoint_client, "close", AsyncMock()),
    ):
        result = await content_archive.restore_content_archive_payload(
            {
                "tables": {"raw_captures": [record]},
            }
        )
    assert result.errors == []
    stored = (
        await api_content._select_many(
            checkpoint_client, "SELECT * FROM raw_captures WHERE uuid = $uuid;", uuid=record["id"]
        )
    )[0]
    assert stored["revision"] == 9
    assert stored["metadata"]["correction_history"] == history
    assert stored["legacy_content_checkpoint"] == {"entries": history, "observed_revision": 9}
    record["legacy_content_checkpoint"] = {"entries": history, "observed_revision": 7}
    with (
        patch.object(
            content_archive, "build_surreal_content_client", return_value=checkpoint_client
        ),
        patch.object(checkpoint_client, "close", AsyncMock()),
    ):
        result = await content_archive.restore_content_archive_payload(
            {
                "tables": {"raw_captures": [record]},
            }
        )
    assert result.errors == []
    stored = (
        await api_content._select_many(
            checkpoint_client, "SELECT * FROM raw_captures WHERE uuid = $uuid;", uuid=record["id"]
        )
    )[0]
    assert stored["legacy_content_checkpoint"]["observed_revision"] == 7


async def test_checkpoint_stale_review_preserves_concurrent_history(checkpoint_client, monkeypatch):
    org = uuid4()
    initial_history = [{"action": "revise", "reason": "imported capture"}]
    capture = await api_content.save_raw_capture_record(
        None,
        capture=RawCaptureRecord(
            organization_id=org,
            principal_id="owner",
            title="Evidence",
            raw_content="before",
            entity_type="raw_memory",
            metadata={"correction_history": initial_history},
        ),
    )
    select_one = api_content._select_one
    raced = False
    history = [*initial_history, {"action": "revise", "reason": "concurrent correction"}]
    initial = (
        await api_content._select_many(
            checkpoint_client,
            "SELECT * FROM raw_captures WHERE uuid = $uuid;",
            uuid=str(capture.id),
        )
    )[0]
    assert initial["revision"] == 1
    assert initial["legacy_content_checkpoint"]["observed_revision"] == 1

    async def read_then_correct(client, query, **params):
        nonlocal raced
        result = await select_one(client, query, **params)
        if not raced and params.get("capture_id") == str(capture.id):
            raced = True
            await client.execute_query(
                "UPDATE raw_captures MERGE $patch WHERE uuid = $uuid;",
                uuid=str(capture.id),
                patch={
                    "revision": result["revision"] + 1,
                    "raw_content": "after",
                    "metadata": {**result["metadata"], "correction_history": history},
                },
            )
        return result

    monkeypatch.setattr(api_content, "_select_one", read_then_correct)
    reviewed = await api_content.update_raw_capture_review_state(
        None,
        organization_id=org,
        capture_id=capture.id,
        review_state="promoted",
    )
    assert reviewed is not None
    stored = (
        await api_content._select_many(
            checkpoint_client,
            "SELECT * FROM raw_captures WHERE uuid = $uuid;",
            uuid=str(capture.id),
        )
    )[0]
    assert raced
    assert stored["revision"] == 3
    assert stored["raw_content"] == "after"
    assert stored["metadata"]["correction_history"] == history
    assert stored["review_state"] == "promoted"
    assert stored["legacy_content_checkpoint"]["observed_revision"] == 2
