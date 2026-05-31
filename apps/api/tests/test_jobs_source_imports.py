from __future__ import annotations

import mailbox
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sibyl.jobs import source_imports
from sibyl.jobs.raw_promotion import promote_raw_captures
from sibyl.jobs.worker import WorkerSettings
from sibyl_core.models.sources import SourceRecord
from sibyl_core.services.mailbox_adapter import IMAP_ADAPTER_NAME
from sibyl_core.services.source_adapters import (
    SourceRawMemoryWrite,
    SourceRecordImportDecision,
    clear_source_adapters,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    clear_source_adapters()
    source_imports.clear_source_import_runs()
    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_dedupe_key",
            AsyncMock(return_value=None),
        ),
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
    ):
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


def _write_mbox(
    path: Path,
    *,
    body: str = "the job path imports raw mailbox records",
) -> Path:
    message = EmailMessage()
    message["Message-ID"] = "<job-msg@example.com>"
    message["Subject"] = "Job import"
    message["Date"] = "Thu, 14 May 2026 12:34:00 -0700"
    message["From"] = "Bliss <bliss@example.com>"
    message["To"] = "Nova <nova@example.com>"
    message.set_content(body)
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

    with patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path):
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
async def test_import_source_archive_surfaces_sensitive_policy(tmp_path: Path) -> None:
    mbox_path = _write_mbox(
        tmp_path / "sensitive-job.mbox",
        body="Rotate AWS key AKIAIOSFODNN7EXAMPLE before importing this archive",
    )
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=f"raw-{len(writes)}")

    with patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path):
        result = await source_imports.import_source_archive(
            {},
            str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            remember=fake_remember,
        )

    assert result["policy"]["privacy_class"] == "sensitive"
    assert result["sensitivity"]["contains_secret"] is True
    assert result["sensitivity"]["sensitivity_flags"] == ["api_key"]
    assert writes[0]["metadata"]["privacy_class"] == "sensitive"


@pytest.mark.asyncio
async def test_import_source_archive_resumes_from_checkpoint(tmp_path: Path) -> None:
    _write_resume_mbox(tmp_path / "resume.mbox")

    async def fake_remember(**kwargs: object) -> RawMemory:
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=str(kwargs["source_id"]))

    with patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path):
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


def _source_import_result_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "adapter_name": "mbox",
        "adapter_version": "1.0",
        "source_identity": "mailbox",
        "source_uri": "memory://mailbox",
        "source_version": "v1",
        "imported_count": 1,
        "skipped_count": 0,
        "dedupe_count": 0,
        "superseded_count": 0,
        "attachment_count": 0,
        "extraction_pending_count": 0,
        "raw_memory_ids": ["raw-new"],
        "source_ids": ["msg-1"],
        "dedupe_keys": ["dedupe-msg-1"],
        "duplicate_dedupe_keys": [],
        "skipped_records": [],
        "checkpoint": {"source_version": "v1", "done": True},
        "policy": {
            "privacy_class": "personal",
            "target_memory_scope": "private",
            "target_scope_key": None,
            "requires_promotion_preview": False,
            "reasons": ["privacy_default_scope"],
            "write_reason": "same_scope_write_allowed",
        },
        "sensitivity": {
            "contains_pii": False,
            "contains_secret": False,
            "contains_sensitive": False,
            "sensitivity_flags": [],
        },
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_duplicate_checker_looks_up_exact_dedupe_by_org_key() -> None:
    lookup_calls: list[dict[str, object]] = []

    async def capture_lookup(**kwargs: object) -> RawMemory | None:
        lookup_calls.append(dict(kwargs))
        return None

    checker = source_imports._default_duplicate_checker(
        organization_id="org-1",
        record_dedupe_keys={},
        record_source_ids={},
    )
    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_dedupe_key",
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
    assert lookup_calls[0]["dedupe_key"] == "dedupe-msg-1"
    assert lookup_calls[0]["principal_id"] is None
    assert lookup_calls[0]["memory_scope"] is MemoryScope.PROJECT
    assert lookup_calls[0]["scope_key"] == "project-7"


@pytest.mark.asyncio
async def test_duplicate_checker_filters_private_dedupe_by_principal() -> None:
    lookup_calls: list[dict[str, object]] = []

    async def capture_lookup(**kwargs: object) -> RawMemory | None:
        lookup_calls.append(dict(kwargs))
        return None

    checker = source_imports._default_duplicate_checker(
        organization_id="org-1",
        record_dedupe_keys={},
        record_source_ids={},
    )
    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_dedupe_key",
        AsyncMock(side_effect=capture_lookup),
    ):
        result = await checker(
            record=_source_record(),
            payload=_raw_memory_write(memory_scope=MemoryScope.PRIVATE),
        )

    assert result is None
    assert len(lookup_calls) == 1
    assert lookup_calls[0]["principal_id"] == "user-1"
    assert lookup_calls[0]["memory_scope"] is MemoryScope.PRIVATE
    assert lookup_calls[0]["scope_key"] is None


