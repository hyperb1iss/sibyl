"""Mailbox source adapters for raw-memory imports."""

from __future__ import annotations

import asyncio
import imaplib
import inspect
import mailbox
import os
import socket
import ssl as ssl_module
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote, unquote, urlparse

from sibyl_core.models.sources import (
    SourceAdapterCapability,
    SourceAdapterDescriptor,
    SourceAttachmentRecord,
    SourceImportCheckpoint,
    SourceImportManifest,
    SourcePrivacyClass,
    SourceRecord,
    SourceRecordBatch,
    SourceSkippedRecord,
    SourceTransformBehavior,
)
from sibyl_core.services.source_adapters import (
    build_source_content_hash,
    build_source_dedupe_key,
    build_source_record_id,
    register_source_adapter,
    source_adapter_registry,
)

MBOX_ADAPTER_NAME = "mbox"
MBOX_ADAPTER_VERSION = "1.0"
MAILDIR_ADAPTER_NAME = "maildir"
MAILDIR_ADAPTER_VERSION = "1.0"
IMAP_ADAPTER_NAME = "imap"
IMAP_ADAPTER_VERSION = "1.0"
_EMAIL_DEDUPE_IDENTITY = "email_message"
_IMAP_SECRET_OPTION_KEYS = frozenset({"access_token", "oauth2_token", "password"})
_IMAP_PASSWORD_ENV_PREFIX = "SIBYL_SOURCE_IMPORT_IMAP_"


class ImapClient(Protocol):
    def login(self, user: str, password: str) -> object: ...

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> object: ...

    def response(self, code: str) -> object: ...

    def uid(self, command: str, *args: object) -> object: ...

    def logout(self) -> object: ...


type ImapClientFactory = Callable[..., ImapClient]


class MboxSourceAdapter:
    """Source adapter for local MBOX archives."""

    descriptor = SourceAdapterDescriptor(
        name=MBOX_ADAPTER_NAME,
        version=MBOX_ADAPTER_VERSION,
        source_type="mailbox",
        display_name="MBOX mailbox archive",
        capabilities=[
            SourceAdapterCapability.ATTACHMENTS,
            SourceAdapterCapability.CHECKPOINTS,
            SourceAdapterCapability.INCREMENTAL,
            SourceAdapterCapability.SKIPPED_RECORDS,
        ],
        default_privacy_class=SourcePrivacyClass.PERSONAL,
        transform_behavior=SourceTransformBehavior.RAW,
        metadata_schema={
            "message_id": "string",
            "thread_id": "string",
            "in_reply_to": "string",
            "references": "string[]",
            "from": "string[]",
            "to": "string[]",
            "cc": "string[]",
            "bcc": "string[]",
            "subject": "string",
            "source_path": "string",
        },
        supports_incremental=True,
    )

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        path = _resolve_mbox_path(source_uri)
        stat = path.stat()
        option_values = dict(options or {})
        target_memory_scope = str(option_values.get("target_memory_scope") or "private")
        target_scope_key = _optional_str(option_values.get("target_scope_key"))
        privacy_class = SourcePrivacyClass(
            str(option_values.get("privacy_class") or self.descriptor.default_privacy_class)
        )
        source_identity = str(option_values.get("source_identity") or path)

        return SourceImportManifest(
            adapter_name=self.descriptor.name,
            adapter_version=self.descriptor.version,
            source_identity=source_identity,
            source_uri=str(path),
            source_version=f"mtime:{stat.st_mtime_ns}:size:{stat.st_size}",
            target_memory_scope=target_memory_scope,
            target_scope_key=target_scope_key,
            privacy_class=privacy_class,
            transform_behavior=self.descriptor.transform_behavior,
            metadata_schema=dict(self.descriptor.metadata_schema),
            metadata={
                "mailbox_format": "mbox",
                "source_path": str(path),
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            },
            options=option_values,
        )

    async def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        """Yield one bounded batch so callers can persist each checkpoint."""
        if not manifest.source_uri:
            msg = "MBOX imports require manifest.source_uri"
            raise ValueError(msg)

        path = _resolve_mbox_path(manifest.source_uri)
        start = int(checkpoint.cursor) if checkpoint and checkpoint.cursor else 0
        batch_records: list[SourceRecord] = []
        skipped: list[SourceSkippedRecord] = []
        cursor = start
        total_seen = start

        mbox = mailbox.mbox(path, create=False)
        try:
            message_count = len(mbox)
            for index, message in enumerate(mbox.itervalues()):
                if index < start:
                    continue
                if len(batch_records) >= batch_size:
                    break
                cursor = index + 1
                total_seen = cursor
                try:
                    batch_records.append(_record_from_message(manifest, message, index=index))
                except Exception as exc:
                    skipped.append(
                        SourceSkippedRecord(
                            adapter_record_id=f"mbox:{index}",
                            source_uri=_message_source_uri(manifest, index),
                            reason="message_parse_failed",
                            metadata={"error": str(exc)},
                        )
                    )

            done = cursor >= message_count
            if batch_records or skipped or start < message_count:
                yield SourceRecordBatch(
                    records=batch_records,
                    skipped=skipped,
                    checkpoint=SourceImportCheckpoint(
                        cursor=str(cursor) if not done else None,
                        source_version=manifest.source_version,
                        records_seen=total_seen,
                        records_imported=len(batch_records),
                        records_skipped=len(skipped),
                        done=done,
                        metadata={"source_uri": manifest.source_uri},
                    ),
                )
        finally:
            mbox.close()


