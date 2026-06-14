from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sibyl_core.models.sources import SourcePrivacyClass
from sibyl_core.services.source_adapters import (
    SourceRecordImportDecision,
    clear_source_adapters,
    get_source_adapter,
    import_source_batch,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory
from sibyl_core.services.transcript_adapters import (
    CLAUDE_CODE_ADAPTER_NAME,
    CODEX_ADAPTER_NAME,
    ClaudeCodeJsonlAdapter,
    CodexJsonlAdapter,
    ensure_transcript_adapters_registered,
)


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    clear_source_adapters()
    yield
    clear_source_adapters()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        "".join(f"{json.dumps(row)}\n" for row in rows),
        encoding="utf-8",
    )
    return path


class InMemoryRawMemoryRememberer:
    def __init__(self) -> None:
        self.memories: list[RawMemory] = []
        self.by_dedupe_key: dict[str, RawMemory] = {}

    async def __call__(self, **kwargs: object) -> RawMemory:
        memory = RawMemory(
            id=f"raw-{len(self.memories) + 1}",
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
            captured_at=datetime(2026, 5, 31, 12, tzinfo=UTC),
            created_at=datetime(2026, 5, 31, 12, tzinfo=UTC),
        )
        self.memories.append(memory)
        self.by_dedupe_key[str(memory.metadata["dedupe_key"])] = memory
        return memory

    async def remember_many(self, payloads):
        return [
            await self(
                organization_id=payload.organization_id,
                principal_id=payload.principal_id,
                source_id=payload.source_id,
                raw_content=payload.raw_content,
                title=payload.title,
                memory_scope=payload.memory_scope,
                scope_key=payload.scope_key,
                tags=payload.tags,
                metadata=payload.metadata,
                provenance=payload.provenance,
                capture_surface=payload.capture_surface,
                entity_type=payload.entity_type,
            )
            for payload in payloads
        ]

    async def duplicate_checker(self, *, record, payload):
        existing = self.by_dedupe_key.get(str(payload.metadata["dedupe_key"]))
        if existing is None:
            return None
        return SourceRecordImportDecision(duplicate_raw_memory_id=existing.id)


@pytest.mark.asyncio
async def test_claude_code_adapter_preserves_turn_metadata(tmp_path: Path) -> None:
    transcript = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            {
                "type": "user",
                "uuid": "turn-user-1",
                "parentUuid": None,
                "sessionId": "session-1",
                "timestamp": "2026-05-29T04:52:48.980Z",
                "cwd": "/Users/bliss/dev/sibyl",
                "gitBranch": "main",
                "message": {"role": "user", "content": "build the transcript adapter"},
            },
            {
                "type": "assistant",
                "uuid": "turn-assistant-1",
                "parentUuid": "turn-user-1",
                "sessionId": "session-1",
                "timestamp": "2026-05-29T04:53:00.000Z",
                "cwd": "/Users/bliss/dev/sibyl",
                "agentId": "agent-1",
                "forkedFrom": "turn-root",
                "promptId": "prompt-1",
                "sourceToolAssistantUUID": "turn-tool",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "adapter started"},
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "AGENTS.md"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "tool-result-1",
                "parentUuid": "turn-assistant-1",
                "sessionId": "session-1",
                "timestamp": "2026-05-29T04:53:01.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "file contents"}],
                },
            },
            {
                "type": "assistant",
                "uuid": "sidechain-1",
                "parentUuid": "turn-assistant-1",
                "isSidechain": True,
                "sessionId": "session-1",
                "timestamp": "2026-05-29T04:53:02.000Z",
                "message": {"role": "assistant", "content": "subagent reply"},
            },
        ],
    )
    adapter = ClaudeCodeJsonlAdapter()

    manifest = await adapter.prepare_manifest(source_uri=str(transcript))
    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    assert manifest.adapter_name == "claude_code_jsonl"
    assert manifest.privacy_class is SourcePrivacyClass.PRIVATE
    assert batch.checkpoint.done is True
    assert [record.adapter_record_id for record in batch.records] == [
        "session.jsonl:turn-user-1",
        "session.jsonl:turn-assistant-1",
        "session.jsonl:sidechain-1",
    ]
    assistant = batch.records[1]
    assert assistant.source_type == "agent_transcript_turn"
    assert "adapter started" in assistant.body
    assert "Tool result" in assistant.body
    assert assistant.metadata["agent_id"] == "agent-1"
    assert assistant.metadata["forked_from"] == "turn-root"
    assert assistant.metadata["parent_uuid"] == "turn-user-1"
    assert assistant.metadata["prompt_id"] == "prompt-1"
    assert assistant.metadata["source_tool_assistant_uuid"] == "turn-tool"
    assert assistant.metadata["folded_tool_result_count"] == 1
    assert assistant.metadata["source_platform"] == "claude_code"
    assert assistant.occurred_at == datetime(2026, 5, 29, 4, 53, tzinfo=UTC)
    sidechain = batch.records[2]
    assert sidechain.metadata["parent_adapter_record_id"] == "session.jsonl:turn-assistant-1"


