from __future__ import annotations

import mailbox
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from sibyl_core.models.sources import SourcePrivacyClass
from sibyl_core.services.mailbox_adapter import (
    MAILDIR_ADAPTER_NAME,
    MBOX_ADAPTER_NAME,
    MaildirSourceAdapter,
    MboxSourceAdapter,
    ensure_mailbox_adapter_registered,
)
from sibyl_core.services.source_adapters import (
    clear_source_adapters,
    get_source_adapter,
    import_source_batch,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    clear_source_adapters()
    yield
    clear_source_adapters()


def _write_mbox(path: Path, messages: list[EmailMessage]) -> Path:
    box = mailbox.mbox(path)
    try:
        for message in messages:
            box.add(message)
        box.flush()
    finally:
        box.close()
    return path


def _write_maildir(path: Path, messages: list[EmailMessage]) -> Path:
    box = mailbox.Maildir(path, create=True)
    try:
        for message in messages:
            box.add(message)
        box.flush()
    finally:
        box.close()
    return path


def _message(
    *,
    message_id: str = "msg-1@example.com",
    subject: str = "Source Adapter Notes",
    body: str = "mailbox import should stay source-preserving",
    date: str = "Thu, 14 May 2026 12:34:00 -0700",
    from_addr: str = "Bliss <bliss@example.com>",
    to_addr: str = "Nova <nova@example.com>",
    references: str | None = None,
    attachment: bytes | None = b"remember the attachments",
) -> EmailMessage:
    message = EmailMessage()
    message["Message-ID"] = f"<{message_id}>"
    message["Subject"] = subject
    message["Date"] = date
    message["From"] = from_addr
    message["To"] = to_addr
    if references:
        message["References"] = references
        message["In-Reply-To"] = references.split()[-1]
    message.set_content(body)
    if attachment is not None:
        message.add_attachment(
            attachment,
            maintype="text",
            subtype="plain",
            filename="notes.txt",
        )
    return message


@pytest.mark.asyncio
async def test_mbox_manifest_defaults_to_private_memory(tmp_path: Path) -> None:
    mbox_path = _write_mbox(tmp_path / "mail.mbox", [_message()])
    adapter = MboxSourceAdapter()

    manifest = await adapter.prepare_manifest(source_uri=str(mbox_path))

    assert manifest.adapter_name == "mbox"
    assert manifest.adapter_version == "1.0"
    assert manifest.source_identity == str(mbox_path.resolve())
    assert manifest.source_uri == str(mbox_path.resolve())
    assert manifest.source_version.startswith("mtime:")
    assert manifest.target_memory_scope == "private"
    assert manifest.privacy_class is SourcePrivacyClass.PERSONAL
    assert manifest.metadata["mailbox_format"] == "mbox"
    assert manifest.metadata_schema["message_id"] == "string"


@pytest.mark.asyncio
async def test_mbox_adapter_preserves_message_metadata(tmp_path: Path) -> None:
    root_id = "<thread-root@example.com>"
    mbox_path = _write_mbox(
        tmp_path / "mail.mbox",
        [_message(message_id="reply@example.com", references=root_id)],
    )
    adapter = MboxSourceAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(mbox_path))

    batches = [
        batch
        async for batch in adapter.iter_records(
            manifest,
            batch_size=10,
        )
    ]

    assert len(batches) == 1
    batch = batches[0]
    assert batch.checkpoint.done is True
    assert batch.skipped == []
    record = batch.records[0]
    assert record.adapter_record_id == "reply@example.com"
    assert record.source_type == "mailbox_message"
    assert record.source_uri == f"{manifest.source_uri}#message=0"
    assert record.title == "Source Adapter Notes"
    assert "source-preserving" in record.body
    assert record.dedupe_key.startswith("source:")
    assert record.occurred_at == datetime(2026, 5, 14, 19, 34, tzinfo=UTC)
    assert record.participants == ["bliss@example.com", "nova@example.com"]
    assert record.labels == ["mailbox", "email"]
    assert record.metadata["message_id"] == "reply@example.com"
    assert record.metadata["thread_id"] == "thread-root@example.com"
    assert record.metadata["references"] == ["thread-root@example.com"]
    assert record.metadata["source_path"] == manifest.source_uri
    assert record.attachments[0].filename == "notes.txt"
    assert record.attachments[0].media_type == "text/plain"
    assert record.attachments[0].size_bytes == len(b"remember the attachments")
    assert record.attachments[0].source_path == f"{record.source_uri}&part=2"


