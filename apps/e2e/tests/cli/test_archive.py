"""E2E tests for the capture archive."""

import pytest


@pytest.mark.cli
class TestArchiveWorkflow:
    """Test remembered memories round-trip through the archive."""

    def test_remember_round_trip_through_archive(self, cli, unique_id) -> None:
        """Remember a memory and retrieve its archive entry."""
        title = f"E2E archive smoke {unique_id}"
        content = f"Raw archive content {unique_id}"

        capture_result = cli.remember(title, content, kind="episode")
        assert capture_result.success, f"Remember failed: {capture_result.stderr}"

        captured_entity = capture_result.json()
        assert captured_entity.get("id")
        assert captured_entity.get("name") == title
        assert captured_entity.get("metadata", {}).get("capture_mode") == "remember"
        assert captured_entity.get("metadata", {}).get("capture_surface") == "cli"

        archived_capture = cli.wait_for_archive_capture(title, capture_surface="cli")
        assert archived_capture.get("title") == title
        assert archived_capture.get("capture_surface") == "cli"
        assert archived_capture.get("entity_id") == captured_entity.get("id")

        show_result = cli.archive_show(archived_capture["id"])
        assert show_result.success, f"Archive show failed: {show_result.stderr}"

        archive_detail = show_result.json()
        assert archive_detail.get("id") == archived_capture.get("id")
        assert archive_detail.get("title") == title
        assert archive_detail.get("raw_content") == content
        assert archive_detail.get("metadata", {}).get("capture_mode") == "remember"
        assert archive_detail.get("metadata", {}).get("capture_surface") == "cli"
