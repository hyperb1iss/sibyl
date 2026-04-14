"""Tests for crawl/document CLI commands."""

from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli import crawl
from sibyl_cli.client import SibylClientError


class TestCrawlCliSurface:
    """Canonical crawl commands should own source management."""

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_crawl_add_uses_crawl_source_api(self, mock_get_client: MagicMock) -> None:
        """crawl add should create a relational crawl source."""
        mock_client = MagicMock()
        mock_client.create_crawl_source = AsyncMock(return_value={"id": "src_123"})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            crawl.app,
            ["add", "https://docs.example.com", "--name", "Example Docs"],
        )

        assert result.exit_code == 0
        mock_client.create_crawl_source.assert_called_once_with(
            name="Example Docs",
            url="https://docs.example.com",
            source_type="website",
            crawl_depth=2,
            include_patterns=[],
        )

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_crawl_show_uses_crawl_source_api(self, mock_get_client: MagicMock) -> None:
        """crawl show should use the crawl source endpoint."""
        mock_client = MagicMock()
        mock_client.get_crawl_source = AsyncMock(
            return_value={
                "id": "src_123",
                "name": "Example Docs",
                "url": "https://docs.example.com",
                "crawl_status": "completed",
                "document_count": 12,
                "chunk_count": 24,
            }
        )
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(crawl.app, ["show", "src_123", "--json"])

        assert result.exit_code == 0
        mock_client.get_crawl_source.assert_called_once_with("src_123")

    @patch("sibyl_cli.document.get_client")
    def test_crawl_documents_list_uses_crawl_documents_api(
        self, mock_get_client: MagicMock
    ) -> None:
        """crawl documents list should use the crawler document listing endpoint."""
        mock_client = MagicMock()
        mock_client.list_crawl_documents = AsyncMock(return_value={"documents": []})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            crawl.app,
            ["documents", "list", "--source", "src_123", "--json"],
        )

        assert result.exit_code == 0
        mock_client.list_crawl_documents.assert_called_once_with(source_id="src_123", limit=20)

    @patch("sibyl_cli.document.get_client")
    def test_crawl_documents_show_keeps_document_specific_404_message(
        self, mock_get_client: MagicMock
    ) -> None:
        """crawl documents show should preserve its document-specific not-found wording."""
        mock_client = MagicMock()
        mock_client.get_crawl_document = AsyncMock(
            side_effect=SibylClientError(
                "Document missing",
                status_code=404,
                detail="doc_123",
            )
        )
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(crawl.app, ["documents", "show", "doc_123"])

        assert result.exit_code == 1
        assert "Document not found: doc_123" in result.stdout

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_crawl_ingest_treats_queued_as_success(self, mock_get_client: MagicMock) -> None:
        """crawl ingest should not report queued jobs as failures."""
        mock_client = MagicMock()
        mock_client.start_crawl = AsyncMock(
            return_value={"status": "queued", "message": "Crawl job queued"}
        )
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(crawl.app, ["ingest", "src_123"])

        assert result.exit_code == 0
        mock_client.start_crawl.assert_called_once_with(
            source_id="src_123",
            max_pages=50,
            max_depth=3,
            generate_embeddings=True,
        )

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_crawl_status_uses_crawl_source_endpoints(self, mock_get_client: MagicMock) -> None:
        """crawl status should read from crawl source and crawl status APIs."""
        mock_client = MagicMock()
        mock_client.get_crawl_source = AsyncMock(
            return_value={
                "id": "src_123",
                "name": "Example Docs",
                "url": "https://docs.example.com",
                "crawl_status": "completed",
                "document_count": 12,
                "chunk_count": 24,
            }
        )
        mock_client.get_crawl_status = AsyncMock(
            return_value={
                "crawl_status": "completed",
                "document_count": 12,
                "chunk_count": 24,
                "current_job_id": None,
                "last_crawled_at": "2026-04-13T12:00:00",
                "last_error": None,
            }
        )
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(crawl.app, ["status", "src_123", "--json"])

        assert result.exit_code == 0
        mock_client.get_crawl_source.assert_called_once_with("src_123")
        mock_client.get_crawl_status.assert_called_once_with("src_123")

    @patch("sibyl_cli.crawl.get_client")
    def test_crawl_link_graph_can_create_new_entities(
        self, mock_get_client: MagicMock
    ) -> None:
        """crawl link-graph should forward the create-new flag to the API."""
        mock_client = MagicMock()
        mock_client.link_graph = AsyncMock(
            return_value={
                "status": "completed",
                "chunks_processed": 3,
                "entities_extracted": 5,
                "entities_linked": 2,
                "new_entities_created": 2,
                "chunks_remaining": 0,
            }
        )
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            crawl.app,
            [
                "link-graph",
                "src_123",
                "--create-new",
            ],
        )

        assert result.exit_code == 0
        mock_client.link_graph.assert_called_once_with(
            source_id="src_123",
            batch_size=50,
            dry_run=False,
            create_new_entities=True,
        )
        assert "New entities created" in result.stdout

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_crawl_show_keeps_conflict_message(self, mock_get_client: MagicMock) -> None:
        """crawl show should preserve conflict wording through the shared handler."""
        mock_client = MagicMock()
        mock_client.get_crawl_source = AsyncMock(
            side_effect=SibylClientError(
                "Conflict",
                status_code=409,
                detail="source is busy",
            )
        )
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(crawl.app, ["show", "src_123"])

        assert result.exit_code == 1
        assert "Conflict: source is busy" in result.stdout


class TestCrawlCliAddFlags:
    """Crawler add CLI should accept both include flag spellings."""

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_add_accepts_include_alias(self, mock_get_client: MagicMock) -> None:
        """crawl add should forward --include to include_patterns."""
        mock_client = MagicMock()
        mock_client.create_crawl_source = AsyncMock(return_value={"id": "src_123"})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            crawl.app,
            [
                "add",
                "https://docs.example.com",
                "--name",
                "Example Docs",
                "--include",
                "docs/**",
                "--include",
                "guides/**",
            ],
        )

        assert result.exit_code == 0
        mock_client.create_crawl_source.assert_called_once_with(
            name="Example Docs",
            url="https://docs.example.com",
            source_type="website",
            crawl_depth=2,
            include_patterns=["docs/**", "guides/**"],
        )

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_add_still_accepts_pattern_flag(self, mock_get_client: MagicMock) -> None:
        """crawl add should keep the legacy --pattern spelling working."""
        mock_client = MagicMock()
        mock_client.create_crawl_source = AsyncMock(return_value={"id": "src_123"})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            crawl.app,
            [
                "add",
                "https://docs.example.com",
                "--name",
                "Example Docs",
                "--pattern",
                "docs/**",
            ],
        )

        assert result.exit_code == 0
        mock_client.create_crawl_source.assert_called_once_with(
            name="Example Docs",
            url="https://docs.example.com",
            source_type="website",
            crawl_depth=2,
            include_patterns=["docs/**"],
        )


def test_main_help_omits_source_group() -> None:
    runner = CliRunner()
    main_cli = import_module("sibyl_cli.main")

    result = runner.invoke(main_cli.app, ["--help"])

    assert result.exit_code == 0
    assert "│ crawl " in result.stdout
    assert "│ document " not in result.stdout
    assert "│ source " not in result.stdout


def test_crawl_help_shows_nested_documents_group() -> None:
    runner = CliRunner()

    result = runner.invoke(crawl.app, ["--help"])

    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "documents" in result.stdout