@pytest.mark.asyncio
async def test_mbox_adapter_resumes_from_checkpoint(tmp_path: Path) -> None:
    mbox_path = _write_mbox(
        tmp_path / "mail.mbox",
        [
            _message(message_id="msg-1@example.com", attachment=None),
            _message(message_id="msg-2@example.com", attachment=None),
            _message(message_id="msg-3@example.com", attachment=None),
        ],
    )
    adapter = MboxSourceAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(mbox_path))
    first_batch = await anext(
        adapter.iter_records(
            manifest,
            batch_size=2,
        )
    )

    assert [record.adapter_record_id for record in first_batch.records] == [
        "msg-1@example.com",
        "msg-2@example.com",
    ]
    assert first_batch.checkpoint.cursor == "2"
    assert first_batch.checkpoint.done is False

    second_batch = await anext(
        adapter.iter_records(
            manifest,
            checkpoint=first_batch.checkpoint,
            batch_size=2,
        )
    )

    assert [record.adapter_record_id for record in second_batch.records] == [
        "msg-3@example.com",
    ]
    assert second_batch.checkpoint.cursor is None
    assert second_batch.checkpoint.done is True


@pytest.mark.asyncio
async def test_mbox_import_writes_private_source_records(tmp_path: Path) -> None:
    mbox_path = _write_mbox(tmp_path / "mail.mbox", [_message()])
    adapter = MboxSourceAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(mbox_path))
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return RawMemory(
            id=f"raw-{len(writes)}",
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

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=fake_remember,
    )

    assert result.imported_count == 1
    assert result.skipped_count == 0
    assert result.checkpoint is not None
    assert result.checkpoint.done is True
    assert writes[0]["memory_scope"] is MemoryScope.PRIVATE
    assert writes[0]["capture_surface"] == "source_import"
    source_metadata = writes[0]["metadata"]["source_record_metadata"]
    assert source_metadata["message_id"] == "msg-1@example.com"
    assert writes[0]["metadata"]["attachment_count"] == 1


@pytest.mark.asyncio
async def test_maildir_manifest_defaults_to_private_memory(tmp_path: Path) -> None:
    maildir_path = _write_maildir(tmp_path / "maildir", [_message()])
    adapter = MaildirSourceAdapter()

    manifest = await adapter.prepare_manifest(source_uri=str(maildir_path))

    assert manifest.adapter_name == "maildir"
    assert manifest.adapter_version == "1.0"
    assert manifest.source_identity == str(maildir_path.resolve())
    assert manifest.source_uri == str(maildir_path.resolve())
    assert manifest.source_version.startswith("entries:1:mtime:")
    assert manifest.target_memory_scope == "private"
    assert manifest.privacy_class is SourcePrivacyClass.PERSONAL
    assert manifest.metadata["mailbox_format"] == "maildir"
    assert manifest.metadata["message_count"] == 1
    assert manifest.metadata_schema["mailbox_key"] == "string"


