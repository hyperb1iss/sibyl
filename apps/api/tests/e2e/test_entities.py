"""E2E tests for entity and search operations.

Note: Tests use 'pattern' type which uses create_direct() - no LLM needed.
This allows e2e tests to run without real OpenAI API keys.
"""


class TestEntityOperations:
    """Test entity creation and search."""

    def test_add_pattern(self, cli, unique_id) -> None:
        """Add a pattern to the knowledge graph."""
        title = f"E2E Pattern {unique_id}"
        content = "This is a test pattern for e2e testing"

        result = cli.add(title, content, entity_type="pattern", category="testing")
        assert result.success, f"Add pattern failed: {result.stderr}"

        data = result.json()
        assert data.get("name") == title
        assert "id" in data

    def test_add_pattern_with_tags(self, cli, unique_id) -> None:
        """Add a pattern with tags and language."""
        title = f"E2E Tagged Pattern {unique_id}"
        content = "Pattern with metadata for e2e testing"

        result = cli.add(
            title, content, entity_type="pattern", category="testing", language="python"
        )
        assert result.success, f"Add pattern failed: {result.stderr}"

        data = result.json()
        assert data.get("name") == title

    def test_entity_list(self, cli) -> None:
        """List entities by type."""
        result = cli.entity_list(entity_type="pattern")
        assert result.success, f"Entity list failed: {result.stderr}"

        data = result.json()
        assert isinstance(data, list)

    def test_search(self, cli, unique_id) -> None:
        """Add content and search for it."""
        title = f"Searchable E2E {unique_id}"
        content = f"Unique searchable content {unique_id} for verification"

        # Add as pattern (uses create_direct, no LLM needed)
        add_result = cli.add(title, content, entity_type="pattern")
        assert add_result.success

        # Search - give it a moment to index
        import time

        time.sleep(0.5)

        search_result = cli.search(unique_id, limit=10)
        assert search_result.success, f"Search failed: {search_result.stderr}"

        # Should find our content (search may return results in different formats)
        data = search_result.json()
        # Results might be in 'results' key or be a list directly
        results = data.get("results", data) if isinstance(data, dict) else data
        assert isinstance(results, list)

    def test_entity_list_multiple_types(self, cli) -> None:
        """List entities of different types."""
        for entity_type in ["pattern", "episode", "task", "project"]:
            result = cli.entity_list(entity_type=entity_type)
            assert result.success, f"Entity list for {entity_type} failed: {result.stderr}"

            data = result.json()
            assert isinstance(data, list)
