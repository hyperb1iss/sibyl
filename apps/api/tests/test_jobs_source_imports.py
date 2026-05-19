from __future__ import annotations

import mailbox
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sibyl.jobs import source_imports
from sibyl.jobs.worker import WorkerSettings
from sibyl_core.models.sources import SourceRecord
from sibyl_core.services.source_adapters import SourceRawMemoryWrite, clear_source_adapters
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    clear_source_adapters()
    source_imports.clear_source_import_runs()
    yield
    clear_source_adapters()
    source_imports.clear_source_import_runs()


def _policy_context(
    *,
    organization_id: str = "org-1",
    actor_user_id: str = "user-1",
    memory_space: str = "private",
    scope_key: str | None = None,
) -> dict[str, object]:
    return {
        "actor_user_id": actor_user_id,
        "organization_id": organization_id,
        "organization_role": "member",
        "accessible_projects": [scope_key] if memory_space == "project" and scope_key else [],
        "accessible_delegations": [],
        "memory_space": memory_space,
        "scope_key": scope_key,
        "source_surface": "source_import",
    }


def _raw_memory_from_kwargs(kwargs: dict[str, object], *, raw_id: str) -> RawMemory:
    return RawMemory(
        id=raw_id,
        organization_id=str(kwargs["organization_id"]),
        source_id=str(kwargs["source_id"]),
        principal_id=str(kwargs["principal_id"]),
        memory_scope=kwargs["memory_scope"],
        scope_key=kwargs["scope_key"],
        title=str(kwargs["title"]),
        raw_content=str(kwargs["raw_content"]),
        tags=list(kwargs["tags"]),
        metadata=dict(kwargs["metadata"]),
        provenance=dict(kwargs["provenance"]),
        capture_surface=str(kwargs["capture_surface"]),
        entity_type=str(kwargs["entity_type"]),
        captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )


def _write_mbox(path: Path) -> Path:
    message = EmailMessage()
    message["Message-ID"] = "<job-msg@example.com>"
    message["Subject"] = "Job import"
    message["Date"] = "Thu, 14 May 2026 12:34:00 -0700"
    message["From"] = "Bliss <bliss@example.com>"
    message["To"] = "Nova <nova@example.com>"
    message.set_content("the job path imports raw mailbox records")
    message.add_attachment(
        b"job attachment",
        maintype="text",
        subtype="plain",
        filename="job.txt",
    )

    box = mailbox.mbox(path)
    try:
        box.add(message)
        box.flush()
    finally:
        box.close()
    return path


def _write_resume_mbox(path: Path) -> Path:
    first = EmailMessage()
    first["Message-ID"] = "<first@example.com>"
    first["Subject"] = "First"
    first.set_content("first body")
    second = EmailMessage()
    second["Message-ID"] = "<second@example.com>"
    second["Subject"] = "Second"
    second.set_content("second body")
    box = mailbox.mbox(path)
    try:
        box.add(first)
        box.add(second)
        box.flush()
    finally:
        box.close()
    return path


@pytest.mark.asyncio
async def test_import_source_archive_imports_mbox_with_private_scope(tmp_path: Path) -> None:
    mbox_path = _write_mbox(tmp_path / "job.mbox")
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=f"raw-{len(writes)}")

    result = await source_imports.import_source_archive(
        {},
        str(mbox_path),
        organization_id="org-1",
        principal_id="user-1",
        policy_context=_policy_context(),
        remember=fake_remember,
    )

    assert result["adapter_name"] == "mbox"
    assert result["imported_count"] == 1
    assert result["skipped_count"] == 0
    assert result["dedupe_count"] == 0
    assert result["attachment_count"] == 1
    assert result["extraction_pending_count"] == 1
    assert result["checkpoint"]["done"] is True
    assert result["policy"]["target_memory_scope"] == "private"
    assert result["policy"]["requires_promotion_preview"] is False
    assert writes[0]["memory_scope"] is MemoryScope.PRIVATE
    assert result["raw_memory_ids"] == ["raw-1"]
    assert str(writes[0]["source_id"]).startswith("source-record:")
    source_metadata = writes[0]["metadata"]["source_record_metadata"]
    assert source_metadata["message_id"] == "job-msg@example.com"
    assert source_metadata["from"] == ["bliss@example.com"]
    assert writes[0]["metadata"]["attachment_count"] == 1


