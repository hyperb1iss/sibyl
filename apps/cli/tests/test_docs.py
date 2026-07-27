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


def _import_status(
    *,
    status: str = "pending",
    imported_count: int = 0,
    adapter_name: str = "document_url",
    source_identity: str = "https://docs.example.com/page",
) -> dict[str, object]:
    return {
        "import_id": "import/run:1",
        "status": status,
        "adapter_name": adapter_name,
        "source_identity": source_identity,
        "target_memory_scope": "project",
        "target_scope_key": "project_123",
        "progress": {
            "imported_count": imported_count,
            "dedupe_count": 0,
            "skipped_count": 0,
            "error_count": 0,
        },
        "raw_memory_ids": ["raw-1"] if imported_count else [],
    }


@pytest.mark.asyncio
async def test_start_document_import_client_posts_ingestion_payload() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    client._request = AsyncMock(return_value={"import_id": "import-1"})  # type: ignore[method-assign]

    data = await client.start_document_import(
        kind="url",
        source_uri="https://docs.example.com/page",
        collection="docs",
        target_scope_key="project_123",
        batch_size=50,
        promotion_preview_approved=True,
        allow_private_network=True,
    )

    assert data == {"import_id": "import-1"}
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "POST",
        "/ingestion/documents",
        json={
            "kind": "url",
            "source_uri": "https://docs.example.com/page",
            "text": None,
            "title": None,
            "collection": "docs",
            "target_scope_key": "project_123",
            "batch_size": 50,
            "promotion_preview_approved": True,
            "allow_private_network": True,
        },
    )


@pytest.mark.asyncio
async def test_list_document_collections_client_gets_endpoint() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    client._request = AsyncMock(return_value={"collections": []})  # type: ignore[method-assign]

    data = await client.list_document_collections()

    assert data == {"collections": []}
    client._request.assert_awaited_once_with("GET", "/ingestion/collections")  # type: ignore[attr-defined]


@patch("sibyl_cli.document.get_client")
@patch("sibyl_cli.document.resolve_project_from_cwd", return_value="project_123")
def test_docs_add_url_starts_project_import(
    mock_resolve_project: MagicMock,
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.start_document_import = AsyncMock(return_value=_import_status())
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        [
            "docs",
            "add",
            "https://docs.example.com/page",
            "--collection",
            "docs",
            "--allow-private-network",
        ],
    )

    assert result.exit_code == 0
    assert "Document import queued" in result.stdout
    mock_resolve_project.assert_called_once()
    mock_client.start_document_import.assert_awaited_once_with(
        kind="url",
        source_uri="https://docs.example.com/page",
        collection="docs",
        target_scope_key="project_123",
        batch_size=100,
        promotion_preview_approved=False,
        allow_private_network=True,
    )