class MaildirSourceAdapter:
    """Source adapter for local Maildir archives."""

    descriptor = SourceAdapterDescriptor(
        name=MAILDIR_ADAPTER_NAME,
        version=MAILDIR_ADAPTER_VERSION,
        source_type="mailbox",
        display_name="Maildir mailbox archive",
        capabilities=[
            SourceAdapterCapability.ATTACHMENTS,
            SourceAdapterCapability.CHECKPOINTS,
            SourceAdapterCapability.INCREMENTAL,
            SourceAdapterCapability.SKIPPED_RECORDS,
        ],
        default_privacy_class=SourcePrivacyClass.PERSONAL,
        transform_behavior=SourceTransformBehavior.RAW,
        metadata_schema={
            "message_id": "string",
            "thread_id": "string",
            "in_reply_to": "string",
            "references": "string[]",
            "from": "string[]",
            "to": "string[]",
            "cc": "string[]",
            "bcc": "string[]",
            "subject": "string",
            "source_path": "string",
            "mailbox_key": "string",
        },
        supports_incremental=True,
    )

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        path = _resolve_maildir_path(source_uri)
        option_values = dict(options or {})
        target_memory_scope = str(option_values.get("target_memory_scope") or "private")
        target_scope_key = _optional_str(option_values.get("target_scope_key"))
        privacy_class = SourcePrivacyClass(
            str(option_values.get("privacy_class") or self.descriptor.default_privacy_class)
        )
        source_identity = str(option_values.get("source_identity") or path)
        entries = _maildir_entries(path)

        return SourceImportManifest(
            adapter_name=self.descriptor.name,
            adapter_version=self.descriptor.version,
            source_identity=source_identity,
            source_uri=str(path),
            source_version=_maildir_source_version(entries),
            target_memory_scope=target_memory_scope,
            target_scope_key=target_scope_key,
            privacy_class=privacy_class,
            transform_behavior=self.descriptor.transform_behavior,
            metadata_schema=dict(self.descriptor.metadata_schema),
            metadata={
                "mailbox_format": "maildir",
                "source_path": str(path),
                "message_count": len(entries),
            },
            options=option_values,
        )

    async def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        if not manifest.source_uri:
            msg = "Maildir imports require manifest.source_uri"
            raise ValueError(msg)

        path = _resolve_maildir_path(manifest.source_uri)
        start = int(checkpoint.cursor) if checkpoint and checkpoint.cursor else 0
        batch_records: list[SourceRecord] = []
        skipped: list[SourceSkippedRecord] = []
        cursor = start
        total_seen = start

        maildir = mailbox.Maildir(path, create=False)
        try:
            symlinked_keys = _maildir_symlinked_keys(path, maildir.colon)
            # iterating the mailbox yields messages, not keys, so .keys() is required
            keys = sorted(
                key
                for key in maildir.keys()  # noqa: SIM118
                if key not in symlinked_keys
            )
            message_count = len(keys)
            for index, key in enumerate(keys):
                if index < start:
                    continue
                if len(batch_records) >= batch_size:
                    break
                cursor = index + 1
                total_seen = cursor
                source_uri = _message_source_uri(manifest, index, mailbox_key=key)
                try:
                    message = maildir.get_message(key)
                    batch_records.append(
                        _record_from_message(
                            manifest,
                            message,
                            index=index,
                            source_uri=source_uri,
                            extra_metadata={
                                "mailbox_format": "maildir",
                                "mailbox_key": key,
                            },
                        )
                    )
                except Exception as exc:
                    skipped.append(
                        SourceSkippedRecord(
                            adapter_record_id=f"maildir:{key}",
                            source_uri=source_uri,
                            reason="message_parse_failed",
                            metadata={"error": str(exc), "mailbox_key": key},
                        )
                    )

            done = cursor >= message_count
            if batch_records or skipped or start < message_count:
                yield SourceRecordBatch(
                    records=batch_records,
                    skipped=skipped,
                    checkpoint=SourceImportCheckpoint(
                        cursor=str(cursor) if not done else None,
                        source_version=manifest.source_version,
                        records_seen=total_seen,
                        records_imported=len(batch_records),
                        records_skipped=len(skipped),
                        done=done,
                        metadata={"source_uri": manifest.source_uri},
                    ),
                )
        finally:
            maildir.close()