@pytest.mark.asyncio
async def test_import_source_archive_resumes_from_checkpoint(tmp_path: Path) -> None:
    _write_resume_mbox(tmp_path / "resume.mbox")

    async def fake_remember(**kwargs: object) -> RawMemory:
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=str(kwargs["source_id"]))

    first_result = await source_imports.import_source_archive(
        {},
        str(tmp_path / "resume.mbox"),
        organization_id="org-1",
        principal_id="user-1",
        policy_context=_policy_context(),
        batch_size=1,
        remember=fake_remember,
    )
    second_result = await source_imports.import_source_archive(
        {},
        str(tmp_path / "resume.mbox"),
        organization_id="org-1",
        principal_id="user-1",
        policy_context=_policy_context(),
        checkpoint=first_result["checkpoint"],
        batch_size=1,
        remember=fake_remember,
    )

    assert first_result["checkpoint"]["cursor"] == "1"
    assert first_result["checkpoint"]["done"] is False
    assert second_result["checkpoint"]["cursor"] is None
    assert second_result["checkpoint"]["done"] is True
    assert first_result["dedupe_keys"] != second_result["dedupe_keys"]


def _source_record(source_id: str = "msg-1") -> SourceRecord:
    return SourceRecord(
        adapter_record_id=source_id,
        source_id=source_id,
        source_type="mbox",
        content_hash=f"hash-{source_id}",
        dedupe_key=f"dedupe-{source_id}",
    )


def _raw_memory_write(
    *,
    source_id: str = "msg-1",
    principal_id: str = "user-1",
    memory_scope: MemoryScope = MemoryScope.PRIVATE,
    scope_key: str | None = None,
) -> SourceRawMemoryWrite:
    return SourceRawMemoryWrite(
        organization_id="org-1",
        principal_id=principal_id,
        source_id=source_id,
        raw_content="body",
        title="subject",
        memory_scope=memory_scope,
        scope_key=scope_key,
        tags=[],
        metadata={},
        provenance={},
    )


@pytest.mark.asyncio
async def test_duplicate_checker_scopes_db_lookup_to_principal_and_scope() -> None:
    lookup_calls: list[dict[str, object]] = []

    async def capture_lookup(**kwargs: object) -> RawMemory | None:
        lookup_calls.append(dict(kwargs))
        return None

    checker = source_imports._default_duplicate_checker(
        organization_id="org-1",
        record_source_ids={},
    )
    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
        AsyncMock(side_effect=capture_lookup),
    ):
        result = await checker(
            record=_source_record(),
            payload=_raw_memory_write(
                memory_scope=MemoryScope.PROJECT,
                scope_key="project-7",
            ),
        )

    assert result is None
    assert len(lookup_calls) == 1
    assert lookup_calls[0]["organization_id"] == "org-1"
    assert lookup_calls[0]["principal_id"] == "user-1"
    assert lookup_calls[0]["memory_scope"] is MemoryScope.PROJECT
    assert lookup_calls[0]["scope_key"] == "project-7"


@pytest.mark.asyncio
async def test_duplicate_checker_prefers_in_run_source_ids_over_db() -> None:
    async def fail_lookup(**kwargs: object) -> RawMemory | None:
        raise AssertionError("db lookup should be skipped for in-run duplicates")

    checker = source_imports._default_duplicate_checker(
        organization_id="org-1",
        record_source_ids={"msg-1": "raw-existing"},
    )
    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
        AsyncMock(side_effect=fail_lookup),
    ):
        result = await checker(record=_source_record(), payload=_raw_memory_write())

    assert result == "raw-existing"


@pytest.mark.asyncio
async def test_import_source_archive_fails_closed_without_policy_context(tmp_path: Path) -> None:
    mbox_path = _write_mbox(tmp_path / "job.mbox")

    with pytest.raises(ValueError, match="job_policy_context_missing"):
        await source_imports.import_source_archive(
            {},
            str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
        )


@pytest.mark.asyncio
async def test_source_import_run_resumes_from_persisted_checkpoint(
    tmp_path: Path,
) -> None:
    mbox_path = _write_resume_mbox(tmp_path / "resume.mbox")
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=f"raw-{len(writes)}")

    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
        AsyncMock(return_value=None),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
            remember=fake_remember,
        )
        second = await source_imports.resume_source_import(
            first["import_id"],
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            remember=fake_remember,
        )

    assert first["status"] == "paused"
    assert first["checkpoint"]["cursor"] == "1"
    assert first["progress"]["imported_count"] == 1
    assert second["status"] == "completed"
    assert second["checkpoint"]["done"] is True
    assert second["progress"]["imported_count"] == 2
    assert len(writes) == 2
    assert writes[0]["source_id"] != writes[1]["source_id"]


