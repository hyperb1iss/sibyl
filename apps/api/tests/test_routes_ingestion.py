from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.ingestion import (
    _document_collections_from_captures,
    _document_import_options,
    _resolve_route_import_source_uri,
    _resolve_route_import_source_uri_for_adapter,
    list_document_collections_route,
    list_import_adapters,
    resume_source_import_route,
    start_document_import_route,
    start_source_import_route,
)
from sibyl.api.schemas import (
    DocumentImportRequest,
    SourceImportResumeRequest,
    SourceImportStartRequest,
)
from sibyl.config import settings
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl_core.models.sources import (
    SourceAdapterCapability,
    SourceAdapterDescriptor,
    SourcePrivacyClass,
    SourceTransformBehavior,
)
from sibyl_core.services.mailbox_adapter import IMAP_ADAPTER_NAME
from sibyl_core.services.source_adapters import clear_source_adapters


def _source_import_payload(
    *,
    adapter_name: str = "mbox",
    target_memory_scope: str = "private",
    target_scope_key: str | None = None,
) -> dict[str, object]:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    return {
        "import_id": "source_import:run-1",
        "adapter_name": adapter_name,
        "adapter_version": None,
        "source_identity": None,
        "source_version": None,
        "status": "pending",
        "privacy_class": None,
        "target_memory_scope": target_memory_scope,
        "target_scope_key": target_scope_key,
        "checkpoint": None,
        "progress": {
            "imported_count": 0,
            "skipped_count": 0,
            "dedupe_count": 0,
            "superseded_count": 0,
            "error_count": 0,
            "attachment_count": 0,
            "extraction_pending_count": 0,
            "raw_memory_count": 0,
        },
        "raw_memory_ids": [],
        "dedupe_keys": [],
        "duplicate_dedupe_keys": [],
        "skipped_records": [],
        "errors": [],
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }


@pytest.mark.asyncio
async def test_list_import_adapters_returns_registered_contracts() -> None:
    descriptor = SourceAdapterDescriptor(
        name="mailbox",
        version="1.0",
        source_type="mailbox",
        display_name="Mailbox",
        capabilities=[SourceAdapterCapability.CHECKPOINTS],
        default_privacy_class=SourcePrivacyClass.PERSONAL,
        transform_behavior=SourceTransformBehavior.RAW,
        metadata_schema={"message_id": "string"},
        supports_incremental=True,
    )

    with patch("sibyl.api.routes.ingestion.list_source_adapters", return_value=[descriptor]):
        response = await list_import_adapters()

    assert len(response.adapters) == 1
    adapter = response.adapters[0]
    assert adapter.name == "mailbox"
    assert adapter.capabilities == ["checkpoints"]
    assert adapter.default_privacy_class == "personal"
    assert adapter.metadata_schema == {"message_id": "string"}
    assert adapter.supports_incremental is True


@pytest.mark.asyncio
async def test_list_import_adapters_includes_builtin_mailbox() -> None:
    clear_source_adapters()
    try:
        response = await list_import_adapters()
    finally:
        clear_source_adapters()

    names = {adapter.name for adapter in response.adapters}

    assert "mbox" in names
    assert "claude_code_jsonl" in names
    assert "codex_jsonl" in names
    assert "document_file" in names
    assert "document_folder" in names
    assert "document_url" in names
    assert "document_text" in names


def test_source_import_route_rejects_paths_outside_import_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    allowed = import_root / "mail.mbox"
    allowed.write_text("", encoding="utf-8")
    denied = tmp_path / "outside.mbox"
    denied.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "source_import_dir", import_root)

    assert _resolve_route_import_source_uri(str(allowed)) == str(allowed.resolve())
    with pytest.raises(HTTPException) as exc:
        _resolve_route_import_source_uri(str(denied))

    assert exc.value.status_code == 403
    assert exc.value.detail == "source_import_path_denied"