class ImapSourceAdapter:
    """Read-only source adapter for IMAP mailboxes."""

    descriptor = SourceAdapterDescriptor(
        name=IMAP_ADAPTER_NAME,
        version=IMAP_ADAPTER_VERSION,
        source_type="mailbox",
        display_name="IMAP mailbox",
        capabilities=[
            SourceAdapterCapability.ATTACHMENTS,
            SourceAdapterCapability.CHECKPOINTS,
            SourceAdapterCapability.INCREMENTAL,
            SourceAdapterCapability.SKIPPED_RECORDS,
        ],
        default_privacy_class=SourcePrivacyClass.PERSONAL,
        transform_behavior=SourceTransformBehavior.RAW,
        metadata_schema={
            "message_id": "string",
            "thread_id": "string",
            "in_reply_to": "string",
            "references": "string[]",
            "from": "string[]",
            "to": "string[]",
            "cc": "string[]",
            "bcc": "string[]",
            "subject": "string",
            "uid": "string",
            "uidvalidity": "string",
            "mailbox": "string",
            "gmail_labels": "string[]",
        },
        supports_incremental=True,
    )

    def __init__(self, client_factory: ImapClientFactory | None = None) -> None:
        self._client_factory = client_factory or _default_imap_client_factory
        self._pin_connect_host = client_factory is None

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        option_values = dict(options or {})
        config = _imap_config(
            source_uri,
            option_values,
            pin_connect_host=self._pin_connect_host,
        )
        target_memory_scope = str(option_values.get("target_memory_scope") or "private")
        target_scope_key = _optional_str(option_values.get("target_scope_key"))
        privacy_class = SourcePrivacyClass(
            str(option_values.get("privacy_class") or self.descriptor.default_privacy_class)
        )
        uidvalidity, message_count = await self._select_readonly_metadata(config)

        return SourceImportManifest(
            adapter_name=self.descriptor.name,
            adapter_version=self.descriptor.version,
            source_identity=config["source_identity"],
            source_uri=_imap_source_uri(config),
            source_version=f"uidvalidity:{uidvalidity}",
            target_memory_scope=target_memory_scope,
            target_scope_key=target_scope_key,
            privacy_class=privacy_class,
            transform_behavior=self.descriptor.transform_behavior,
            metadata_schema=dict(self.descriptor.metadata_schema),
            metadata={
                "mailbox_format": "imap",
                "host": config["host"],
                "port": config["port"],
                "ssl": config["ssl"],
                "mailbox": config["mailbox"],
                "username": config["username"],
                "message_count": message_count,
                "uidvalidity": uidvalidity,
            },
            options=_public_imap_options(option_values),
        )

    async def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        if not manifest.source_uri:
            msg = "IMAP imports require manifest.source_uri"
            raise ValueError(msg)

        config = _imap_config(
            manifest.source_uri,
            manifest.options,
            pin_connect_host=self._pin_connect_host,
        )
        uidvalidity, _message_count = await self._select_readonly_metadata(config)
        source_version = f"uidvalidity:{uidvalidity}"
        checkpoint_matches = checkpoint is not None and checkpoint.source_version == source_version
        start_uid = int(checkpoint.cursor) + 1 if checkpoint_matches and checkpoint.cursor else 1
        uids = await self._search_uids(config, start_uid=start_uid)
        batch_uids = uids[:batch_size]
        batch_records: list[SourceRecord] = []
        skipped: list[SourceSkippedRecord] = []

        fetched_messages, fetch_errors = await self._fetch_messages(config, uids=batch_uids)
        for uid in batch_uids:
            source_uri = _imap_message_source_uri(manifest, uid)
            fetch_error = fetch_errors.get(uid)
            if fetch_error is not None:
                skipped.append(
                    SourceSkippedRecord(
                        adapter_record_id=f"imap:{uid}",
                        source_uri=source_uri,
                        reason="message_fetch_failed",
                        metadata={
                            "error": str(fetch_error),
                            "uid": str(uid),
                            "uidvalidity": uidvalidity,
                        },
                    )
                )
                continue
            try:
                message = fetched_messages[uid]
                batch_records.append(
                    _record_from_message(
                        manifest,
                        message,
                        index=uid,
                        source_uri=source_uri,
                        extra_metadata={
                            "mailbox_format": "imap",
                            "mailbox": config["mailbox"],
                            "uid": str(uid),
                            "uidvalidity": uidvalidity,
                        },
                    )
                )
            except Exception as exc:
                skipped.append(
                    SourceSkippedRecord(
                        adapter_record_id=f"imap:{uid}",
                        source_uri=source_uri,
                        reason="message_fetch_failed",
                        metadata={"error": str(exc), "uid": str(uid), "uidvalidity": uidvalidity},
                    )
                )

        done = len(uids) <= batch_size
        cursor = (
            str(batch_uids[-1])
            if batch_uids
            else (checkpoint.cursor if checkpoint_matches and checkpoint else None)
        )
        if batch_uids or not checkpoint_matches or done:
            yield SourceRecordBatch(
                records=batch_records,
                skipped=skipped,
                checkpoint=SourceImportCheckpoint(
                    cursor=cursor,
                    source_version=source_version,
                    records_seen=len(batch_uids),
                    records_imported=len(batch_records),
                    records_skipped=len(skipped),
                    done=done,
                    metadata={
                        "source_uri": manifest.source_uri,
                        "mailbox": config["mailbox"],
                        "start_uid": start_uid,
                        "uidvalidity": uidvalidity,
                        "uidvalidity_reset": not checkpoint_matches and checkpoint is not None,
                    },
                ),
            )

    async def _select_readonly_metadata(self, config: Mapping[str, Any]) -> tuple[str, int]:
        client = await self._connect(config)
        try:
            status, payload = _imap_status_payload(
                await _imap_call(client.select, str(config["mailbox"]), readonly=True)
            )
            _require_imap_ok(status, "SELECT")
            uidvalidity = await _imap_uidvalidity(client)
            message_count = _imap_message_count(payload)
            return uidvalidity, message_count
        finally:
            await _imap_logout(client)

    async def _search_uids(self, config: Mapping[str, Any], *, start_uid: int) -> list[int]:
        client = await self._connect(config)
        try:
            status, _payload = _imap_status_payload(
                await _imap_call(client.select, str(config["mailbox"]), readonly=True)
            )
            _require_imap_ok(status, "SELECT")
            status, payload = _imap_status_payload(
                await _imap_call(client.uid, "SEARCH", None, "UID", f"{start_uid}:*")
            )
            _require_imap_ok(status, "UID SEARCH")
            return [uid for uid in _parse_uid_search(payload) if uid >= start_uid]
        finally:
            await _imap_logout(client)

    async def _fetch_messages(
        self, config: Mapping[str, Any], *, uids: Sequence[int]
    ) -> tuple[dict[int, Message], dict[int, Exception]]:
        client = await self._connect(config)
        try:
            status, _payload = _imap_status_payload(
                await _imap_call(client.select, str(config["mailbox"]), readonly=True)
            )
            _require_imap_ok(status, "SELECT")
            messages: dict[int, Message] = {}
            errors: dict[int, Exception] = {}
            for uid in uids:
                try:
                    status, payload = _imap_status_payload(
                        await _imap_call(client.uid, "FETCH", str(uid), "(RFC822)")
                    )
                    _require_imap_ok(status, "UID FETCH")
                    messages[uid] = message_from_bytes(_imap_rfc822_payload(payload))
                except Exception as exc:
                    errors[uid] = exc
            return messages, errors
        finally:
            await _imap_logout(client)

    async def _connect(self, config: Mapping[str, Any]) -> ImapClient:
        username = _optional_str(config.get("username"))
        password = _imap_password(config)
        if username and password is None:
            msg = "IMAP imports require password or password_env when username is set"
            raise ValueError(msg)

        connect_host = _optional_str(config.get("connect_host"))
        if self._client_factory is _default_imap_client_factory:
            client_result = await _imap_call(
                self._client_factory,
                str(config["host"]),
                int(config["port"]),
                bool(config["ssl"]),
                connect_host=connect_host,
            )
        else:
            client_result = await _imap_call(
                self._client_factory,
                connect_host or str(config["host"]),
                int(config["port"]),
                bool(config["ssl"]),
            )
        client = cast(
            ImapClient,
            client_result,
        )
        if username:
            status, _payload = _imap_status_payload(
                await _imap_call(client.login, username, password)
            )
            _require_imap_ok(status, "LOGIN")
        return client