@pytest.mark.asyncio
async def test_source_import_run_broadcasts_status_changes(tmp_path: Path) -> None:
    mbox_path = _write_resume_mbox(tmp_path / "broadcast.mbox")
    statuses: list[str] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=str(kwargs["source_id"]))

    async def capture_status(run: source_imports.SourceImportRun) -> None:
        statuses.append(run.status.value)

    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "sibyl.jobs.source_imports._safe_broadcast_source_import",
            AsyncMock(side_effect=capture_status),
        ),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
            remember=fake_remember,
        )

    assert first["status"] == "paused"
    assert statuses == ["pending", "running", "paused"]


def test_source_import_event_payload_is_json_ready(tmp_path: Path) -> None:
    run = source_imports.SourceImportRun(
        import_id="source_import:test",
        organization_id="org-1",
        principal_id="user-1",
        source_uri=str(tmp_path / "source.mbox"),
        adapter_name="mbox",
        options={},
        policy_context=_policy_context(),
        batch_size=100,
        promotion_preview_approved=False,
    )

    payload = source_imports._source_import_event_payload(run)

    assert payload["import_id"] == "source_import:test"
    assert isinstance(payload["created_at"], str)
    assert isinstance(payload["updated_at"], str)


@pytest.mark.asyncio
async def test_source_import_run_records_dedupe_without_duplicate_write(
    tmp_path: Path,
) -> None:
    mbox_path = _write_mbox(tmp_path / "dedupe.mbox")
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return _raw_memory_from_kwargs(dict(kwargs), raw_id="raw-existing")

    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
        AsyncMock(return_value=None),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
            remember=fake_remember,
        )
    run = source_imports._SOURCE_IMPORT_RUNS[first["import_id"]]
    run.checkpoint = None
    run.status = source_imports.SourceImportStatus.PAUSED
    run.completed_at = None

    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
        AsyncMock(return_value=None),
    ):
        second = await source_imports.resume_source_import(
            first["import_id"],
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            remember=fake_remember,
        )

    assert len(writes) == 1
    assert second["progress"]["dedupe_count"] == 1
    assert second["progress"]["skipped_count"] == 1
    assert second["skipped_records"][0]["reason"] == "duplicate_dedupe_key"


@pytest.mark.asyncio
async def test_cancel_source_import_blocks_resume(tmp_path: Path) -> None:
    mbox_path = _write_resume_mbox(tmp_path / "cancel.mbox")

    async def fake_remember(**kwargs: object) -> RawMemory:
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=str(kwargs["source_id"]))

    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
        AsyncMock(return_value=None),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
            remember=fake_remember,
        )

    canceled = await source_imports.cancel_source_import(
        first["import_id"],
        organization_id="org-1",
        principal_id="user-1",
    )

    assert canceled["status"] == "canceled"
    with pytest.raises(ValueError, match="source_import_canceled"):
        await source_imports.resume_source_import(
            first["import_id"],
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            remember=fake_remember,
        )


@pytest.mark.asyncio
async def test_source_import_controls_are_principal_bound(tmp_path: Path) -> None:
    mbox_path = _write_resume_mbox(tmp_path / "principal.mbox")

    async def fake_remember(**kwargs: object) -> RawMemory:
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=str(kwargs["source_id"]))

    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
        AsyncMock(return_value=None),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
            remember=fake_remember,
        )

    with pytest.raises(PermissionError, match="source_import_forbidden"):
        await source_imports.resume_source_import(
            first["import_id"],
            organization_id="org-1",
            principal_id="user-2",
            policy_context=_policy_context(actor_user_id="user-2"),
            remember=fake_remember,
        )
    with pytest.raises(PermissionError, match="source_import_forbidden"):
        await source_imports.cancel_source_import(
            first["import_id"],
            organization_id="org-1",
            principal_id="user-2",
        )


@pytest.mark.asyncio
async def test_resume_source_import_rechecks_current_policy_context(tmp_path: Path) -> None:
    mbox_path = _write_resume_mbox(tmp_path / "project.mbox")

    async def fake_remember(**kwargs: object) -> RawMemory:
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=str(kwargs["source_id"]))

    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
        AsyncMock(return_value=None),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(memory_space="project", scope_key="project-1"),
            options={
                "target_memory_scope": "project",
                "target_scope_key": "project-1",
            },
            batch_size=1,
            promotion_preview_approved=True,
            remember=fake_remember,
        )

    stale_context = _policy_context(memory_space="project", scope_key="project-1")
    stale_context["accessible_projects"] = []
    with pytest.raises(ValueError, match="unverified_membership"):
        await source_imports.resume_source_import(
            first["import_id"],
            organization_id="org-1",
            principal_id="user-1",
            policy_context=stale_context,
            promotion_preview_approved=True,
            remember=fake_remember,
        )


def test_worker_settings_registers_source_import_job() -> None:
    assert source_imports.import_source_archive in WorkerSettings.functions
