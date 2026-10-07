"""Task list rendering under the explore window contract."""

from unittest.mock import MagicMock, patch

from sibyl_cli.task import _output_tasks_table


@patch("sibyl_cli.task.info")
def test_empty_window_with_more_points_at_the_next_page(mock_info: MagicMock) -> None:
    """A page every row of which was filtered is still a page, not the end."""
    _output_tasks_table([], 50, 50, True, 0)

    mock_info.assert_called_once_with("No tasks on this page (--page 3 for more)")


@patch("sibyl_cli.task.info")
def test_empty_last_page_reports_no_tasks(mock_info: MagicMock) -> None:
    _output_tasks_table([], 0, 50, False, 0)

    mock_info.assert_called_once_with("No tasks found")