def ensure_mailbox_adapter_registered() -> None:
    """Register built-in mailbox adapters once."""
    if not source_adapter_registry.has(MBOX_ADAPTER_NAME):
        register_source_adapter(MboxSourceAdapter())
    if not source_adapter_registry.has(MAILDIR_ADAPTER_NAME):
        register_source_adapter(MaildirSourceAdapter())
    if not source_adapter_registry.has(IMAP_ADAPTER_NAME):
        register_source_adapter(ImapSourceAdapter())


def _resolve_mbox_path(source_uri: str) -> Path:
    raw_path = source_uri[7:] if source_uri.startswith("file://") else source_uri
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        msg = f"MBOX source does not exist: {path}"
        raise FileNotFoundError(msg)
    if not path.is_file():
        msg = f"MBOX source is not a file: {path}"
        raise ValueError(msg)
    return path


def _resolve_maildir_path(source_uri: str) -> Path:
    raw_path = source_uri[7:] if source_uri.startswith("file://") else source_uri
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        msg = f"Maildir source does not exist: {path}"
        raise FileNotFoundError(msg)
    if not path.is_dir():
        msg = f"Maildir source is not a directory: {path}"
        raise ValueError(msg)
    missing = [name for name in ("cur", "new", "tmp") if not (path / name).is_dir()]
    if missing:
        msg = f"Maildir source is missing required directories: {', '.join(missing)}"
        raise ValueError(msg)
    symlinked_dirs = [name for name in ("cur", "new", "tmp") if (path / name).is_symlink()]
    if symlinked_dirs:
        msg = f"Maildir source has symlinked directories: {', '.join(symlinked_dirs)}"
        raise ValueError(msg)
    return path


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _email_message_dedupe_key(
    *,
    manifest: SourceImportManifest,
    adapter_record_id: str,
    content_hash: str,
    message_id: str | None,
) -> str:
    if message_id:
        raw_value = "\0".join((_EMAIL_DEDUPE_IDENTITY, message_id, content_hash))
        return f"source:{sha256(raw_value.encode()).hexdigest()}"
    return build_source_dedupe_key(
        manifest=manifest,
        adapter_record_id=adapter_record_id,
        content_hash=content_hash,
    ).value