def test_source_import_route_allows_imap_uri() -> None:
    source_uri = "imaps://mail.example.com/INBOX"

    resolved = _resolve_route_import_source_uri_for_adapter(IMAP_ADAPTER_NAME, source_uri)

    assert resolved == source_uri


@pytest.mark.asyncio
async def test_start_source_import_route_enqueues_drain_without_inline_resume(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "mail.mbox"
    source_path.write_text("", encoding="utf-8")
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    payload = {
        "import_id": "source_import:run-1",
        "adapter_name": "mbox",
        "adapter_version": None,
        "source_identity": None,
        "source_version": None,
        "status": "pending",
        "privacy_class": None,
        "target_memory_scope": "private",
        "target_scope_key": None,
        "checkpoint": None,
        "progress": {
            "imported_count": 0,
            "skipped_count": 0,
            "dedupe_count": 0,
            "superseded_count": 0,
            "error_count": 0,
            "attachment_count": 0,
            "extraction_pending_count": 0,
            "raw_memory_count": 0,
        },
        "raw_memory_ids": [],
        "dedupe_keys": [],
        "duplicate_dedupe_keys": [],
        "skipped_records": [],
        "errors": [],
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    policy_context = {
        "actor_user_id": "user-1",
        "organization_id": "00000000-0000-0000-0000-000000000111",
        "memory_space": "private",
        "scope_key": None,
    }
    request = SourceImportStartRequest(source_uri=str(source_path), batch_size=25)
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")

    with (
        patch("sibyl.api.routes.ingestion.settings.source_import_dir", tmp_path),
        patch(
            "sibyl.api.routes.ingestion._source_import_policy_context",
            AsyncMock(return_value=policy_context),
        ),
        patch(
            "sibyl.api.routes.ingestion.start_source_import",
            AsyncMock(return_value=payload),
        ) as start,
        patch(
            "sibyl.jobs.queue.enqueue_source_import_drain",
            AsyncMock(return_value="source_import_drain:source_import:run-1"),
        ) as enqueue,
    ):
        response = await start_source_import_route(request, org=org, ctx=ctx)

    assert response.import_id == "source_import:run-1"
    assert response.status == "pending"
    start.assert_awaited_once()
    enqueue.assert_awaited_once_with(
        "source_import:run-1",
        organization_id=str(org.id),
        principal_id="user-1",
        policy_context=policy_context,
        batch_size=25,
        promotion_preview_approved=False,
    )


@pytest.mark.asyncio
async def test_start_source_import_route_passes_imap_uri_without_path_resolution() -> None:
    policy_context = {
        "actor_user_id": "user-1",
        "organization_id": "00000000-0000-0000-0000-000000000111",
        "memory_space": "private",
        "scope_key": None,
    }
    request = SourceImportStartRequest(
        source_uri="imaps://mail.example.com/INBOX",
        adapter_name=IMAP_ADAPTER_NAME,
        options={"username": "bliss"},
        batch_size=25,
    )
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")

    with (
        patch(
            "sibyl.api.routes.ingestion._source_import_policy_context",
            AsyncMock(return_value=policy_context),
        ),
        patch(
            "sibyl.api.routes.ingestion.start_source_import",
            AsyncMock(return_value=_source_import_payload(adapter_name=IMAP_ADAPTER_NAME)),
        ) as start,
        patch(
            "sibyl.jobs.queue.enqueue_source_import_drain",
            AsyncMock(return_value="source_import_drain:source_import:run-1"),
        ),
    ):
        response = await start_source_import_route(request, org=org, ctx=ctx)

    assert response.adapter_name == IMAP_ADAPTER_NAME
    start.assert_awaited_once()
    call = start.await_args.kwargs
    assert call["source_uri"] == "imaps://mail.example.com/INBOX"
    assert call["options"]["username"] == "bliss"


@pytest.mark.asyncio
async def test_start_document_import_route_enqueues_url_import() -> None:
    policy_context = {
        "actor_user_id": "user-1",
        "organization_id": "00000000-0000-0000-0000-000000000111",
        "memory_space": "project",
        "scope_key": "project_123",
    }
    request = DocumentImportRequest(
        kind="url",
        source_uri="https://docs.example.com/page",
        target_scope_key="project_123",
        collection="docs",
        batch_size=25,
    )
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")

    with (
        patch(
            "sibyl.api.routes.ingestion._source_import_policy_context",
            AsyncMock(return_value=policy_context),
        ),
        patch(
            "sibyl.api.routes.ingestion.start_source_import",
            AsyncMock(
                return_value=_source_import_payload(
                    adapter_name="document_url",
                    target_memory_scope="project",
                    target_scope_key="project_123",
                )
            ),
        ) as start,
        patch(
            "sibyl.jobs.queue.enqueue_source_import_drain",
            AsyncMock(return_value="source_import_drain:source_import:run-1"),
        ) as enqueue,
    ):
        response = await start_document_import_route(request, org=org, ctx=ctx)

    assert response.import_id == "source_import:run-1"
    start.assert_awaited_once_with(
        source_uri="https://docs.example.com/page",
        organization_id=str(org.id),
        principal_id="user-1",
        policy_context=policy_context,
        adapter_name="document_url",
        options={
            "target_memory_scope": "project",
            "target_scope_key": "project_123",
            "collection": "docs",
        },
        batch_size=25,
        promotion_preview_approved=False,
    )
    enqueue.assert_awaited_once_with(
        "source_import:run-1",
        organization_id=str(org.id),
        principal_id="user-1",
        policy_context=policy_context,
        batch_size=25,
        promotion_preview_approved=False,
    )


def test_document_import_request_ignores_private_network_bypass_option() -> None:
    request = DocumentImportRequest(
        kind="url",
        source_uri="https://docs.example.com/page",
        target_scope_key="project_123",
        allow_private_network=True,
    )

    assert not hasattr(request, "allow_private_network")
    assert _document_import_options(request) == {
        "target_memory_scope": "project",
        "target_scope_key": "project_123",
    }


@pytest.mark.asyncio
async def test_start_document_import_route_uses_text_payload_identity() -> None:
    policy_context = {
        "actor_user_id": "user-1",
        "organization_id": "00000000-0000-0000-0000-000000000111",
        "memory_space": "project",
        "scope_key": "project_123",
    }
    request = DocumentImportRequest(
        kind="text",
        text="Pinned launch notes",
        title="Launch notes",
        target_scope_key="project_123",
    )
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")

    with (
        patch(
            "sibyl.api.routes.ingestion._source_import_policy_context",
            AsyncMock(return_value=policy_context),
        ),
        patch(
            "sibyl.api.routes.ingestion.start_source_import",
            AsyncMock(
                return_value=_source_import_payload(
                    adapter_name="document_text",
                    target_memory_scope="project",
                    target_scope_key="project_123",
                )
            ),
        ) as start,
        patch("sibyl.jobs.queue.enqueue_source_import_drain", AsyncMock()),
    ):
        response = await start_document_import_route(request, org=org, ctx=ctx)

    assert response.adapter_name == "document_text"
    call = start.await_args.kwargs
    assert call["source_uri"].startswith("text://")
    assert call["adapter_name"] == "document_text"
    assert call["options"]["text"] == "Pinned launch notes"
    assert call["options"]["title"] == "Launch notes"


@pytest.mark.asyncio
async def test_resume_source_import_route_enqueues_drain_without_inline_batch() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    status_payload = {
        "import_id": "source_import:run-1",
        "adapter_name": "mbox",
        "adapter_version": "1.0",
        "source_identity": "mailbox",
        "source_version": "v1",
        "status": "paused",
        "privacy_class": "personal",
        "target_memory_scope": "private",
        "target_scope_key": None,
        "checkpoint": {"cursor": "100", "done": False},
        "progress": {
            "imported_count": 100,
            "skipped_count": 0,
            "dedupe_count": 0,
            "superseded_count": 0,
            "error_count": 0,
            "attachment_count": 0,
            "extraction_pending_count": 0,
            "raw_memory_count": 100,
        },
        "raw_memory_ids": [],
        "dedupe_keys": [],
        "duplicate_dedupe_keys": [],
        "skipped_records": [],
        "errors": [],
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    policy_context = {
        "actor_user_id": "user-1",
        "organization_id": "00000000-0000-0000-0000-000000000111",
        "memory_space": "private",
        "scope_key": None,
    }
    org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
    ctx = SimpleNamespace(user_id="user-1")

    with (
        patch(
            "sibyl.api.routes.ingestion.get_source_import_status",
            AsyncMock(return_value=status_payload),
        ),
        patch(
            "sibyl.api.routes.ingestion._source_import_policy_context",
            AsyncMock(return_value=policy_context),
        ),
        patch(
            "sibyl.jobs.queue.enqueue_source_import_drain",
            AsyncMock(return_value="source_import_drain:source_import:run-1"),
        ) as enqueue,
    ):
        response = await resume_source_import_route(
            "source_import:run-1",
            SourceImportResumeRequest(batch_size=50),
            org=org,
            ctx=ctx,
        )

    assert response.import_id == "source_import:run-1"
    assert response.status == "paused"
    enqueue.assert_awaited_once_with(
        "source_import:run-1",
        organization_id=str(org.id),
        principal_id="user-1",
        policy_context=policy_context,
        batch_size=50,
        promotion_preview_approved=None,
    )


@pytest.mark.asyncio
async def test_list_document_collections_route_returns_accessible_collections() -> None:
    org_id = UUID("00000000-0000-0000-0000-000000000111")
    visible = RawCaptureRecord(
        organization_id=org_id,
        title="Guide",
        raw_content="guide",
        entity_type="raw_memory",
        memory_scope="project",
        scope_key="project_123",
        capture_surface="source_import",
        metadata={
            "source_type": "document",
            "source_record_metadata": {"collection": "docs"},
        },
        captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )
    newer = RawCaptureRecord(
        organization_id=org_id,
        title="Guide 2",
        raw_content="guide",
        entity_type="raw_memory",
        memory_scope="project",
        scope_key="project_123",
        capture_surface="source_import",
        metadata={
            "source_type": "document",
            "source_record_metadata": {"collection": "docs"},
        },
        captured_at=datetime(2026, 5, 14, 13, 0, tzinfo=UTC),
    )
    hidden = RawCaptureRecord(
        organization_id=org_id,
        title="Private project guide",
        raw_content="guide",
        entity_type="raw_memory",
        memory_scope="project",
        scope_key="project_hidden",
        capture_surface="source_import",
        metadata={
            "source_type": "document",
            "source_record_metadata": {"collection": "hidden"},
        },
    )
    org = SimpleNamespace(id=org_id)
    ctx = SimpleNamespace(user_id="user-1")

    with (
        patch(
            "sibyl.api.routes.ingestion.list_accessible_project_graph_ids",
            AsyncMock(return_value={"project_123"}),
        ),
        patch(
            "sibyl.api.routes.ingestion._load_document_collection_captures",
            AsyncMock(return_value=[visible, newer, hidden]),
        ),
    ):
        response = await list_document_collections_route(org=org, ctx=ctx)

    assert [(item.name, item.document_count) for item in response.collections] == [("docs", 2)]
    assert response.collections[0].updated_at == newer.captured_at


def test_document_collections_from_captures_skips_non_documents() -> None:
    org_id = UUID("00000000-0000-0000-0000-000000000111")
    capture = RawCaptureRecord(
        organization_id=org_id,
        title="Mail",
        raw_content="mail",
        entity_type="raw_memory",
        memory_scope="project",
        scope_key="project_123",
        capture_surface="source_import",
        metadata={"source_type": "mailbox"},
    )

    assert (
        _document_collections_from_captures(
            [capture],
            accessible_project_ids={"project_123"},
        )
        == []
    )