@pytest.mark.asyncio
async def test_codex_adapter_pairs_tool_calls_and_resumes(tmp_path: Path) -> None:
    transcript = _write_jsonl(
        tmp_path / "rollout-2026-05-30T00-00-00-session.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-05-30T00:00:00Z",
                "payload": {
                    "id": "session-1",
                    "timestamp": "2026-05-30T00:00:00Z",
                    "cwd": "/Users/bliss/dev/sibyl",
                    "source": {"subagent": "review"},
                    "thread_source": {
                        "parent_uuid": "session-parent",
                        "forked_from": "session-fork",
                    },
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-05-30T00:00:01Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "parent_uuid": "codex-parent",
                    "forked_from": "codex-fork",
                    "content": [{"type": "input_text", "text": "ship it"}],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-05-30T00:00:02Z",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": '{"cmd": "git status"}',
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-05-30T00:00:03Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "clean",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-05-30T00:00:04Z",
                "payload": {
                    "type": "agent_message",
                    "message": "done",
                    "phase": "commentary",
                },
            },
        ],
    )
    adapter = CodexJsonlAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(transcript))

    first = await anext(adapter.iter_records(manifest, batch_size=2))
    second = await anext(adapter.iter_records(manifest, checkpoint=first.checkpoint, batch_size=2))

    assert [record.adapter_record_id for record in first.records] == [
        "rollout-2026-05-30T00-00-00-session.jsonl:session-1:message:1",
        "rollout-2026-05-30T00-00-00-session.jsonl:session-1:tool:call-1",
    ]
    assert first.records[0].metadata["parent_uuid"] == "codex-parent"
    assert first.records[0].metadata["forked_from"] == "codex-fork"
    assert first.records[1].source_type == "agent_tool_call"
    assert first.records[1].metadata["tool_name"] == "exec_command"
    assert "clean" in first.records[1].body
    assert first.checkpoint.cursor == "2"
    assert second.records[0].body == "done"
    assert second.records[0].metadata["source_subagent"] == "review"
    assert second.records[0].metadata["parent_uuid"] == "session-parent"
    assert second.records[0].metadata["forked_from"] == "session-fork"
    assert second.checkpoint.done is True


@pytest.mark.asyncio
async def test_codex_history_adapter_imports_prompts(tmp_path: Path) -> None:
    history = _write_jsonl(
        tmp_path / "history.jsonl",
        [{"session_id": "session-1", "ts": 1773094092, "text": "audit startup"}],
    )
    adapter = CodexJsonlAdapter()

    manifest = await adapter.prepare_manifest(source_uri=str(history))
    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    record = batch.records[0]
    assert record.adapter_record_id == "history.jsonl:session-1:history:0"
    assert record.title.startswith("codex user turn")
    assert record.body == "audit startup"
    assert record.metadata["source_event_type"] == "history_prompt"


