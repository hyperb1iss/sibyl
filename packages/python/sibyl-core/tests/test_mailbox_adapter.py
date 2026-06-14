from __future__ import annotations

import mailbox
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from sibyl_core.models.sources import SourceImportCheckpoint, SourcePrivacyClass
from sibyl_core.services import mailbox_adapter as mailbox_adapter_module
from sibyl_core.services.mailbox_adapter import (
    IMAP_ADAPTER_NAME,
    MAILDIR_ADAPTER_NAME,
    MBOX_ADAPTER_NAME,
    ImapSourceAdapter,
    MaildirSourceAdapter,
    MboxSourceAdapter,
    _imap_config,
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


class FakeImapClient:
    def __init__(
        self,
        messages: dict[int, EmailMessage],
        *,
        fetch_failures: set[int] | None = None,
        search_uids: list[int] | None = None,
        uidvalidity: str = "123",
    ) -> None:
        self.messages = messages
        self.fetch_failures = fetch_failures or set()
        self.search_uids = search_uids
        self.uidvalidity = uidvalidity
        self.commands: list[tuple[str, tuple[object, ...]]] = []
        self.readonly_selects: list[bool] = []
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGIN", (user, password)))
        return "OK", [b"authenticated"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.commands.append(("SELECT", (mailbox, readonly)))
        self.readonly_selects.append(readonly)
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        self.commands.append(("RESPONSE", (code,)))
        return code, [self.uidvalidity.encode()]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.commands.append((f"UID {command}", args))
        if command == "SEARCH":
            start = int(str(args[-1]).split(":", 1)[0])
            if self.search_uids is not None:
                uids = [str(uid) for uid in self.search_uids]
            else:
                uids = [str(uid) for uid in sorted(self.messages) if uid >= start]
            return "OK", [" ".join(uids).encode()]
        if command == "FETCH":
            uid = int(str(args[0]))
            if uid in self.fetch_failures:
                return "NO", [b"message vanished"]
            return "OK", [(b"RFC822", self.messages[uid].as_bytes())]
        return "BAD", [b"unsupported"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGOUT", ()))
        self.logged_out = True
        return "BYE", [b"logout"]


class FakeImapServer:
    def __init__(
        self,
        messages: dict[int, EmailMessage],
        *,
        fetch_failures: set[int] | None = None,
        search_uids: list[int] | None = None,
        uidvalidity: str = "123",
    ) -> None:
        self.messages = messages
        self.fetch_failures = fetch_failures or set()
        self.search_uids = search_uids
        self.uidvalidity = uidvalidity
        self.clients: list[FakeImapClient] = []

    def factory(self, host: str, port: int, ssl: bool) -> FakeImapClient:
        client = FakeImapClient(
            self.messages,
            fetch_failures=self.fetch_failures,
            search_uids=self.search_uids,
            uidvalidity=self.uidvalidity,
        )
        client.commands.append(("CONNECT", (host, port, ssl)))
        self.clients.append(client)
        return client

    @property
    def commands(self) -> list[tuple[str, tuple[object, ...]]]:
        return [command for client in self.clients for command in client.commands]


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
async def test_mbox_adapter_resumes_without_fetching_prior_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    messages = [
        _message(message_id="msg-1@example.com", attachment=None),
        _message(message_id="msg-2@example.com", attachment=None),
        _message(message_id="msg-3@example.com", attachment=None),
    ]
    fetched_keys: list[int] = []
    mbox_path = tmp_path / "mail.mbox"
    mbox_path.write_text("", encoding="utf-8")

    class FakeMbox:
        def __init__(self, _path: Path, *, create: bool = False) -> None:
            assert create is False

        def iterkeys(self) -> Iterator[int]:
            return iter(range(len(messages)))

        def get_message(self, key: int) -> EmailMessage:
            fetched_keys.append(key)
            return messages[key]

        def close(self) -> None:
            pass

    monkeypatch.setattr(mailbox_adapter_module.mailbox, "mbox", FakeMbox)
    adapter = MboxSourceAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(mbox_path))
    checkpoint = SourceImportCheckpoint(
        cursor="2",
        source_version=manifest.source_version,
        done=False,
    )

    batch = await anext(adapter.iter_records(manifest, checkpoint=checkpoint, batch_size=1))

    assert fetched_keys == [2]
    assert [record.adapter_record_id for record in batch.records] == ["msg-3@example.com"]
    assert batch.checkpoint.cursor is None
    assert batch.checkpoint.done is True


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
async def test_maildir_adapter_resumes_without_fetching_prior_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    messages = {
        "a": _message(message_id="maildir-1@example.com", attachment=None),
        "b": _message(message_id="maildir-2@example.com", attachment=None),
        "c": _message(message_id="maildir-3@example.com", attachment=None),
    }
    fetched_keys: list[str] = []
    maildir_path = tmp_path / "maildir"
    for name in ("cur", "new", "tmp"):
        (maildir_path / name).mkdir(parents=True)

    class FakeMaildir:
        colon = ":"

        def __init__(self, _path: Path, *, create: bool = False) -> None:
            assert create is False

        def keys(self) -> list[str]:
            return list(messages)

        def get_message(self, key: str) -> EmailMessage:
            fetched_keys.append(key)
            return messages[key]

        def close(self) -> None:
            pass

    monkeypatch.setattr(mailbox_adapter_module.mailbox, "Maildir", FakeMaildir)
    adapter = MaildirSourceAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(maildir_path))
    checkpoint = SourceImportCheckpoint(
        cursor="2",
        source_version=manifest.source_version,
        done=False,
    )

    batch = await anext(adapter.iter_records(manifest, checkpoint=checkpoint, batch_size=1))

    assert fetched_keys == ["c"]
    assert [record.adapter_record_id for record in batch.records] == ["maildir-3@example.com"]
    assert batch.checkpoint.cursor is None
    assert batch.checkpoint.done is True


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


def test_imap_config_rejects_encoded_mailbox_control_characters() -> None:
    with pytest.raises(ValueError, match="control characters"):
        _imap_config(
            "imaps://127.0.0.1/INBOX%0D%0AA999%20SELECT%20Archive",
            {"allow_private_network": True},
        )


def test_imap_config_rejects_option_mailbox_control_characters() -> None:
    with pytest.raises(ValueError, match="control characters"):
        _imap_config(
            "imaps://127.0.0.1/INBOX",
            {"allow_private_network": True, "mailbox": "INBOX\r\nA999 SELECT Archive"},
        )


@pytest.mark.asyncio
async def test_imap_manifest_uses_uidvalidity_and_private_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SIBYL_SOURCE_IMPORT_IMAP_MAIL_EXAMPLE_COM_993_BLISS_PASSWORD",
        "secret",
    )
    server = FakeImapServer({101: _message()})
    adapter = ImapSourceAdapter(client_factory=server.factory)

    manifest = await adapter.prepare_manifest(
        source_uri="imaps://mail.example.com/INBOX",
        options={"username": "bliss"},
    )

    assert manifest.adapter_name == IMAP_ADAPTER_NAME
    assert manifest.adapter_version == "1.0"
    assert manifest.source_identity == "imap://bliss@mail.example.com:993/INBOX"
    assert manifest.source_uri == "imaps://bliss@mail.example.com:993/INBOX"
    assert manifest.source_version == "uidvalidity:123"
    assert manifest.target_memory_scope == "private"
    assert manifest.metadata["mailbox_format"] == "imap"
    assert manifest.metadata["message_count"] == 1
    assert manifest.metadata["uidvalidity"] == "123"
    assert "password" not in manifest.options
    assert "password_env" not in manifest.options
    assert all(select is True for client in server.clients for select in client.readonly_selects)