@pytest.mark.asyncio
async def test_maildir_adapter_preserves_metadata_and_resumes(tmp_path: Path) -> None:
    maildir_path = _write_maildir(
        tmp_path / "maildir",
        [
            _message(message_id="maildir-1@example.com", attachment=None),
            _message(message_id="maildir-2@example.com", attachment=None),
        ],
    )
    adapter = MaildirSourceAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(maildir_path))
    first_batch = await anext(
        adapter.iter_records(
            manifest,
            batch_size=1,
        )
    )

    assert len(first_batch.records) == 1
    assert first_batch.checkpoint.cursor == "1"
    assert first_batch.checkpoint.done is False
    first_record = first_batch.records[0]
    assert first_record.adapter_record_id in {
        "maildir-1@example.com",
        "maildir-2@example.com",
    }
    assert first_record.source_uri is not None
    assert first_record.source_uri.startswith(f"{manifest.source_uri}#message=0&key=")
    assert first_record.metadata["mailbox_format"] == "maildir"
    assert first_record.metadata["mailbox_key"]

    second_batch = await anext(
        adapter.iter_records(
            manifest,
            checkpoint=first_batch.checkpoint,
            batch_size=2,
        )
    )

    assert len(second_batch.records) == 1
    assert second_batch.checkpoint.cursor is None
    assert second_batch.checkpoint.done is True
    assert {
        first_batch.records[0].adapter_record_id,
        second_batch.records[0].adapter_record_id,
    } == {"maildir-1@example.com", "maildir-2@example.com"}


@pytest.mark.asyncio
async def test_maildir_manifest_rejects_symlinked_maildir_subdirectories(
    tmp_path: Path,
) -> None:
    source = _write_maildir(tmp_path / "maildir", [_message()])
    cur_dir = source / "cur"
    shadow = tmp_path / "outside-cur"
    shadow.mkdir()
    cur_dir.rename(source / "cur.real")
    cur_dir.symlink_to(shadow, target_is_directory=True)
    adapter = MaildirSourceAdapter()

    with pytest.raises(ValueError, match="symlinked directories"):
        await adapter.prepare_manifest(source_uri=str(source))


@pytest.mark.asyncio
async def test_maildir_manifest_ignores_symlinked_entries(tmp_path: Path) -> None:
    source = _write_maildir(
        tmp_path / "maildir",
        [_message(message_id="maildir-1@example.com", attachment=None)],
    )
    outside_message = tmp_path / "outside.eml"
    outside_message.write_text(
        "From: attacker@example.com\nSubject: outside\n\nsecret\n",
        encoding="utf-8",
    )
    (source / "cur" / "leak:2,").symlink_to(outside_message)
    adapter = MaildirSourceAdapter()

    manifest = await adapter.prepare_manifest(source_uri=str(source))

    assert manifest.metadata["message_count"] == 1
    assert manifest.source_version.startswith("entries:1:mtime:")


@pytest.mark.asyncio
async def test_maildir_iter_records_skips_symlinked_entries(tmp_path: Path) -> None:
    source = _write_maildir(
        tmp_path / "maildir",
        [_message(message_id="maildir-1@example.com", attachment=None)],
    )
    outside_message = tmp_path / "outside.eml"
    outside_message.write_text(
        "From: attacker@example.com\nSubject: outside\n\nsecret host data\n",
        encoding="utf-8",
    )
    (source / "cur" / "leak:2,").symlink_to(outside_message)
    adapter = MaildirSourceAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(source))

    batches = [batch async for batch in adapter.iter_records(manifest, batch_size=10)]

    ingested = [record for batch in batches for record in batch.records]
    assert len(ingested) == 1
    assert ingested[0].adapter_record_id == "maildir-1@example.com"
    assert all("secret host data" not in record.body for record in ingested)


def test_ensure_mailbox_adapter_registers_once() -> None:
    ensure_mailbox_adapter_registered()
    ensure_mailbox_adapter_registered()

    mbox_adapter = get_source_adapter(MBOX_ADAPTER_NAME)
    maildir_adapter = get_source_adapter(MAILDIR_ADAPTER_NAME)

    assert isinstance(mbox_adapter, MboxSourceAdapter)
    assert isinstance(maildir_adapter, MaildirSourceAdapter)
