"""Mailbox source adapters for raw-memory imports."""

from __future__ import annotations

import mailbox
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
            keys = sorted(maildir.keys())
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


def ensure_mailbox_adapter_registered() -> None:
    """Register built-in mailbox adapters once."""
    if not source_adapter_registry.has(MBOX_ADAPTER_NAME):
        register_source_adapter(MboxSourceAdapter())
    if not source_adapter_registry.has(MAILDIR_ADAPTER_NAME):
        register_source_adapter(MaildirSourceAdapter())


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
    return path


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    dedupe_key = build_source_dedupe_key(
        manifest=manifest,
        adapter_record_id=adapter_record_id,
        content_hash=content_hash,
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

    metadata: dict[str, Any] = {
        "message_id": message_id,
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "subject": subject,
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
        dedupe_key=dedupe_key.value,
        privacy_class=manifest.privacy_class,
        transform_behavior=manifest.transform_behavior,
        transform_version=manifest.adapter_version,
        occurred_at=occurred_at,
        participants=participants,
        labels=["mailbox", "email"],
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
        entries.extend(child for child in (path / folder).iterdir() if child.is_file())
    return entries


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


__all__ = [
    "MAILDIR_ADAPTER_NAME",
    "MAILDIR_ADAPTER_VERSION",
    "MBOX_ADAPTER_NAME",
    "MBOX_ADAPTER_VERSION",
    "MaildirSourceAdapter",
    "MboxSourceAdapter",
    "ensure_mailbox_adapter_registered",
]
