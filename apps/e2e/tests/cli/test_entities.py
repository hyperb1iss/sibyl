"""E2E tests for entity and search operations.

Note: Tests use 'pattern' type which uses create_direct() - no LLM needed.
This allows e2e tests to run without real OpenAI API keys.
"""

import pytest


@pytest.mark.cli
class TestEntityOperations:
    """Test entity creation and search."""

    @staticmethod
    def _matches_unique_result(result: dict, unique_id: str) -> bool:
        haystack = " ".join(
            str(result.get(field, "")) for field in ("id", "name", "content", "source")
        )
        return unique_id in haystack

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

        add_result = cli.add(title, content, entity_type="pattern", wait_searchable=True)
        assert add_result.success

        search_result = cli.search(title, limit=10, entity_type="pattern")
        assert search_result.success, f"Search failed: {search_result.stderr}"

        payload = search_result.json()
        results = payload.get("results", []) if isinstance(payload, dict) else []

        assert any(self._matches_unique_result(result, unique_id) for result in results)

    def test_entity_list_multiple_types(self, cli) -> None:
        """List entities of different types."""
        for entity_type in ["pattern", "episode", "task", "project"]:
            result = cli.entity_list(entity_type=entity_type)
            assert result.success, f"Entity list for {entity_type} failed: {result.stderr}"

            data = result.json()
            assert isinstance(data, list)