@pytest.mark.asyncio
async def test_imap_adapter_fetches_readonly_uid_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    server = FakeImapServer(
        {
            101: _message(message_id="imap-1@example.com", attachment=None),
            102: _message(message_id="imap-2@example.com", attachment=None),
        }
    )
    adapter = ImapSourceAdapter(client_factory=server.factory)
    manifest = await adapter.prepare_manifest(
        source_uri="imaps://mail.example.com/INBOX",
        options={"username": "bliss", "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD"},
    )

    first_batch = await anext(adapter.iter_records(manifest, batch_size=1))
    second_batch = await anext(
        adapter.iter_records(
            manifest,
            checkpoint=first_batch.checkpoint,
            batch_size=1,
        )
    )

    assert [record.adapter_record_id for record in first_batch.records] == ["imap-1@example.com"]
    assert first_batch.records[0].source_uri == f"{manifest.source_uri}#uid=101"
    assert first_batch.records[0].metadata["uid"] == "101"
    assert first_batch.checkpoint.cursor == "101"
    assert first_batch.checkpoint.done is False
    assert [record.adapter_record_id for record in second_batch.records] == ["imap-2@example.com"]
    assert second_batch.checkpoint.cursor == "102"
    assert second_batch.checkpoint.done is True
    assert all(select is True for client in server.clients for select in client.readonly_selects)
    commands = [command for command, _args in server.commands]
    assert "UID SEARCH" in commands
    assert "UID FETCH" in commands
    assert "STORE" not in commands
    assert "EXPUNGE" not in commands