@pytest.mark.asyncio
async def test_transcript_adapter_rejects_symlinked_jsonl(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}", encoding="utf-8")
    symlink = import_root / "linked.jsonl"
    try:
        symlink.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    adapter = ClaudeCodeJsonlAdapter()

    with pytest.raises(ValueError, match="symlinked entries"):
        await adapter.prepare_manifest(source_uri=str(import_root))


@pytest.mark.asyncio
async def test_transcript_adapter_rejects_symlinked_directories(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jsonl").write_text("{}", encoding="utf-8")
    symlink = import_root / "linked"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    adapter = CodexJsonlAdapter()

    with pytest.raises(ValueError, match="symlinked entries"):
        await adapter.prepare_manifest(source_uri=str(import_root))


@pytest.mark.asyncio
async def test_transcript_adapter_ids_include_nested_file_identity(
    tmp_path: Path,
) -> None:
    for directory in (tmp_path / "a", tmp_path / "b"):
        directory.mkdir()
        _write_jsonl(
            directory / "history.jsonl",
            [{"session_id": "session-1", "ts": 1773094092, "text": "audit startup"}],
        )
    adapter = CodexJsonlAdapter()

    manifest = await adapter.prepare_manifest(source_uri=str(tmp_path))
    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    ids = [record.adapter_record_id for record in batch.records]
    assert ids == [
        "a/history.jsonl:session-1:history:0",
        "b/history.jsonl:session-1:history:0",
    ]
    assert len({record.source_id for record in batch.records}) == 2


@pytest.mark.asyncio
async def test_codex_adapter_emits_completed_custom_tool_call_in_place(
    tmp_path: Path,
) -> None:
    transcript = _write_jsonl(
        tmp_path / "rollout.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-05-30T00:00:00Z",
                "payload": {"id": "session-1", "cwd": "/Users/bliss/dev/sibyl"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-05-30T00:00:02Z",
                "payload": {
                    "type": "custom_tool_call",
                    "status": "completed",
                    "name": "apply_patch",
                    "call_id": "call-apply",
                    "input": "*** Begin Patch\n*** End Patch\n",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-05-30T00:00:03Z",
                "payload": {
                    "type": "agent_message",
                    "message": "patched",
                    "phase": "commentary",
                },
            },
        ],
    )
    adapter = CodexJsonlAdapter()

    manifest = await adapter.prepare_manifest(source_uri=str(transcript))
    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    tool_record = batch.records[0]
    assert tool_record.adapter_record_id == "rollout.jsonl:session-1:tool:call-apply"
    assert tool_record.source_uri == f"{transcript.resolve()}#line=2"
    assert tool_record.occurred_at == datetime(2026, 5, 30, 0, 0, 2, tzinfo=UTC)
    assert "apply_patch" in tool_record.body
    assert batch.records[1].body == "patched"


@pytest.mark.asyncio
async def test_transcript_resume_rejects_source_version_mismatch(
    tmp_path: Path,
) -> None:
    transcript = _write_jsonl(
        tmp_path / "history.jsonl",
        [{"session_id": "session-1", "ts": 1773094092, "text": "first"}],
    )
    adapter = CodexJsonlAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(transcript))
    first = await anext(adapter.iter_records(manifest, batch_size=1))
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"session_id": "session-1", "ts": 1773094092, "text": "first"}),
                json.dumps({"session_id": "session-1", "ts": 1773094093, "text": "second"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    changed_manifest = await adapter.prepare_manifest(source_uri=str(transcript))

    with pytest.raises(ValueError, match="checkpoint_source_version_mismatch"):
        await anext(
            adapter.iter_records(
                changed_manifest,
                checkpoint=first.checkpoint,
                batch_size=1,
            )
        )


@pytest.mark.asyncio
async def test_codex_history_adapter_reports_malformed_rows(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        'not json\n{"session_id": "session-1", "ts": 1773094092, "text": "ok"}\n',
        encoding="utf-8",
    )
    adapter = CodexJsonlAdapter()

    manifest = await adapter.prepare_manifest(source_uri=str(history))
    batch = await anext(adapter.iter_records(manifest, batch_size=10))

    assert len(batch.records) == 1
    assert len(batch.skipped) == 1
    assert batch.skipped[0].reason == "json_parse_failed"


@pytest.mark.asyncio
async def test_transcript_import_writes_private_raw_records(tmp_path: Path) -> None:
    transcript = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            {
                "type": "user",
                "uuid": "turn-user-1",
                "sessionId": "session-1",
                "message": {"role": "user", "content": "remember this"},
            }
        ],
    )
    adapter = ClaudeCodeJsonlAdapter()
    manifest = await adapter.prepare_manifest(source_uri=str(transcript))
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return RawMemory(
            id="raw-1",
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
            captured_at=datetime(2026, 5, 30, tzinfo=UTC),
            created_at=datetime(2026, 5, 30, tzinfo=UTC),
        )

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=fake_remember,
    )

    assert result.imported_count == 1
    assert writes[0]["memory_scope"] is MemoryScope.PRIVATE
    assert writes[0]["capture_surface"] == "source_import"
    metadata = writes[0]["metadata"]
    assert metadata["source_type"] == "agent_transcript_turn"
    assert metadata["source_record_metadata"]["source_platform"] == "claude_code"


