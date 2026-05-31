"""Agent transcript source adapters for raw-memory imports."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from sibyl_core.models.sources import (
    SourceAdapterCapability,
    SourceAdapterDescriptor,
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

CLAUDE_CODE_ADAPTER_NAME = "claude_code_jsonl"
CLAUDE_CODE_ADAPTER_VERSION = "1.0"
CODEX_ADAPTER_NAME = "codex_jsonl"
CODEX_ADAPTER_VERSION = "1.0"


@dataclass(slots=True)
class _TranscriptTurn:
    adapter_record_id: str
    source_type: str
    title: str
    body: str
    role: str
    source_uri: str | None
    occurred_at: datetime | None
    metadata: dict[str, object]
    participants: list[str]
    labels: list[str]
    folded_tool_results: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ParsedTranscript:
    records: tuple[SourceRecord, ...]
    skipped: tuple[SourceSkippedRecord, ...]


class ClaudeCodeJsonlAdapter:
    """Source adapter for Claude Code JSONL transcripts."""

    descriptor = SourceAdapterDescriptor(
        name=CLAUDE_CODE_ADAPTER_NAME,
        version=CLAUDE_CODE_ADAPTER_VERSION,
        source_type="agent_transcript",
        display_name="Claude Code JSONL transcript",
        capabilities=[
            SourceAdapterCapability.CHECKPOINTS,
            SourceAdapterCapability.INCREMENTAL,
            SourceAdapterCapability.SKIPPED_RECORDS,
        ],
        default_privacy_class=SourcePrivacyClass.PRIVATE,
        transform_behavior=SourceTransformBehavior.RAW,
        metadata_schema={
            "cwd": "string",
            "entrypoint": "string",
            "agent_id": "string",
            "forked_from": "string",
            "forked_from_adapter_record_id": "string",
            "git_branch": "string",
            "is_compact_summary": "boolean",
            "parent_adapter_record_id": "string",
            "parent_uuid": "string",
            "prompt_id": "string",
            "role": "string",
            "session_id": "string",
            "source_tool_assistant_adapter_record_id": "string",
            "source_tool_assistant_uuid": "string",
            "turn_uuid": "string",
        },
        supports_incremental=True,
    )

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        path = _resolve_transcript_path(source_uri)
        files = _jsonl_files(path)
        option_values = dict(options or {})
        return _manifest_for_path(
            adapter=self.descriptor,
            path=path,
            files=files,
            options=option_values,
        )

    def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        return _iter_parsed_records(
            manifest,
            checkpoint=checkpoint,
            batch_size=batch_size,
            parser=_parse_claude_transcripts,
        )


class CodexJsonlAdapter:
    """Source adapter for Codex rollout and history JSONL transcripts."""

    descriptor = SourceAdapterDescriptor(
        name=CODEX_ADAPTER_NAME,
        version=CODEX_ADAPTER_VERSION,
        source_type="agent_transcript",
        display_name="Codex JSONL transcript",
        capabilities=[
            SourceAdapterCapability.CHECKPOINTS,
            SourceAdapterCapability.INCREMENTAL,
            SourceAdapterCapability.SKIPPED_RECORDS,
        ],
        default_privacy_class=SourcePrivacyClass.PRIVATE,
        transform_behavior=SourceTransformBehavior.RAW,
        metadata_schema={
            "call_id": "string",
            "cwd": "string",
            "forked_from": "string",
            "parent_uuid": "string",
            "role": "string",
            "session_id": "string",
            "source_subagent": "string",
            "source_event_type": "string",
            "thread_source": "object",
            "tool_name": "string",
        },
        supports_incremental=True,
    )

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        path = _resolve_transcript_path(source_uri)
        files = _jsonl_files(path)
        option_values = dict(options or {})
        return _manifest_for_path(
            adapter=self.descriptor,
            path=path,
            files=files,
            options=option_values,
        )

    def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        return _iter_parsed_records(
            manifest,
            checkpoint=checkpoint,
            batch_size=batch_size,
            parser=_parse_codex_transcripts,
        )


def ensure_transcript_adapters_registered() -> None:
    """Register built-in transcript adapters once."""
    if not source_adapter_registry.has(CLAUDE_CODE_ADAPTER_NAME):
        register_source_adapter(ClaudeCodeJsonlAdapter())
    if not source_adapter_registry.has(CODEX_ADAPTER_NAME):
        register_source_adapter(CodexJsonlAdapter())


async def _iter_parsed_records(
    manifest: SourceImportManifest,
    *,
    checkpoint: SourceImportCheckpoint | None,
    batch_size: int,
    parser: Callable[[SourceImportManifest], _ParsedTranscript],
) -> AsyncIterator[SourceRecordBatch]:
    parsed = parser(manifest)
    if (
        checkpoint
        and checkpoint.source_version
        and checkpoint.source_version != manifest.source_version
    ):
        msg = "source_import_checkpoint_source_version_mismatch"
        raise ValueError(msg)
    start = int(checkpoint.cursor) if checkpoint and checkpoint.cursor else 0
    batch_records = list(parsed.records[start : start + batch_size])
    cursor = start + len(batch_records)
    done = cursor >= len(parsed.records)
    skipped = list(parsed.skipped) if start == 0 else []
    if batch_records or skipped or start < len(parsed.records):
        yield SourceRecordBatch(
            records=batch_records,
            skipped=skipped,
            checkpoint=SourceImportCheckpoint(
                cursor=str(cursor) if not done else None,
                source_version=manifest.source_version,
                records_seen=cursor,
                records_imported=len(batch_records),
                records_skipped=len(skipped),
                done=done,
                metadata={"source_uri": manifest.source_uri},
            ),
        )


def _manifest_for_path(
    *,
    adapter: SourceAdapterDescriptor,
    path: Path,
    files: Sequence[Path],
    options: Mapping[str, object],
) -> SourceImportManifest:
    target_memory_scope = str(options.get("target_memory_scope") or "private")
    target_scope_key = _optional_str(options.get("target_scope_key"))
    source_identity = str(options.get("source_identity") or path)
    return SourceImportManifest(
        adapter_name=adapter.name,
        adapter_version=adapter.version,
        source_identity=source_identity,
        source_uri=str(path),
        source_version=_source_version(files, root=path),
        target_memory_scope=target_memory_scope,
        target_scope_key=target_scope_key,
        privacy_class=adapter.default_privacy_class,
        transform_behavior=adapter.transform_behavior,
        metadata_schema=dict(adapter.metadata_schema),
        metadata={
            "source_path": str(path),
            "file_count": len(files),
            "transcript_adapter": adapter.name,
        },
        options=dict(options),
    )


def _parse_claude_transcripts(manifest: SourceImportManifest) -> _ParsedTranscript:
    turns: list[_TranscriptTurn] = []
    skipped: list[SourceSkippedRecord] = []
    for file_path in _jsonl_files(_resolve_transcript_path(str(manifest.source_uri))):
        file_key = _file_record_key(manifest, file_path)
        for line_index, raw_line in enumerate(file_path.open(encoding="utf-8", errors="replace")):
            obj = _json_object(raw_line)
            if obj is None:
                skipped.append(_skipped(manifest, file_path, line_index, "json_parse_failed"))
                continue
            if obj.get("type") not in {"user", "assistant"}:
                continue
            message = obj.get("message")
            if not isinstance(message, Mapping):
                continue
            message = cast("Mapping[str, object]", message)
            role = _optional_str(message.get("role")) or str(obj.get("type") or "")
            if role not in {"user", "assistant"}:
                continue
            body, content_types = _content_text(message.get("content"))
            if not body:
                continue
            if role == "user" and content_types and set(content_types) <= {"tool_result"}:
                if turns and turns[-1].role == "assistant":
                    turns[-1].folded_tool_results.append(body)
                continue
            turn_uuid = _optional_str(obj.get("uuid")) or f"line:{line_index}"
            metadata = _claude_metadata(obj, content_types, file_path, line_index)
            turns.append(
                _TranscriptTurn(
                    adapter_record_id=_adapter_record_id(file_key, turn_uuid),
                    source_type="agent_transcript_turn",
                    title=_turn_title("claude_code", role, obj.get("timestamp")),
                    body=body,
                    role=role,
                    source_uri=_line_uri(file_path, line_index),
                    occurred_at=_parse_datetime(obj.get("timestamp")),
                    metadata=metadata,
                    participants=[role],
                    labels=["agent_transcript", "claude_code", role],
                )
            )
    _attach_claude_lineage_adapter_record_ids(turns)
    return _ParsedTranscript(
        records=tuple(_records_from_turns(manifest, turns)),
        skipped=tuple(skipped),
    )


def _parse_codex_transcripts(manifest: SourceImportManifest) -> _ParsedTranscript:
    turns: list[_TranscriptTurn] = []
    skipped: list[SourceSkippedRecord] = []
    for file_path in _jsonl_files(_resolve_transcript_path(str(manifest.source_uri))):
        file_key = _file_record_key(manifest, file_path)
        if file_path.name == "history.jsonl":
            turns.extend(_parse_codex_history_file(manifest, file_path, skipped))
            continue
        session_id = file_path.stem
        cwd: str | None = None
        session_metadata: dict[str, object] = {}
        pending_calls: dict[str, dict[str, object]] = {}
        emitted_tool_calls: set[str] = set()
        for line_index, raw_line in enumerate(file_path.open(encoding="utf-8", errors="replace")):
            obj = _json_object(raw_line)
            if obj is None:
                skipped.append(_skipped(manifest, file_path, line_index, "json_parse_failed"))
                continue
            payload = obj.get("payload")
            if not isinstance(payload, Mapping):
                continue
            payload = cast("Mapping[str, object]", payload)
            if obj.get("type") == "session_meta":
                session_id = _optional_str(payload.get("id")) or session_id
                cwd = _optional_str(payload.get("cwd"))
                session_metadata = _codex_session_metadata(payload)
                continue
            payload_type = payload.get("type")
            if payload_type == "message":
                turn = _codex_message_turn(
                    payload=payload,
                    file_path=file_path,
                    file_key=file_key,
                    line_index=line_index,
                    session_id=session_id,
                    cwd=cwd,
                    session_metadata=session_metadata,
                    timestamp=obj.get("timestamp"),
                )
                if turn is not None:
                    turns.append(turn)
            elif payload_type == "agent_message":
                message = _optional_str(payload.get("message"))
                if message:
                    turns.append(
                        _codex_agent_message_turn(
                            payload=payload,
                            file_path=file_path,
                            file_key=file_key,
                            line_index=line_index,
                            session_id=session_id,
                            cwd=cwd,
                            session_metadata=session_metadata,
                            timestamp=obj.get("timestamp"),
                        )
                    )
            elif payload_type == "function_call":
                call_id = _optional_str(payload.get("call_id"))
                if call_id:
                    pending_calls[call_id] = dict(payload)
            elif payload_type == "custom_tool_call":
                call_id = _optional_str(payload.get("call_id"))
                if call_id and payload.get("status") == "completed":
                    emitted_tool_calls.add(call_id)
                    turns.append(
                        _codex_tool_turn(
                            call=payload,
                            output={},
                            file_path=file_path,
                            file_key=file_key,
                            line_index=line_index,
                            session_id=session_id,
                            cwd=cwd,
                            session_metadata=session_metadata,
                            timestamp=obj.get("timestamp"),
                        )
                    )
                elif call_id:
                    pending_calls[call_id] = dict(payload)
            elif payload_type in {"function_call_output", "patch_apply_end"}:
                call_id = _optional_str(payload.get("call_id"))
                if call_id and call_id not in emitted_tool_calls:
                    call = pending_calls.pop(call_id, {})
                    turns.append(
                        _codex_tool_turn(
                            call=call,
                            output=dict(payload),
                            file_path=file_path,
                            file_key=file_key,
                            line_index=line_index,
                            session_id=session_id,
                            cwd=cwd,
                            session_metadata=session_metadata,
                            timestamp=obj.get("timestamp"),
                        )
                    )
        for call_id, call in pending_calls.items():
            turns.append(
                _codex_tool_turn(
                    call=call,
                    output={},
                    file_path=file_path,
                    file_key=file_key,
                    line_index=-1,
                    session_id=session_id,
                    cwd=cwd,
                    session_metadata=session_metadata,
                    timestamp=None,
                    fallback_call_id=call_id,
                )
            )
    return _ParsedTranscript(
        records=tuple(_records_from_turns(manifest, turns)),
        skipped=tuple(skipped),
    )


def _parse_codex_history_file(
    manifest: SourceImportManifest,
    file_path: Path,
    skipped: list[SourceSkippedRecord],
) -> list[_TranscriptTurn]:
    turns: list[_TranscriptTurn] = []
    file_key = _file_record_key(manifest, file_path)
    for line_index, raw_line in enumerate(file_path.open(encoding="utf-8", errors="replace")):
        obj = _json_object(raw_line)
        if obj is None:
            skipped.append(_skipped(manifest, file_path, line_index, "json_parse_failed"))
            continue
        session_id = _optional_str(obj.get("session_id")) or file_path.stem
        body = _optional_str(obj.get("text"))
        if not body:
            continue
        occurred_at = _timestamp_datetime(obj.get("ts"))
        turns.append(
            _TranscriptTurn(
                adapter_record_id=_adapter_record_id(
                    file_key,
                    session_id,
                    "history",
                    str(line_index),
                ),
                source_type="agent_transcript_turn",
                title=_turn_title(
                    "codex", "user", occurred_at.isoformat() if occurred_at else None
                ),
                body=body,
                role="user",
                source_uri=_line_uri(file_path, line_index),
                occurred_at=occurred_at,
                metadata={
                    "line_index": line_index,
                    "role": "user",
                    "session_id": session_id,
                    "source_event_type": "history_prompt",
                    "source_file": str(file_path),
                    "source_platform": "codex",
                },
                participants=["user"],
                labels=["agent_transcript", "codex", "user"],
            )
        )
    return turns


def _codex_message_turn(
    *,
    payload: Mapping[str, object],
    file_path: Path,
    file_key: str,
    line_index: int,
    session_id: str,
    cwd: str | None,
    session_metadata: Mapping[str, object] | None,
    timestamp: object,
) -> _TranscriptTurn | None:
    role = _optional_str(payload.get("role"))
    if role not in {"user", "assistant"}:
        return None
    body, content_types = _content_text(payload.get("content"))
    if not body:
        return None
    return _TranscriptTurn(
        adapter_record_id=_adapter_record_id(
            file_key,
            session_id,
            "message",
            str(line_index),
        ),
        source_type="agent_transcript_turn",
        title=_turn_title("codex", role, timestamp),
        body=body,
        role=role,
        source_uri=_line_uri(file_path, line_index),
        occurred_at=_parse_datetime(timestamp),
        metadata={
            **_codex_record_metadata(session_metadata, payload),
            "content_types": content_types,
            "cwd": cwd,
            "line_index": line_index,
            "role": role,
            "session_id": session_id,
            "source_event_type": "message",
            "source_file": str(file_path),
            "source_platform": "codex",
        },
        participants=[role],
        labels=["agent_transcript", "codex", role],
    )


def _codex_agent_message_turn(
    *,
    payload: Mapping[str, object],
    file_path: Path,
    file_key: str,
    line_index: int,
    session_id: str,
    cwd: str | None,
    session_metadata: Mapping[str, object] | None,
    timestamp: object,
) -> _TranscriptTurn:
    message = _optional_str(payload.get("message")) or ""
    return _TranscriptTurn(
        adapter_record_id=_adapter_record_id(
            file_key,
            session_id,
            "agent_message",
            str(line_index),
        ),
        source_type="agent_transcript_turn",
        title=_turn_title("codex", "assistant", timestamp),
        body=message,
        role="assistant",
        source_uri=_line_uri(file_path, line_index),
        occurred_at=_parse_datetime(timestamp),
        metadata={
            **_codex_record_metadata(session_metadata, payload),
            "cwd": cwd,
            "line_index": line_index,
            "phase": _optional_str(payload.get("phase")),
            "role": "assistant",
            "session_id": session_id,
            "source_event_type": "agent_message",
            "source_file": str(file_path),
            "source_platform": "codex",
        },
        participants=["assistant"],
        labels=["agent_transcript", "codex", "assistant"],
    )


def _codex_tool_turn(
    *,
    call: Mapping[str, object],
    output: Mapping[str, object],
    file_path: Path,
    file_key: str,
    line_index: int,
    session_id: str,
    cwd: str | None,
    session_metadata: Mapping[str, object] | None,
    timestamp: object,
    fallback_call_id: str | None = None,
) -> _TranscriptTurn:
    call_id = _optional_str(call.get("call_id")) or _optional_str(output.get("call_id"))
    call_id = call_id or fallback_call_id or f"{session_id}:{line_index}"
    tool_name = _optional_str(call.get("name")) or _optional_str(output.get("name")) or "tool"
    arguments = _optional_str(call.get("arguments")) or _optional_str(call.get("input")) or ""
    output_text = _optional_str(output.get("output")) or _optional_str(output.get("stdout")) or ""
    stderr = _optional_str(output.get("stderr"))
    body_parts = [f"Tool: {tool_name}"]
    if arguments:
        body_parts.append(f"Arguments:\n{arguments}")
    if output_text:
        body_parts.append(f"Output:\n{output_text}")
    if stderr:
        body_parts.append(f"Stderr:\n{stderr}")
    return _TranscriptTurn(
        adapter_record_id=_adapter_record_id(file_key, session_id, "tool", call_id),
        source_type="agent_tool_call",
        title=f"Codex tool call: {tool_name}",
        body="\n\n".join(body_parts),
        role="tool",
        source_uri=_line_uri(file_path, line_index) if line_index >= 0 else str(file_path),
        occurred_at=_parse_datetime(timestamp),
        metadata={
            **_codex_record_metadata(session_metadata, call, output),
            "call_id": call_id,
            "cwd": cwd,
            "line_index": line_index,
            "role": "tool",
            "session_id": session_id,
            "source_event_type": "tool_call",
            "source_file": str(file_path),
            "source_platform": "codex",
            "tool_name": tool_name,
        },
        participants=[f"tool:{tool_name}"],
        labels=["agent_transcript", "codex", "tool_call"],
    )


def _records_from_turns(
    manifest: SourceImportManifest,
    turns: Sequence[_TranscriptTurn],
) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    for turn in turns:
        body = turn.body
        if turn.folded_tool_results:
            body = "\n\n".join([body, *turn.folded_tool_results])
            turn.metadata["folded_tool_result_count"] = len(turn.folded_tool_results)
        content_hash = build_source_content_hash(turn.title, body, turn.role)
        dedupe_key = build_source_dedupe_key(
            manifest=manifest,
            adapter_record_id=turn.adapter_record_id,
            content_hash=content_hash,
        )
        source_id = build_source_record_id(
            manifest=manifest,
            adapter_record_id=turn.adapter_record_id,
        )
        records.append(
            SourceRecord(
                adapter_record_id=turn.adapter_record_id,
                source_id=source_id,
                source_type=turn.source_type,
                source_uri=turn.source_uri,
                source_version=manifest.source_version,
                title=turn.title,
                body=body,
                content_hash=content_hash,
                dedupe_key=dedupe_key.value,
                privacy_class=manifest.privacy_class,
                transform_behavior=manifest.transform_behavior,
                transform_version=manifest.adapter_version,
                occurred_at=turn.occurred_at,
                participants=turn.participants,
                labels=turn.labels,
                metadata=turn.metadata,
            )
        )
    return records


def _claude_metadata(
    obj: Mapping[str, object],
    content_types: Sequence[str],
    file_path: Path,
    line_index: int,
) -> dict[str, object]:
    return {
        "agent_id": _optional_str(obj.get("agentId")),
        "content_types": list(content_types),
        "cwd": _optional_str(obj.get("cwd")),
        "entrypoint": _optional_str(obj.get("entrypoint")),
        "forked_from": _optional_str(obj.get("forked_from"))
        or _optional_str(obj.get("forkedFrom")),
        "git_branch": _optional_str(obj.get("gitBranch")),
        "is_compact_summary": bool(obj.get("isCompactSummary")),
        "is_sidechain": bool(obj.get("isSidechain")),
        "line_index": line_index,
        "parent_uuid": _optional_str(obj.get("parentUuid")),
        "prompt_id": _optional_str(obj.get("promptId")),
        "role": _optional_str(obj.get("type")),
        "session_id": _optional_str(obj.get("sessionId")),
        "source_file": str(file_path),
        "source_platform": "claude_code",
        "source_tool_assistant_uuid": _optional_str(obj.get("sourceToolAssistantUUID")),
        "turn_uuid": _optional_str(obj.get("uuid")),
        "user_type": _optional_str(obj.get("userType")),
    }


def _attach_claude_lineage_adapter_record_ids(turns: Sequence[_TranscriptTurn]) -> None:
    by_uuid = {
        turn_uuid: turn.adapter_record_id
        for turn in turns
        if (turn_uuid := _optional_str(turn.metadata.get("turn_uuid")))
    }
    for turn in turns:
        for source_key, target_key in (
            ("parent_uuid", "parent_adapter_record_id"),
            ("forked_from", "forked_from_adapter_record_id"),
            (
                "source_tool_assistant_uuid",
                "source_tool_assistant_adapter_record_id",
            ),
        ):
            reference = _optional_str(turn.metadata.get(source_key))
            if reference and (adapter_record_id := by_uuid.get(reference)):
                turn.metadata[target_key] = adapter_record_id


def _codex_session_metadata(payload: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    source = _mapping_dict(payload.get("source"))
    thread_source = _mapping_dict(payload.get("thread_source")) or _mapping_dict(
        payload.get("threadSource")
    )
    if source:
        metadata["source"] = source
        if subagent := _optional_str(source.get("subagent")):
            metadata["source_subagent"] = subagent
    if thread_source:
        metadata["thread_source"] = thread_source
    metadata.update(_codex_lineage_metadata(payload, source, thread_source))
    return metadata


def _codex_record_metadata(
    session_metadata: Mapping[str, object] | None,
    *payloads: Mapping[str, object],
) -> dict[str, object]:
    metadata = dict(session_metadata or {})
    metadata.update(_codex_lineage_metadata(*payloads, metadata))
    return metadata


def _codex_lineage_metadata(*payloads: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in ("parent_uuid", "parentUuid"):
        value = next(
            (_optional_str(payload.get(key)) for payload in payloads if payload.get(key)), None
        )
        if value:
            metadata["parent_uuid"] = value
            break
    for key in ("forked_from", "forkedFrom"):
        value = next(
            (_optional_str(payload.get(key)) for payload in payloads if payload.get(key)), None
        )
        if value:
            metadata["forked_from"] = value
            break
    return metadata


def _mapping_dict(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _content_text(value: object) -> tuple[str, list[str]]:
    if isinstance(value, str):
        text = value.strip()
        return text, ["text"] if text else []
    if not isinstance(value, list):
        return "", []
    parts: list[str] = []
    content_types: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                parts.append(text)
                content_types.append("text")
            continue
        if not isinstance(item, Mapping):
            continue
        item = cast("Mapping[str, object]", item)
        item_type = _optional_str(item.get("type")) or "unknown"
        content_types.append(item_type)
        text = _item_text(item, item_type)
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip(), list(dict.fromkeys(content_types))


def _item_text(item: Mapping[str, object], item_type: str) -> str:
    if item_type == "tool_result":
        result = item.get("content")
        return f"Tool result:\n{_json_text(result)}".strip()
    for key in ("text", "content", "output"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if item_type == "tool_use":
        name = _optional_str(item.get("name")) or "tool"
        tool_input = item.get("input")
        return f"Tool use: {name}\n{_json_text(tool_input)}".strip()
    return _json_text(item).strip()


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _json_object(raw_line: str) -> dict[str, object] | None:
    line = raw_line.strip()
    if not line:
        return None
    if not line.startswith("{"):
        start = line.find("{")
        end = line.rfind("}")
        if start >= 0 and end > start:
            line = line[start : end + 1]
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _resolve_transcript_path(source_uri: str) -> Path:
    raw_path = source_uri[7:] if source_uri.startswith("file://") else source_uri
    unresolved_path = Path(raw_path).expanduser()
    if unresolved_path.is_symlink():
        msg = f"Transcript source cannot be a symlink: {unresolved_path}"
        raise ValueError(msg)
    path = unresolved_path.resolve()
    if not path.exists():
        msg = f"Transcript source does not exist: {path}"
        raise FileNotFoundError(msg)
    if not path.is_file() and not path.is_dir():
        msg = f"Transcript source is not a file or directory: {path}"
        raise ValueError(msg)
    if path.is_file() and path.suffix != ".jsonl":
        msg = f"Transcript source must be a .jsonl file: {path}"
        raise ValueError(msg)
    return path


def _jsonl_files(path: Path) -> tuple[Path, ...]:
    if path.is_file():
        return (path,)
    for child in path.rglob("*"):
        if child.is_symlink():
            msg = f"Transcript source cannot include symlinked entries: {child}"
            raise ValueError(msg)
    files: list[Path] = []
    for child in sorted(path.rglob("*.jsonl")):
        if child.is_file():
            files.append(child.resolve())
    if not files:
        msg = f"Transcript directory contains no JSONL files: {path}"
        raise ValueError(msg)
    return tuple(files)


def _source_version(files: Sequence[Path], *, root: Path) -> str:
    base = root if root.is_dir() else root.parent
    hasher = sha256()
    for file in sorted(files):
        try:
            file_key = file.relative_to(base).as_posix()
        except ValueError:
            file_key = file.name
        stat = file.stat()
        for value in (file_key, str(stat.st_size), str(stat.st_mtime_ns)):
            hasher.update(value.encode("utf-8"))
            hasher.update(b"\0")
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        hasher.update(b"\0")
    return f"files:{len(files)}:sha256:{hasher.hexdigest()}"


def _file_record_key(manifest: SourceImportManifest, file_path: Path) -> str:
    root = _resolve_transcript_path(str(manifest.source_uri))
    base = root if root.is_dir() else root.parent
    try:
        return file_path.relative_to(base).as_posix()
    except ValueError:
        return file_path.name


def _adapter_record_id(file_key: str, *parts: str) -> str:
    raw_value = ":".join([file_key, *parts])
    if len(raw_value) <= 500:
        return raw_value
    digest = sha256(raw_value.encode("utf-8")).hexdigest()
    readable = Path(file_key).name[-96:]
    return f"{readable}:sha256:{digest}"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(value: object) -> datetime | None:
    text = _optional_str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _timestamp_datetime(value: object) -> datetime | None:
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return _parse_datetime(value)


def _turn_title(platform: str, role: str, timestamp: object) -> str:
    occurred = _parse_datetime(timestamp) or _timestamp_datetime(timestamp)
    suffix = f" at {occurred.isoformat()}" if occurred else ""
    return f"{platform} {role} turn{suffix}"


def _line_uri(file_path: Path, line_index: int) -> str:
    return f"{file_path}#line={line_index + 1}"


def _skipped(
    manifest: SourceImportManifest,
    file_path: Path,
    line_index: int,
    reason: str,
) -> SourceSkippedRecord:
    file_key = _file_record_key(manifest, file_path)
    return SourceSkippedRecord(
        adapter_record_id=_adapter_record_id(file_key, "skipped", str(line_index)),
        source_uri=_line_uri(file_path, line_index),
        reason=reason,
        metadata={"source_file": str(file_path), "line_index": line_index},
    )


__all__ = [
    "CLAUDE_CODE_ADAPTER_NAME",
    "CLAUDE_CODE_ADAPTER_VERSION",
    "CODEX_ADAPTER_NAME",
    "CODEX_ADAPTER_VERSION",
    "ClaudeCodeJsonlAdapter",
    "CodexJsonlAdapter",
    "ensure_transcript_adapters_registered",
]