def _record_from_message(
    manifest: SourceImportManifest,
    message: Message,
    *,
    index: int,
    source_uri: str | None = None,
    extra_metadata: Mapping[str, Any] | None = None,
) -> SourceRecord:
    subject = _decode_header_value(message.get("Subject")) or "(no subject)"
    body = _extract_body(message)
    message_id = _normalized_message_id(message.get("Message-ID"))
    fallback_seed = build_source_content_hash(subject, body, str(index))
    adapter_record_id = message_id or f"{manifest.adapter_name}:{index}:{fallback_seed[:16]}"
    content_hash = build_source_content_hash(subject, body, message_id)
    dedupe_key = _email_message_dedupe_key(
        manifest=manifest,
        adapter_record_id=adapter_record_id,
        content_hash=content_hash,
        message_id=message_id,
    )
    record_source_uri = (
        source_uri if source_uri is not None else _message_source_uri(manifest, index)
    )
    source_id = build_source_record_id(
        manifest=manifest,
        adapter_record_id=adapter_record_id,
    )

    header_addresses = _header_addresses(message)
    participants = _unique_participants(
        header_addresses["from"]
        + header_addresses["to"]
        + header_addresses["cc"]
        + header_addresses["bcc"]
    )
    references = _message_id_list(message.get("References"))
    in_reply_to = _normalized_message_id(message.get("In-Reply-To"))
    thread_id = _thread_id(message_id=message_id, in_reply_to=in_reply_to, references=references)
    occurred_at = _message_datetime(message.get("Date"))
    gmail_labels = _gmail_labels(message.get("X-Gmail-Labels"))

    metadata: dict[str, Any] = {
        "message_id": message_id,
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "subject": subject,
        "gmail_labels": gmail_labels,
        "from": header_addresses["from"],
        "to": header_addresses["to"],
        "cc": header_addresses["cc"],
        "bcc": header_addresses["bcc"],
        "source_path": manifest.source_uri,
        "mailbox_index": index,
    }
    if extra_metadata is not None:
        metadata.update(dict(extra_metadata))

    return SourceRecord(
        adapter_record_id=adapter_record_id,
        source_id=source_id,
        source_type="mailbox_message",
        source_uri=record_source_uri,
        source_version=manifest.source_version,
        title=subject,
        body=body,
        content_hash=content_hash,
        dedupe_key=dedupe_key,
        privacy_class=manifest.privacy_class,
        transform_behavior=manifest.transform_behavior,
        transform_version=manifest.adapter_version,
        occurred_at=occurred_at,
        participants=participants,
        labels=_unique_participants(["mailbox", "email", *gmail_labels]),
        metadata=metadata,
        attachments=_extract_attachments(message, adapter_record_id, record_source_uri),
    )


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _normalized_message_id(value: str | None) -> str | None:
    if not value:
        return None
    ids = _message_id_list(value)
    if ids:
        return ids[-1]
    cleaned = value.strip().strip("<>")
    return cleaned or None


def _message_id_list(value: str | None) -> list[str]:
    if not value:
        return []
    decoded = _decode_header_value(value)
    ids: list[str] = []
    token = ""
    in_angle = False
    for char in decoded:
        if char == "<":
            token = ""
            in_angle = True
        elif char == ">" and in_angle:
            cleaned = token.strip()
            if cleaned:
                ids.append(cleaned)
            token = ""
            in_angle = False
        elif in_angle:
            token += char
    if ids:
        return ids
    return [part.strip().strip("<>") for part in decoded.split() if part.strip().strip("<>")]