@pytest.mark.asyncio
async def test_imap_uidvalidity_change_resets_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    server = FakeImapServer(
        {101: _message(message_id="imap-reset@example.com", attachment=None)},
        uidvalidity="456",
    )
    adapter = ImapSourceAdapter(client_factory=server.factory)
    manifest = await adapter.prepare_manifest(
        source_uri="imaps://mail.example.com/INBOX",
        options={"username": "bliss", "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD"},
    )
    old_checkpoint = SourceImportCheckpoint(
        cursor="999",
        source_version="uidvalidity:123",
        done=False,
    )

    batch = await anext(
        adapter.iter_records(
            manifest,
            checkpoint=old_checkpoint,
            batch_size=10,
        )
    )

    search_args = [args for command, args in server.commands if command == "UID SEARCH"]
    assert search_args[-1] == (None, "UID", "1:*")
    assert batch.checkpoint.metadata["uidvalidity_reset"] is True
    assert batch.records[0].metadata["uidvalidity"] == "456"


@pytest.mark.asyncio
async def test_imap_search_filters_stale_uids_after_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    server = FakeImapServer(
        {
            50: _message(message_id="stale@example.com", attachment=None),
            101: _message(message_id="fresh@example.com", attachment=None),
        },
        search_uids=[50, 101],
    )
    adapter = ImapSourceAdapter(client_factory=server.factory)
    manifest = await adapter.prepare_manifest(
        source_uri="imaps://mail.example.com/INBOX",
        options={"username": "bliss", "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD"},
    )
    checkpoint = SourceImportCheckpoint(
        cursor="100",
        source_version="uidvalidity:123",
        done=True,
    )

    batch = await anext(adapter.iter_records(manifest, checkpoint=checkpoint, batch_size=10))

    assert [record.adapter_record_id for record in batch.records] == ["fresh@example.com"]
    assert batch.checkpoint.cursor == "101"
    assert batch.checkpoint.done is True


@pytest.mark.asyncio
async def test_imap_fetch_failure_skips_uid_without_failing_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    server = FakeImapServer(
        {
            101: _message(message_id="ok@example.com", attachment=None),
            102: _message(message_id="vanished@example.com", attachment=None),
        },
        fetch_failures={102},
    )
    adapter = ImapSourceAdapter(client_factory=server.factory)
    manifest = await adapter.prepare_manifest(
        source_uri="imaps://mail.example.com/INBOX",
        options={"username": "bliss", "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD"},
    )

    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    assert [record.adapter_record_id for record in batch.records] == ["ok@example.com"]
    assert len(batch.skipped) == 1
    assert batch.skipped[0].adapter_record_id == "imap:102"
    assert batch.skipped[0].reason == "message_fetch_failed"
    assert batch.checkpoint.cursor == "102"
    assert batch.checkpoint.records_skipped == 1


@pytest.mark.asyncio
async def test_imap_manifest_rejects_private_hosts_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    server = FakeImapServer({101: _message()})
    adapter = ImapSourceAdapter(client_factory=server.factory)

    with pytest.raises(ValueError, match="IMAP host is private"):
        await adapter.prepare_manifest(
            source_uri="imaps://127.0.0.1/INBOX",
            options={
                "username": "bliss",
                "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD",
            },
        )

    assert server.clients == []


@pytest.mark.asyncio
async def test_imap_manifest_rejects_hosts_resolving_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    monkeypatch.setattr(
        "sibyl_core.services.mailbox_adapter._resolve_imap_host_addresses",
        lambda host: ["10.0.0.42"],
    )
    server = FakeImapServer({101: _message()})
    adapter = ImapSourceAdapter(client_factory=server.factory)

    with pytest.raises(ValueError, match="IMAP host is private"):
        await adapter.prepare_manifest(
            source_uri="imaps://mail.private.example/INBOX",
            options={
                "username": "bliss",
                "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD",
            },
        )

    assert server.clients == []


@pytest.mark.asyncio
async def test_imap_manifest_rejects_non_global_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    server = FakeImapServer({101: _message()})
    adapter = ImapSourceAdapter(client_factory=server.factory)

    with pytest.raises(ValueError, match="IMAP host is private"):
        await adapter.prepare_manifest(
            source_uri="imaps://100.64.0.1/INBOX",
            options={
                "username": "bliss",
                "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD",
            },
        )

    assert server.clients == []