@pytest.mark.asyncio
async def test_duplicate_checker_prefers_in_run_dedupe_keys_over_db() -> None:
    async def fail_lookup(**kwargs: object) -> RawMemory | None:
        raise AssertionError("db lookup should be skipped for in-run duplicates")

    checker = source_imports._default_duplicate_checker(
        organization_id="org-1",
        record_dedupe_keys={"dedupe-msg-1": "raw-existing"},
        record_source_ids={},
    )
    with patch(
        "sibyl.jobs.source_imports.get_raw_memory_by_dedupe_key",
        AsyncMock(side_effect=fail_lookup),
    ):
        result = await checker(record=_source_record(), payload=_raw_memory_write())

    assert isinstance(result, SourceRecordImportDecision)
    assert result.duplicate_raw_memory_id == "raw-existing"
    assert result.superseded_raw_memory_id is None


@pytest.mark.asyncio
async def test_duplicate_checker_marks_same_source_changed_content_for_supersession() -> None:
    checker = source_imports._default_duplicate_checker(
        organization_id="org-1",
        record_dedupe_keys={},
        record_source_ids={},
    )
    existing = RawMemory(
        id="raw-old",
        organization_id="org-1",
        source_id="msg-1",
        principal_id="user-1",
        metadata={"content_hash": "hash-old"},
    )

    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_dedupe_key",
            AsyncMock(return_value=None),
        ),
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=existing),
        ),
    ):
        result = await checker(record=_source_record(), payload=_raw_memory_write())

    assert isinstance(result, SourceRecordImportDecision)
    assert result.duplicate_raw_memory_id is None
    assert result.superseded_raw_memory_id == "raw-old"


@pytest.mark.asyncio
async def test_supersession_handler_links_new_and_old_raw_memories() -> None:
    saved: list[RawMemory] = []
    old = RawMemory(
        id="raw-old",
        organization_id="org-1",
        source_id="msg-1",
        principal_id="user-1",
        metadata={"content_hash": "hash-old"},
    )
    new = RawMemory(
        id="raw-new",
        organization_id="org-1",
        source_id="msg-1",
        principal_id="user-1",
        metadata={"content_hash": "hash-new"},
    )

    async def capture_save(memory: RawMemory) -> RawMemory:
        saved.append(memory)
        return memory

    handler = source_imports._default_supersession_handler(organization_id="org-1")
    with (
        patch("sibyl.jobs.source_imports.get_raw_memory", AsyncMock(return_value=old)),
        patch("sibyl.jobs.source_imports.save_raw_memory", AsyncMock(side_effect=capture_save)),
    ):
        await handler(
            record=_source_record(),
            payload=_raw_memory_write(),
            memory=new,
            superseded_raw_memory_id="raw-old",
        )

    assert [memory.id for memory in saved] == ["raw-old", "raw-new"]
    assert old.review_state == "superseded"
    assert old.metadata["superseded_by_raw_memory_id"] == "raw-new"
    assert new.metadata["supersedes_raw_memory_id"] == "raw-old"


@pytest.mark.asyncio
async def test_import_source_archive_fails_closed_without_policy_context(tmp_path: Path) -> None:
    mbox_path = _write_mbox(tmp_path / "job.mbox")

    with (
        patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path),
        pytest.raises(ValueError, match="job_policy_context_missing"),
    ):
        await source_imports.import_source_archive(
            {},
            str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
        )


@pytest.mark.asyncio
async def test_import_source_archive_denies_paths_outside_import_root(tmp_path: Path) -> None:
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    outside_mbox = _write_mbox(tmp_path / "outside.mbox")

    with (
        patch("sibyl.jobs.source_imports.settings.source_import_dir", staged_dir),
        pytest.raises(PermissionError, match="source_import_path_denied"),
    ):
        await source_imports.import_source_archive(
            {},
            str(outside_mbox),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
        )