def _header_addresses(message: Message) -> dict[str, list[str]]:
    return {
        name.lower(): _addresses(message.get_all(name, [])) for name in ("From", "To", "Cc", "Bcc")
    }


def _addresses(values: list[str]) -> list[str]:
    addresses: list[str] = []
    for display_name, address in getaddresses(values):
        if address:
            addresses.append(address)
        elif display_name:
            addresses.append(display_name)
    return _unique_participants(addresses)


def _unique_participants(values: list[str]) -> list[str]:
    seen: set[str] = set()
    participants: list[str] = []
    for value in values:
        participant = value.strip()
        if not participant:
            continue
        key = participant.casefold()
        if key in seen:
            continue
        seen.add(key)
        participants.append(participant)
    return participants


def _gmail_labels(value: str | None) -> list[str]:
    decoded = _decode_header_value(value)
    if not decoded:
        return []
    return _unique_participants(
        [part.strip().strip('"') for part in decoded.split(",") if part.strip().strip('"')]
    )


def _thread_id(
    *,
    message_id: str | None,
    in_reply_to: str | None,
    references: list[str],
) -> str | None:
    if references:
        return references[0]
    if in_reply_to:
        return in_reply_to
    return message_id


def _message_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_body(message: Message) -> str:
    plain_parts: list[str] = []
    fallback_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        if _is_attachment(part):
            continue
        if part.get_content_maintype() != "text":
            continue
        text = _decode_part_payload(part).strip()
        if not text:
            continue
        if part.get_content_subtype() == "plain":
            plain_parts.append(text)
        else:
            fallback_parts.append(text)
    return "\n\n".join(plain_parts or fallback_parts)


def _extract_attachments(
    message: Message,
    adapter_record_id: str,
    source_uri: str | None,
) -> list[SourceAttachmentRecord]:
    attachments: list[SourceAttachmentRecord] = []
    for part_index, part in enumerate(message.walk()):
        if part.is_multipart() or not _is_attachment(part):
            continue
        payload = _part_payload_bytes(part)
        filename = _decode_header_value(part.get_filename()) or f"attachment-{part_index}"
        attachments.append(
            SourceAttachmentRecord(
                adapter_attachment_id=f"{adapter_record_id}:part:{part_index}",
                filename=filename,
                media_type=part.get_content_type(),
                size_bytes=len(payload),
                content_hash=sha256(payload).hexdigest() if payload else None,
                source_path=f"{source_uri}&part={part_index}" if source_uri else None,
                metadata={
                    "content_disposition": part.get_content_disposition(),
                    "content_id": _decode_header_value(part.get("Content-ID")),
                },
            )
        )
    return attachments


def _is_attachment(part: Message) -> bool:
    return part.get_content_disposition() == "attachment" or bool(part.get_filename())


def _decode_part_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    raw_payload = part.get_payload()
    if isinstance(raw_payload, list):
        return ""
    return str(raw_payload or "")


def _part_payload_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    raw_payload = part.get_payload()
    if isinstance(raw_payload, list):
        return b""
    return str(raw_payload or "").encode("utf-8")


def _maildir_entries(path: Path) -> list[Path]:
    entries: list[Path] = []
    for folder in ("cur", "new"):
        entries.extend(
            child
            for child in (path / folder).iterdir()
            if child.is_file() and not child.is_symlink()
        )
    return entries


def _maildir_symlinked_keys(path: Path, colon: str) -> set[str]:
    """Maildir keys whose backing file is a symlink and must not be ingested.

    `mailbox.Maildir` happily reads through symlinked message files, so an
    attacker who stages a Maildir with a symlinked entry could exfiltrate
    arbitrary host files. The Maildir key is the unique-name prefix of the
    filename (everything before the ``colon`` info separator).
    """
    symlinked: set[str] = set()
    for folder in ("cur", "new"):
        for child in (path / folder).iterdir():
            if child.is_symlink():
                symlinked.add(child.name.split(colon)[0])
    return symlinked


def _maildir_source_version(entries: list[Path]) -> str:
    latest_mtime = max((entry.stat().st_mtime_ns for entry in entries), default=0)
    return f"entries:{len(entries)}:mtime:{latest_mtime}"


def _message_source_uri(
    manifest: SourceImportManifest,
    index: int,
    *,
    mailbox_key: str | None = None,
) -> str | None:
    if not manifest.source_uri:
        return None
    source_uri = f"{manifest.source_uri}#message={index}"
    if mailbox_key is not None:
        source_uri = f"{source_uri}&key={quote(mailbox_key, safe='')}"
    return source_uri


