"""Tests for legacy source CLI commands."""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli import crawl, source


class TestSourceCliCompatibility:
    """Legacy source commands should use the crawl-source backend."""

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_source_add_uses_crawl_source_api(self, mock_get_client: MagicMock) -> None:
        """source add should create a relational crawl source, not a graph entity."""
        mock_client = MagicMock()
        mock_client.create_crawl_source = AsyncMock(return_value={"id": "src_123"})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            source.app,
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
    def test_source_add_accepts_include_alias(self, mock_get_client: MagicMock) -> None:
        """source add should accept the same include-pattern flag as crawl add."""
        mock_client = MagicMock()
        mock_client.create_crawl_source = AsyncMock(return_value={"id": "src_123"})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(
            source.app,
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
    def test_source_status_uses_crawl_source_endpoints(self, mock_get_client: MagicMock) -> None:
        """source status should read from crawl source and crawl status APIs."""
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
        result = runner.invoke(source.app, ["status", "src_123", "--json"])

        assert result.exit_code == 0
        mock_client.get_crawl_source.assert_called_once_with("src_123")
        mock_client.get_crawl_status.assert_called_once_with("src_123")

    @patch("sibyl_cli.source.get_client")
    def test_source_documents_use_crawl_documents_api(self, mock_get_client: MagicMock) -> None:
        """source documents should use the crawler document listing endpoint."""
        mock_client = MagicMock()
        mock_client.list_crawl_documents = AsyncMock(return_value={"documents": []})
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(source.app, ["documents", "src_123", "--json"])

        assert result.exit_code == 0
        mock_client.list_crawl_documents.assert_called_once_with(source_id="src_123", limit=50)

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_source_crawl_accepts_queued_status(self, mock_get_client: MagicMock) -> None:
        """source crawl should treat queued jobs as success."""
        mock_client = MagicMock()
        mock_client.start_crawl = AsyncMock(
            return_value={"status": "queued", "message": "Crawl job queued"}
        )
        mock_get_client.return_value = mock_client

        runner = CliRunner()
        result = runner.invoke(source.app, ["crawl", "src_123"])

        assert result.exit_code == 0
        mock_client.start_crawl.assert_called_once_with(
            source_id="src_123",
            max_pages=50,
            max_depth=3,
            generate_embeddings=True,
        )

    @patch("sibyl_cli.source.get_client")
    def test_source_link_graph_can_create_new_entities(
        self, mock_get_client: MagicMock
    ) -> None:
        """source link-graph should forward the create-new flag to the API."""
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
        result = runner.invoke(source.app, ["link-graph", "src_123", "--create-new"])

        assert result.exit_code == 0
        mock_client.link_graph.assert_called_once_with(
            source_id="src_123",
            batch_size=50,
            dry_run=False,
            create_new_entities=True,
        )
        assert "New entities created" in result.stdout


class TestCrawlCliQueuedStatus:
    """Crawler ingest CLI should accept the API's queued success status."""

    @patch("sibyl_cli.crawl_shared.get_client")
    def test_ingest_treats_queued_as_success(self, mock_get_client: MagicMock) -> None:
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
    def test_status_uses_source_status_contract(self, mock_get_client: MagicMock) -> None:
        """crawl status should reuse the current source status API contract."""
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