@patch("sibyl_cli.document.get_client")
@patch("sibyl_cli.document.resolve_project_from_cwd", return_value="project_123")
def test_docs_add_url_defaults_private_network_disallowed(
    mock_resolve_project: MagicMock,
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.start_document_import = AsyncMock(return_value=_import_status())
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(app, ["docs", "add", "https://docs.example.com/page"])

    assert result.exit_code == 0
    mock_resolve_project.assert_called_once()
    mock_client.start_document_import.assert_awaited_once_with(
        kind="url",
        source_uri="https://docs.example.com/page",
        collection=None,
        target_scope_key="project_123",
        batch_size=100,
        promotion_preview_approved=False,
        allow_private_network=False,
    )


@patch("sibyl_cli.document.get_client")
@patch("sibyl_cli.document.resolve_project_from_cwd", return_value="project_123")
def test_docs_add_file_starts_project_import(
    mock_resolve_project: MagicMock,
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    source = tmp_path / "guide.md"
    source.write_text("# Guide\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.start_document_import = AsyncMock(
        return_value=_import_status(
            adapter_name="document_file",
            source_identity=str(source.resolve()),
        )
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(app, ["docs", "add", str(source)])

    assert result.exit_code == 0
    mock_resolve_project.assert_called_once()
    mock_client.start_document_import.assert_awaited_once_with(
        kind="file",
        source_uri=str(source.resolve()),
        collection=None,
        target_scope_key="project_123",
        batch_size=100,
        promotion_preview_approved=False,
        allow_private_network=False,
    )


@patch("sibyl_cli.document.get_client")
@patch("sibyl_cli.document.resolve_project_from_cwd", return_value="project_123")
def test_docs_add_recursive_folder_starts_project_import(
    mock_resolve_project: MagicMock,
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    (source / "guide.md").write_text("# Guide\n", encoding="utf-8")
    mock_client = MagicMock()
    mock_client.start_document_import = AsyncMock(
        return_value=_import_status(
            adapter_name="document_folder",
            source_identity=str(source.resolve()),
        )
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        ["docs", "add", str(source), "--recursive", "--collection", "docs"],
    )

    assert result.exit_code == 0
    mock_resolve_project.assert_called_once()
    mock_client.start_document_import.assert_awaited_once_with(
        kind="folder",
        source_uri=str(source.resolve()),
        collection="docs",
        target_scope_key="project_123",
        batch_size=100,
        promotion_preview_approved=False,
        allow_private_network=False,
    )


@patch("sibyl_cli.document.get_client")
@patch("sibyl_cli.document.resolve_project_from_cwd", return_value="project_123")
def test_docs_add_drain_polls_until_completed(
    mock_resolve_project: MagicMock,
    mock_get_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.start_document_import = AsyncMock(return_value=_import_status(status="running"))
    mock_client.ingestion_source_import_status = AsyncMock(
        return_value=_import_status(status="completed", imported_count=2)
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        [
            "docs",
            "add",
            "https://docs.example.com/page",
            "--drain",
            "--poll-interval",
            "0",
            "--timeout",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert "Document import completed" in result.stdout
    assert "raw-1" in result.stdout
    mock_resolve_project.assert_called_once()
    mock_client.ingestion_source_import_status.assert_awaited_once_with("import/run:1")


@pytest.mark.parametrize("terminal_status", ["failed", "canceled"])
@patch("sibyl_cli.document.get_client")
@patch("sibyl_cli.document.resolve_project_from_cwd", return_value="project_123")
def test_docs_add_drain_failure_statuses_return_nonzero(
    mock_resolve_project: MagicMock,
    mock_get_client: MagicMock,
    terminal_status: str,
) -> None:
    mock_client = MagicMock()
    mock_client.start_document_import = AsyncMock(return_value=_import_status(status="running"))
    mock_client.ingestion_source_import_status = AsyncMock(
        return_value=_import_status(status=terminal_status)
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        [
            "docs",
            "add",
            "https://docs.example.com/page",
            "--drain",
            "--poll-interval",
            "0",
            "--timeout",
            "1",
        ],
    )

    assert result.exit_code == 1
    assert f"Document import {terminal_status}" in result.stdout
    mock_resolve_project.assert_called_once()


@pytest.mark.parametrize("terminal_status", ["failed", "canceled"])
@patch("sibyl_cli.document.get_client")
@patch("sibyl_cli.document.resolve_project_from_cwd", return_value="project_123")
def test_docs_add_json_drain_failure_statuses_return_nonzero(
    mock_resolve_project: MagicMock,
    mock_get_client: MagicMock,
    terminal_status: str,
) -> None:
    mock_client = MagicMock()
    mock_client.start_document_import = AsyncMock(return_value=_import_status(status="running"))
    mock_client.ingestion_source_import_status = AsyncMock(
        return_value=_import_status(status=terminal_status)
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        [
            "docs",
            "add",
            "https://docs.example.com/page",
            "--drain",
            "--poll-interval",
            "0",
            "--timeout",
            "1",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert f'"status": "{terminal_status}"' in result.stdout
    assert f"Document import {terminal_status}" not in result.stdout
    mock_resolve_project.assert_called_once()


@patch("sibyl_cli.document.get_client")
def test_docs_add_directory_requires_recursive(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(app, ["docs", "add", str(tmp_path)])

    assert result.exit_code == 1
    assert "require --recursive" in result.stdout
    mock_get_client.assert_not_called()


@patch("sibyl_cli.document.get_client")
def test_docs_add_rejects_direct_symlink(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    real_file = tmp_path / "real.md"
    real_file.write_text("# Guide\n", encoding="utf-8")
    source = tmp_path / "linked.md"
    source.symlink_to(real_file)

    result = CliRunner().invoke(app, ["docs", "add", str(source)])

    assert result.exit_code == 1
    assert "cannot include symlinks" in result.stdout
    mock_get_client.assert_not_called()


@patch("sibyl_cli.document.get_client")
def test_docs_add_rejects_parent_symlink(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir)
    source = linked_dir / "guide.md"

    result = CliRunner().invoke(app, ["docs", "add", str(source)])

    assert result.exit_code == 1
    assert "cannot include symlinks" in result.stdout
    mock_get_client.assert_not_called()


@patch("sibyl_cli.document.get_client")
def test_docs_add_rejects_folder_symlink_entries(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    real_file = source / "real.md"
    real_file.write_text("# Guide\n", encoding="utf-8")
    (source / "linked.md").symlink_to(real_file)

    result = CliRunner().invoke(app, ["docs", "add", str(source), "--recursive"])

    assert result.exit_code == 1
    assert "cannot include symlinks" in result.stdout
    mock_get_client.assert_not_called()


@patch("sibyl_cli.document.get_client")
@patch("sibyl_cli.document.resolve_project_reference", new_callable=AsyncMock)
def test_docs_paste_reads_file_and_project_option(
    mock_resolve_project: AsyncMock,
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    source = tmp_path / "note.md"
    source.write_text("Launch notes", encoding="utf-8")
    mock_resolve_project.return_value = "project_abc"
    mock_client = MagicMock()
    mock_client.start_document_import = AsyncMock(
        return_value={**_import_status(status="completed"), "adapter_name": "document_text"}
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(
        app,
        [
            "docs",
            "paste",
            "--file",
            str(source),
            "--title",
            "Launch notes",
            "--project",
            "sibyl",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"status": "completed"' in result.stdout
    mock_resolve_project.assert_awaited_once_with(mock_client, "sibyl")
    mock_client.start_document_import.assert_awaited_once_with(
        kind="text",
        text="Launch notes",
        title="Launch notes",
        collection=None,
        target_scope_key="project_abc",
        batch_size=100,
        promotion_preview_approved=False,
    )


@patch("sibyl_cli.document.get_client")
def test_docs_paste_reports_file_read_errors(
    mock_get_client: MagicMock,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.md"

    result = CliRunner().invoke(app, ["docs", "paste", "--file", str(missing)])

    assert result.exit_code == 1
    assert "Content file not found" in result.stdout
    mock_get_client.assert_not_called()


@patch("sibyl_cli.document.get_client")
def test_docs_list_collections(mock_get_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.list_document_collections = AsyncMock(
        return_value={
            "collections": [
                {
                    "name": "docs",
                    "document_count": 3,
                    "updated_at": "2026-05-14T12:00:00Z",
                }
            ]
        }
    )
    mock_get_client.return_value = _FakeClientContext(mock_client)

    result = CliRunner().invoke(app, ["docs", "list"])

    assert result.exit_code == 0
    assert "docs" in result.stdout
    assert "3" in result.stdout
    mock_client.list_document_collections.assert_awaited_once_with()