class _PinnedIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_host: str,
        ssl_context: ssl_module.SSLContext,
    ) -> None:
        self._connect_host = connect_host
        super().__init__(host, port, ssl_context=ssl_context)

    def _create_socket(self, timeout: float | None) -> ssl_module.SSLSocket:
        if timeout is not None and not timeout:
            msg = "Non-blocking socket (timeout=0) is not supported"
            raise ValueError(msg)
        address = (self._connect_host, self.port)
        raw_socket = (
            socket.create_connection(address, timeout)
            if timeout is not None
            else socket.create_connection(address)
        )
        return self.ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


def _default_imap_client_factory(
    host: str,
    port: int,
    ssl: bool,
    *,
    connect_host: str | None = None,
) -> ImapClient:
    if ssl:
        return cast(
            ImapClient,
            _PinnedIMAP4SSL(
                host,
                port,
                connect_host=connect_host or _public_imap_connect_host(host),
                ssl_context=ssl_module.create_default_context(),
            ),
        )
    msg = "IMAP imports require TLS"
    raise ValueError(msg)


async def _imap_call(callable_: Callable[..., object], *args: object, **kwargs: object) -> object:
    result = await asyncio.to_thread(callable_, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _imap_config(
    source_uri: str,
    options: Mapping[str, object],
    *,
    pin_connect_host: bool = False,
) -> dict[str, Any]:
    parsed = urlparse(source_uri)
    if parsed.scheme and parsed.scheme not in {"imap", "imaps"}:
        msg = "IMAP source_uri must use imap:// or imaps://"
        raise ValueError(msg)
    if parsed.password:
        msg = "IMAP source_uri must not contain a password"
        raise ValueError(msg)

    ssl = _bool_option(options.get("ssl")) if "ssl" in options else parsed.scheme != "imap"
    host = _optional_str(options.get("host")) or parsed.hostname
    if not host:
        msg = "IMAP imports require host"
        raise ValueError(msg)
    if not ssl:
        msg = "IMAP imports require TLS"
        raise ValueError(msg)
    allow_private_network = _bool_option(options.get("allow_private_network"))
    connect_host = host
    if not allow_private_network:
        _reject_private_imap_host(host)
        if pin_connect_host:
            connect_host = _public_imap_connect_host(host)
    option_port = options.get("port")
    parsed_port = parsed.port
    if option_port is not None:
        port = int(str(option_port))
    elif parsed_port is not None:
        port = int(parsed_port)
    else:
        port = 993 if ssl else 143
    mailbox = _optional_str(options.get("mailbox")) or unquote(parsed.path.lstrip("/")) or "INBOX"
    username = _optional_str(options.get("username"))
    if username is None and parsed.username:
        username = unquote(parsed.username)

    config: dict[str, Any] = {
        "host": host,
        "port": port,
        "ssl": ssl,
        "mailbox": mailbox,
        "username": username,
        "password": _optional_str(options.get("password")),
        "password_env": _optional_str(options.get("password_env")),
        "connect_host": connect_host,
    }
    config["source_identity"] = _optional_str(options.get("source_identity")) or _imap_identity(
        config
    )
    return config


def _imap_identity(config: Mapping[str, Any]) -> str:
    username = _optional_str(config.get("username"))
    user_part = f"{quote(username, safe='')}@" if username else ""
    mailbox_name = quote(str(config["mailbox"]), safe="/")
    return f"imap://{user_part}{config['host']}:{config['port']}/{mailbox_name}"


def _imap_source_uri(config: Mapping[str, Any]) -> str:
    scheme = "imaps" if config["ssl"] else "imap"
    username = _optional_str(config.get("username"))
    user_part = f"{quote(username, safe='')}@" if username else ""
    mailbox_name = quote(str(config["mailbox"]), safe="/")
    return f"{scheme}://{user_part}{config['host']}:{config['port']}/{mailbox_name}"


def _imap_message_source_uri(manifest: SourceImportManifest, uid: int) -> str | None:
    if not manifest.source_uri:
        return None
    return f"{manifest.source_uri}#uid={uid}"


def _public_imap_options(options: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): value
        for key, value in options.items()
        if str(key) not in _IMAP_SECRET_OPTION_KEYS
    }


def _imap_password(config: Mapping[str, Any]) -> str | None:
    password = _optional_str(config.get("password"))
    if password is not None:
        return password
    password_env = _optional_str(config.get("password_env"))
    if password_env:
        if not password_env.startswith(_IMAP_PASSWORD_ENV_PREFIX):
            msg = "IMAP password_env must use SIBYL_SOURCE_IMPORT_IMAP_*"
            raise ValueError(msg)
        return _optional_str(os.environ.get(password_env))
    username = _optional_str(config.get("username"))
    if not username:
        return None
    return _optional_str(os.environ.get(_managed_imap_password_env(config)))