@pytest.mark.asyncio
async def test_codex_dogfood_fixture_reimport_is_turn_level_idempotent() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "transcripts" / "codex_dogfood"
    adapter = CodexJsonlAdapter()
    manifest = await adapter.prepare_manifest(
        source_uri=str(fixture_dir),
        options={"source_identity": "dogfood:codex:sanitized"},
    )
    rememberer = InMemoryRawMemoryRememberer()

    first = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=rememberer,
        duplicate_checker=rememberer.duplicate_checker,
    )
    second = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=rememberer,
        duplicate_checker=rememberer.duplicate_checker,
    )

    assert first.imported_count == 4
    assert first.dedupe_count == 0
    assert second.imported_count == 0
    assert second.dedupe_count == 4
    assert len(rememberer.memories) == 4
    assert set(second.duplicate_dedupe_keys) == set(first.dedupe_keys)
    skipped_source_ids = {str(record.metadata["source_id"]) for record in second.skipped_records}
    assert skipped_source_ids == set(first.source_ids)
    assert [
        memory.metadata["source_record_metadata"]["source_platform"]
        for memory in rememberer.memories
    ] == [
        "codex",
        "codex",
        "codex",
        "codex",
    ]


@pytest.mark.asyncio
async def test_transcript_import_requires_preview_for_wider_scope(
    tmp_path: Path,
) -> None:
    transcript = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            {
                "type": "user",
                "uuid": "turn-user-1",
                "sessionId": "session-1",
                "message": {"role": "user", "content": "remember this"},
            }
        ],
    )
    adapter = ClaudeCodeJsonlAdapter()
    manifest = await adapter.prepare_manifest(
        source_uri=str(transcript),
        options={"privacy_class": "public", "target_memory_scope": "organization"},
    )
    assert manifest.privacy_class is SourcePrivacyClass.PRIVATE

    async def fake_remember(**kwargs: object) -> RawMemory:
        raise AssertionError("preview-blocked imports should not write raw memory")

    with pytest.raises(ValueError, match="promotion preview"):
        await import_source_batch(
            adapter,
            manifest,
            organization_id="org-1",
            principal_id="user-1",
            remember=fake_remember,
        )


def test_ensure_transcript_adapters_registers_once() -> None:
    ensure_transcript_adapters_registered()
    ensure_transcript_adapters_registered()

    claude = get_source_adapter(CLAUDE_CODE_ADAPTER_NAME)
    codex = get_source_adapter(CODEX_ADAPTER_NAME)

    assert isinstance(claude, ClaudeCodeJsonlAdapter)
    assert isinstance(codex, CodexJsonlAdapter)
