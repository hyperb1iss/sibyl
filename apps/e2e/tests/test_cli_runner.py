"""Tests for the E2E CLI runner timeout behavior."""

from unittest.mock import patch

from tests.conftest import WAIT_SEARCHABLE_COMMAND_TIMEOUT, CLIRunner


class TestCLIRunnerTimeouts:
    """Ensure explicit searchability waits have enough subprocess headroom."""

    def test_add_wait_searchable_uses_extended_timeout(self) -> None:
        """Wait-searchable adds should outlive the CLI's internal wait budget."""
        runner = CLIRunner()

        with patch.object(runner, "run") as mock_run:
            runner.add("Title", "Content", wait_searchable=True)

        assert mock_run.call_args.kwargs["timeout"] == WAIT_SEARCHABLE_COMMAND_TIMEOUT

    def test_capture_wait_searchable_uses_extended_timeout(self) -> None:
        """Wait-searchable captures should use the same extended timeout budget."""
        runner = CLIRunner()

        with patch.object(runner, "run") as mock_run:
            runner.capture("Content", wait_searchable=True)

        assert mock_run.call_args.kwargs["timeout"] == WAIT_SEARCHABLE_COMMAND_TIMEOUT