def _managed_imap_password_env(config: Mapping[str, Any]) -> str:
    raw_value = f"{config['host']}_{config['port']}_{config['username']}".upper()
    token = "".join(
        char if ("A" <= char <= "Z" or "0" <= char <= "9") else "_" for char in raw_value
    )
    token = "_".join(part for part in token.split("_") if part)
    if len(token) > 96:
        digest = sha256(raw_value.encode()).hexdigest()[:16].upper()
        token = f"{token[:79]}_{digest}"
    return f"{_IMAP_PASSWORD_ENV_PREFIX}{token}_PASSWORD"


def _bool_option(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _reject_private_imap_host(host: str) -> None:
    normalized_host = host.strip("[]").strip().lower().rstrip(".")
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
    ):
        msg = f"IMAP host is private: {host}"
        raise ValueError(msg)
    if _is_private_imap_address(normalized_host):
        msg = f"IMAP host is private: {host}"
        raise ValueError(msg)
    for resolved_host in _resolve_imap_host_addresses(normalized_host):
        if _is_private_imap_address(resolved_host):
            msg = f"IMAP host is private: {host}"
            raise ValueError(msg)


def _public_imap_connect_host(host: str) -> str:
    normalized_host = host.strip("[]").strip().lower().rstrip(".")
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
    ):
        msg = f"IMAP host is private: {host}"
        raise ValueError(msg)
    try:
        address = ip_address(normalized_host)
    except ValueError:
        addresses = _resolve_imap_host_addresses(normalized_host)
        if not addresses:
            msg = f"IMAP host could not be resolved: {host}"
            raise ValueError(msg) from None
        for resolved_host in addresses:
            if _is_private_imap_address(resolved_host):
                msg = f"IMAP host is private: {host}"
                raise ValueError(msg) from None
        return addresses[0]
    if _is_private_imap_address(str(address)):
        msg = f"IMAP host is private: {host}"
        raise ValueError(msg)
    return str(address)


def _is_private_imap_address(value: str) -> bool:
    try:
        address = ip_address(value)
    except ValueError:
        return False
    return not address.is_global or address.is_multicast


def _resolve_imap_host_addresses(host: str) -> list[str]:
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    addresses: list[str] = []
    for result in results:
        sockaddr = result[4]
        if isinstance(sockaddr, tuple) and sockaddr:
            addresses.append(str(sockaddr[0]))
    return list(dict.fromkeys(addresses))


async def _imap_logout(client: ImapClient) -> None:
    try:
        await _imap_call(client.logout)
    except Exception:
        return


def _imap_status_payload(result: object) -> tuple[str, list[object]]:
    if not isinstance(result, tuple) or len(result) != 2:
        return str(result), []
    status_value, payload = result
    status = status_value.decode() if isinstance(status_value, bytes) else str(status_value)
    if isinstance(payload, list | tuple):
        return status, list(payload)
    return status, [payload]


def _require_imap_ok(status: str, operation: str) -> None:
    if status.upper() == "OK":
        return
    msg = f"imap_{operation.lower().replace(' ', '_')}_failed"
    raise ValueError(msg)


async def _imap_uidvalidity(client: ImapClient) -> str:
    _status, payload = _imap_status_payload(await _imap_call(client.response, "UIDVALIDITY"))
    for item in payload:
        text = _imap_payload_text(item)
        for token in text.replace("[", " ").replace("]", " ").split():
            if token.isdigit():
                return token
    msg = "imap_uidvalidity_missing"
    raise ValueError(msg)


def _imap_message_count(payload: list[object]) -> int:
    if not payload:
        return 0
    text = _imap_payload_text(payload[0])
    return int(text) if text.isdigit() else 0


def _parse_uid_search(payload: list[object]) -> list[int]:
    uids: list[int] = []
    for item in payload:
        for token in _imap_payload_text(item).split():
            if token.isdigit():
                uids.append(int(token))
    return uids


def _imap_rfc822_payload(payload: list[object]) -> bytes:
    for item in payload:
        if isinstance(item, tuple):
            for part in item:
                if isinstance(part, bytes) and b"\n" in part:
                    return part
        elif isinstance(item, bytes) and b"\n" in item:
            return item
    msg = "imap_rfc822_payload_missing"
    raise ValueError(msg)


def _imap_payload_text(item: object) -> str:
    if isinstance(item, bytes):
        return item.decode("utf-8", errors="replace").strip()
    return str(item).strip()


__all__ = [
    "IMAP_ADAPTER_NAME",
    "IMAP_ADAPTER_VERSION",
    "MAILDIR_ADAPTER_NAME",
    "MAILDIR_ADAPTER_VERSION",
    "MBOX_ADAPTER_NAME",
    "MBOX_ADAPTER_VERSION",
    "ImapSourceAdapter",
    "MaildirSourceAdapter",
    "MboxSourceAdapter",
    "ensure_mailbox_adapter_registered",
]