@pytest.mark.asyncio
async def test_imap_manifest_pins_verified_connect_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    monkeypatch.setattr(
        "sibyl_core.services.mailbox_adapter._resolve_imap_host_addresses",
        lambda host: ["93.184.216.34"],
    )
    server = FakeImapServer({101: _message()})
    adapter = ImapSourceAdapter(client_factory=server.factory)
    adapter._pin_connect_host = True

    manifest = await adapter.prepare_manifest(
        source_uri="imaps://mail.example.com/INBOX",
        options={
            "username": "bliss",
            "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD",
        },
    )

    assert manifest.source_uri == "imaps://bliss@mail.example.com:993/INBOX"
    assert server.clients[0].commands[0] == ("CONNECT", ("93.184.216.34", 993, True))


@pytest.mark.asyncio
async def test_imap_manifest_rejects_untrusted_password_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    server = FakeImapServer({101: _message()})
    adapter = ImapSourceAdapter(client_factory=server.factory)

    with pytest.raises(ValueError, match="SIBYL_SOURCE_IMPORT_IMAP"):
        await adapter.prepare_manifest(
            source_uri="imaps://mail.example.com/INBOX",
            options={
                "username": "bliss",
                "password_env": "AWS_SECRET_ACCESS_KEY",
            },
        )

    assert server.clients == []


@pytest.mark.asyncio
async def test_imap_manifest_rejects_plaintext_imap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    server = FakeImapServer({101: _message()})
    adapter = ImapSourceAdapter(client_factory=server.factory)

    with pytest.raises(ValueError, match="IMAP imports require TLS"):
        await adapter.prepare_manifest(
            source_uri="imap://mail.example.com/INBOX",
            options={
                "username": "bliss",
                "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD",
            },
        )

    assert server.clients == []


@pytest.mark.asyncio
async def test_imap_manifest_allows_private_hosts_when_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    server = FakeImapServer({101: _message()})
    adapter = ImapSourceAdapter(client_factory=server.factory)

    manifest = await adapter.prepare_manifest(
        source_uri="imaps://127.0.0.1/INBOX",
        options={
            "allow_private_network": True,
            "username": "bliss",
            "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD",
        },
    )

    assert manifest.source_uri == "imaps://bliss@127.0.0.1:993/INBOX"
    assert server.clients[0].commands[0] == ("CONNECT", ("127.0.0.1", 993, True))


@pytest.mark.asyncio
async def test_imap_and_mbox_message_id_dedupe_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD", "secret")
    message = _message(message_id="same-message@example.com", attachment=None)
    mbox_path = _write_mbox(tmp_path / "mail.mbox", [message])
    mbox_adapter = MboxSourceAdapter()
    mbox_manifest = await mbox_adapter.prepare_manifest(source_uri=str(mbox_path))
    mbox_batch = await anext(mbox_adapter.iter_records(mbox_manifest, batch_size=10))

    server = FakeImapServer({101: message})
    imap_adapter = ImapSourceAdapter(client_factory=server.factory)
    imap_manifest = await imap_adapter.prepare_manifest(
        source_uri="imaps://mail.example.com/INBOX",
        options={"username": "bliss", "password_env": "SIBYL_SOURCE_IMPORT_IMAP_TEST_PASSWORD"},
    )
    imap_batch = await anext(imap_adapter.iter_records(imap_manifest, batch_size=10))

    assert imap_batch.records[0].dedupe_key == mbox_batch.records[0].dedupe_key
    assert imap_batch.records[0].source_id != mbox_batch.records[0].source_id


@pytest.mark.asyncio
async def test_gmail_labels_are_faceting_metadata(tmp_path: Path) -> None:
    message = _message(message_id="gmail-labels@example.com", attachment=None)
    message["X-Gmail-Labels"] = "Inbox, Important, Foo/Bar"
    mbox_path = _write_mbox(tmp_path / "mail.mbox", [message])
    adapter = MboxSourceAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(mbox_path))

    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    assert batch.records[0].metadata["gmail_labels"] == ["Inbox", "Important", "Foo/Bar"]
    assert batch.records[0].labels == ["mailbox", "email", "Inbox", "Important", "Foo/Bar"]


def test_ensure_mailbox_adapter_registers_once() -> None:
    ensure_mailbox_adapter_registered()
    ensure_mailbox_adapter_registered()

    imap_adapter = get_source_adapter(IMAP_ADAPTER_NAME)
    mbox_adapter = get_source_adapter(MBOX_ADAPTER_NAME)
    maildir_adapter = get_source_adapter(MAILDIR_ADAPTER_NAME)

    assert isinstance(imap_adapter, ImapSourceAdapter)
    assert isinstance(mbox_adapter, MboxSourceAdapter)
    assert isinstance(maildir_adapter, MaildirSourceAdapter)
