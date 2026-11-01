from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli.client import SibylClient
from sibyl_cli.main import app


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def _import_status(*, status: str = "pending", imported_count: int = 0) -> dict[str, object]:
    return {
        "import_id": "import/run:1",
        "status": status,
        "adapter_name": "claude_code_jsonl",
        "source_identity": "/tmp/transcripts",
        "target_memory_scope": "private",
        "target_scope_key": None,
        "progress": {
            "imported_count": imported_count,
            "dedupe_count": 0,
            "skipped_count": 0,
            "error_count": 0,
        },
        "raw_memory_ids": ["raw-1"] if imported_count else [],
    }


@pytest.mark.asyncio
async def test_start_source_import_client_posts_ingestion_payload() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    client._request = AsyncMock(return_value={"import_id": "import-1"})  # type: ignore[method-assign]

    data = await client.start_source_import(
        source_uri="/imports/session",
        adapter_name="claude_code_jsonl",
        target_memory_scope="project",
        target_scope_key="project-1",
        options={"source_identity": "session-export"},
        batch_size=50,
        promotion_preview_approved=True,
    )

    assert data == {"import_id": "import-1"}
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "POST",
        "/ingestion/imports",
        json={
            "source_uri": "/imports/session",
            "adapter_name": "claude_code_jsonl",
            "target_memory_scope": "project",
            "target_scope_key": "project-1",
            "options": {"source_identity": "session-export"},
            "batch_size": 50,
            "promotion_preview_approved": True,
        },
    )


@patch("sibyl_cli.ingest.get_client")
def test_ingest_claude_code_starts_private_import(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.start_source_import = AsyncMock(return_value=_import_status())
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(app, ["ingest", "claude-code", str(transcript)])

    assert result.exit_code == 0
    assert "Import queued" in result.stdout
    assert "claude_code_jsonl" in result.stdout
    mock_client.start_source_import.assert_awaited_once_with(
        source_uri=str(transcript.resolve()),
        adapter_name="claude_code_jsonl",
        target_memory_scope="private",
        target_scope_key=None,
        options={},
        batch_size=100,
        promotion_preview_approved=False,
    )
    mock_client.source_import_status.assert_not_called()


@patch("sibyl_cli.ingest.get_client")
def test_ingest_codex_drain_polls_until_completed(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    transcript_dir = tmp_path / "codex"
    transcript_dir.mkdir()
    (transcript_dir / "rollout.jsonl").write_text("{}\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.start_source_import = AsyncMock(
        return_value={
            **_import_status(status="running"),
            "adapter_name": "codex_jsonl",
        }
    )
    mock_client.source_import_status = AsyncMock(
        side_effect=[
            {**_import_status(status="running"), "adapter_name": "codex_jsonl"},
            {**_import_status(status="completed", imported_count=2), "adapter_name": "codex_jsonl"},
        ]
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "codex",
            str(transcript_dir),
            "--drain",
            "--poll-interval",
            "0",
            "--timeout",
            "1",
            "--source-identity",
            "codex-export",
        ],
    )

    assert result.exit_code == 0
    assert "Import completed" in result.stdout
    assert "codex_jsonl" in result.stdout
    assert "raw-1" in result.stdout
    mock_client.start_source_import.assert_awaited_once_with(
        source_uri=str(transcript_dir.resolve()),
        adapter_name="codex_jsonl",
        target_memory_scope="private",
        target_scope_key=None,
        options={"source_identity": "codex-export"},
        batch_size=100,
        promotion_preview_approved=False,
    )
    assert mock_client.source_import_status.await_count == 2


@patch("sibyl_cli.ingest.get_client")
def test_ingest_drain_fails_when_import_fails(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.start_source_import = AsyncMock(return_value=_import_status(status="running"))
    mock_client.source_import_status = AsyncMock(return_value=_import_status(status="failed"))
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "claude-code",
            str(transcript),
            "--drain",
            "--poll-interval",
            "0",
            "--timeout",
            "1",
        ],
    )

    assert result.exit_code == 1
    assert "Import failed" in result.stdout
    assert "Import queued" not in result.stdout


@patch("sibyl_cli.ingest.get_client")
def test_ingest_json_drain_failure_returns_nonzero_with_json(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.start_source_import = AsyncMock(return_value=_import_status(status="running"))
    mock_client.source_import_status = AsyncMock(return_value=_import_status(status="failed"))
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        [
            "ingest",
            "claude-code",
            str(transcript),
            "--drain",
            "--poll-interval",
            "0",
            "--timeout",
            "1",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert '"status": "failed"' in result.stdout
    assert "Import failed" not in result.stdout


@patch("sibyl_cli.ingest.get_client")
def test_ingest_rejects_symlink_source(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    source = tmp_path / "linked-session.jsonl"
    source.symlink_to(transcript)

    result = CliRunner().invoke(app, ["ingest", "claude-code", str(source)])

    assert result.exit_code == 1
    assert "cannot be a symlink" in result.stdout
    mock_get_client.assert_not_called()
