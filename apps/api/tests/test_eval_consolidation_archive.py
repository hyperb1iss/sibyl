"""A consolidation tombstone survives archive restoration without source text."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.services import content_client
from sibyl_core.services.eval_publication import ConsolidationOperation, get_stored_consolidation
from tests.test_memory_eval_admission import eval_api as eval_api  # noqa: PLC0414


async def test_archive_preserves_purged_consolidation_identity(eval_api, monkeypatch):
    api = eval_api
    operation = ConsolidationOperation(
        organization_id=api.assignment.organization_id,
        principal_id="owner",
        experiment_id="experiment",
        experiment_revision="1",
        arm_id="raw",
        checkpoint=0,
        group_id="contrast",
        attempt_ids=("a", "b"),
        mechanism="check output",
        controller_policy_sha256="c" * 64,
        extractor_revision="frozen-v1",
    )
    await api.store.execute_query(
        "CREATE eval_consolidations CONTENT $record;",
        record={
            "uuid": operation.key,
            "organization_id": operation.organization_id,
            "principal_id": operation.principal_id,
            "request_sha256": operation.request_sha256,
            "candidate_id": "already-purged",
            "result_kind": "candidate",
        },
    )
    monkeypatch.setattr(api.store, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: api.store)
    payload = await content_archive.export_content_archive_payload(operation.organization_id)
    assert len(payload["tables"]["eval_consolidations"]) == 1
    restored = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(restored, reset=True)
    close = restored.close
    monkeypatch.setattr(restored, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: restored)

    @asynccontextmanager
    async def session():
        yield restored

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    try:
        result = await content_archive.restore_content_archive_payload(payload)
        assert not result.errors
        assert (await get_stored_consolidation(operation)).status == "gone"
    finally:
        await close()