def test_document_url_import_bypasses_filesystem_root_resolution(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    url = "https://docs.example.com/page"

    with patch("sibyl.jobs.source_imports.settings.source_import_dir", import_root):
        resolved = source_imports._resolve_import_source_uri_for_adapter("document_url", url)

    assert resolved == url


def test_imap_import_bypasses_filesystem_root_resolution(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source_uri = "imaps://mail.example.com/INBOX"

    with patch("sibyl.jobs.source_imports.settings.source_import_dir", import_root):
        resolved = source_imports._resolve_import_source_uri_for_adapter(
            IMAP_ADAPTER_NAME,
            source_uri,
        )

    assert resolved == source_uri


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "option_name",
    [
        "access_token",
        "api_key",
        "client_secret",
        "oauth2_token",
        "password",
        "refresh_token",
        "token",
    ],
)
async def test_start_source_import_rejects_persisted_imap_secrets(option_name: str) -> None:
    with pytest.raises(
        ValueError,
        match="imap_credentials_not_allowed_in_source_import_options",
    ):
        await source_imports.start_source_import(
            source_uri="imaps://mail.example.com/INBOX",
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={"username": "bliss", option_name: "secret"},
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
async def test_start_source_import_rejects_imap_password_env_before_persist() -> None:
    with pytest.raises(
        ValueError,
        match="imap_credentials_not_allowed_in_source_import_options",
    ):
        await source_imports.start_source_import(
            source_uri="imaps://mail.example.com/INBOX",
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={"username": "bliss", "password_env": "AWS_SECRET_ACCESS_KEY"},
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
async def test_start_source_import_rejects_unknown_imap_options_before_persist() -> None:
    with pytest.raises(ValueError, match="imap_source_import_options_not_allowed"):
        await source_imports.start_source_import(
            source_uri="imaps://mail.example.com/INBOX",
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={"username": "bliss", "host": "other.example.com"},
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
async def test_start_source_import_rejects_imap_private_network_override() -> None:
    with pytest.raises(
        ValueError,
        match="imap_private_network_not_allowed_in_source_import_options",
    ):
        await source_imports.start_source_import(
            source_uri="imaps://mail.example.com/INBOX",
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={"username": "bliss", "allow_private_network": True},
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
async def test_start_source_import_rejects_imap_uri_password_before_persist() -> None:
    with pytest.raises(ValueError, match="imap_source_uri_must_not_include_password"):
        await source_imports.start_source_import(
            source_uri="imaps://bliss:secret@mail.example.com/INBOX",
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={"username": "bliss"},
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_uri",
    [
        "imaps://mail.example.com/INBOX?password=secret",
        "imaps://mail.example.com/INBOX?refresh_token=secret",
        "imaps://mail.example.com/INBOX#token=secret",
    ],
)
async def test_start_source_import_rejects_imap_query_before_persist(source_uri: str) -> None:
    with pytest.raises(ValueError, match="imap_source_uri_must_not_include_query_or_fragment"):
        await source_imports.start_source_import(
            source_uri=source_uri,
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={"username": "bliss"},
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
async def test_start_source_import_rejects_non_url_imap_source_before_persist() -> None:
    with pytest.raises(ValueError, match="imap_source_uri_must_use_tls"):
        await source_imports.start_source_import(
            source_uri="mail.example.com/INBOX?password=secret",
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={"username": "bliss"},
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
async def test_start_source_import_rejects_plaintext_imap_before_persist() -> None:
    with pytest.raises(ValueError, match="imap_source_uri_must_use_tls"):
        await source_imports.start_source_import(
            source_uri="imap://mail.example.com/INBOX",
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={"username": "bliss"},
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
async def test_start_source_import_rejects_tls_disabled_imap_before_persist() -> None:
    with pytest.raises(ValueError, match="imap_source_uri_must_use_tls"):
        await source_imports.start_source_import(
            source_uri="imaps://mail.example.com/INBOX",
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            adapter_name=IMAP_ADAPTER_NAME,
            options={
                "username": "bliss",
                "ssl": False,
            },
        )

    assert source_imports._SOURCE_IMPORT_RUNS == {}


@pytest.mark.asyncio
async def test_source_import_drain_resumes_from_persisted_checkpoint(
    tmp_path: Path,
) -> None:
    mbox_path = _write_resume_mbox(tmp_path / "resume.mbox")
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=f"raw-{len(writes)}")

    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
        patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
        )
        completed = await source_imports.drain_source_import(
            {},
            first["import_id"],
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
            remember=fake_remember,
        )

    assert first["status"] == "pending"
    assert first["checkpoint"] is None
    assert first["progress"]["imported_count"] == 0
    assert completed["status"] == "completed"
    assert completed["checkpoint"]["done"] is True
    assert completed["progress"]["imported_count"] == 2
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
        patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
        )
        completed = await source_imports.drain_source_import(
            {},
            first["import_id"],
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
            remember=fake_remember,
        )

    assert first["status"] == "pending"
    assert completed["status"] == "completed"
    assert statuses == ["pending", "running", "paused", "running", "completed"]


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
    assert set(payload) == {
        "import_id",
        "status",
        "progress",
        "created_at",
        "updated_at",
        "completed_at",
    }


@pytest.mark.asyncio
async def test_source_import_run_records_dedupe_without_duplicate_write(
    tmp_path: Path,
) -> None:
    mbox_path = _write_mbox(tmp_path / "dedupe.mbox")
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return _raw_memory_from_kwargs(dict(kwargs), raw_id="raw-existing")

    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
        patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
        )
        completed = await source_imports.drain_source_import(
            {},
            first["import_id"],
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
            remember=fake_remember,
        )
    assert completed["status"] == "completed"
    run = source_imports._SOURCE_IMPORT_RUNS[first["import_id"]]
    run.checkpoint = None
    run.status = source_imports.SourceImportStatus.PAUSED
    run.completed_at = None

    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
        patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path),
    ):
        second = await source_imports.drain_source_import(
            {},
            first["import_id"],
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
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

    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
        patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
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
async def test_resume_source_import_preserves_external_cancel_after_batch(
    tmp_path: Path,
) -> None:
    mbox_path = _write_mbox(tmp_path / "cancel-during-batch.mbox")

    with patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
        )

    canceled = source_imports.SourceImportRun(
        import_id=str(first["import_id"]),
        organization_id="org-1",
        principal_id="user-1",
        source_uri=str(mbox_path),
        adapter_name="mbox",
        options={},
        policy_context=_policy_context(),
        batch_size=1,
        promotion_preview_approved=False,
        status=source_imports.SourceImportStatus.CANCELED,
        completed_at=datetime.now(UTC),
    )

    async def load_canceled(import_id: str, *, organization_id: str):
        assert import_id == first["import_id"]
        assert organization_id == "org-1"
        source_imports._SOURCE_IMPORT_RUNS[import_id] = canceled
        return canceled

    with (
        patch(
            "sibyl.jobs.source_imports.import_source_archive",
            AsyncMock(return_value=_source_import_result_payload()),
        ),
        patch(
            "sibyl.jobs.source_imports._load_persisted_run",
            AsyncMock(side_effect=load_canceled),
        ),
    ):
        result = await source_imports.resume_source_import(
            str(first["import_id"]),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
        )

    assert result["status"] == "canceled"
    assert result["progress"]["imported_count"] == 0
    assert source_imports._SOURCE_IMPORT_RUNS[str(first["import_id"])].status == (
        source_imports.SourceImportStatus.CANCELED
    )


@pytest.mark.asyncio
async def test_resume_source_import_enqueues_raw_promotion_after_batch(tmp_path: Path) -> None:
    mbox_path = _write_mbox(tmp_path / "promote-after-batch.mbox")

    with patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
        )

    enqueue_raw_promotion = AsyncMock(return_value="raw_promotion:job")
    with (
        patch(
            "sibyl.jobs.source_imports.import_source_archive",
            AsyncMock(return_value=_source_import_result_payload(raw_memory_ids=["raw-new"])),
        ),
        patch("sibyl.jobs.queue.enqueue_raw_promotion", enqueue_raw_promotion),
    ):
        result = await source_imports.resume_source_import(
            str(first["import_id"]),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
        )

    assert result["status"] == "completed"
    enqueue_raw_promotion.assert_awaited_once_with(
        "org-1",
        raw_memory_ids=["raw-new"],
    )


@pytest.mark.asyncio
async def test_source_import_controls_are_principal_bound(tmp_path: Path) -> None:
    mbox_path = _write_resume_mbox(tmp_path / "principal.mbox")

    async def fake_remember(**kwargs: object) -> RawMemory:
        return _raw_memory_from_kwargs(dict(kwargs), raw_id=str(kwargs["source_id"]))

    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
        patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path),
    ):
        first = await source_imports.start_source_import(
            source_uri=str(mbox_path),
            organization_id="org-1",
            principal_id="user-1",
            policy_context=_policy_context(),
            batch_size=1,
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

    with (
        patch(
            "sibyl.jobs.source_imports.get_raw_memory_by_source_id",
            AsyncMock(return_value=None),
        ),
        patch("sibyl.jobs.source_imports.settings.source_import_dir", tmp_path),
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
    assert source_imports.drain_source_import in WorkerSettings.functions
    assert promote_raw_captures in WorkerSettings.functions
