"""Tests for E2E CLI runner behavior."""

import json
import subprocess
from unittest.mock import patch

from tests.conftest import API_BASE_URL, WAIT_SEARCHABLE_COMMAND_TIMEOUT, CLIResult, CLIRunner


class TestCLIRunner:
    """Exercise command construction and subprocess behavior."""

    def test_remember_wait_searchable_uses_extended_timeout(self) -> None:
        """Wait-searchable remembers should outlive the CLI's internal wait budget."""
        runner = CLIRunner()

        with patch.object(runner, "run") as mock_run:
            runner.remember(
                "Title",
                "Content",
                kind="pattern",
                domain="testing",
                tags="python,e2e",
                wait_searchable=True,
            )

        mock_run.assert_called_once_with(
            "remember",
            "Title",
            "Content",
            "--kind",
            "pattern",
            "--json",
            "--domain",
            "testing",
            "--tags",
            "python,e2e",
            "--wait-searchable",
            timeout=WAIT_SEARCHABLE_COMMAND_TIMEOUT,
        )

    def test_capture_wait_searchable_uses_extended_timeout(self) -> None:
        """Wait-searchable captures should use the same extended timeout budget."""
        runner = CLIRunner()

        with patch.object(runner, "run") as mock_run:
            runner.capture("Content", wait_searchable=True)

        assert mock_run.call_args.kwargs["timeout"] == WAIT_SEARCHABLE_COMMAND_TIMEOUT

    def test_context_uses_converged_command(self) -> None:
        runner = CLIRunner()

        with patch.object(runner, "run") as mock_run:
            runner.context("Goal", limit=10, domain="testing")

        mock_run.assert_called_once_with(
            "context",
            "Goal",
            "--limit",
            "10",
            "--json",
            "--domain",
            "testing",
            "--all",
        )

    def test_wait_for_context_items_flattens_sections(self) -> None:
        runner = CLIRunner()
        expected = {"id": "pattern_1", "name": "Matching pattern"}
        result = CLIResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "sections": [
                        {"facet": "patterns", "items": [expected]},
                        {"facet": "decisions", "items": []},
                    ]
                }
            ),
            stderr="",
        )

        with patch.object(runner, "context", return_value=result):
            items = runner.wait_for_context_items(
                "Matching pattern",
                match=lambda item: item["id"] == "pattern_1",
            )

        assert items == [expected]

    def test_run_exports_explicit_api_url(self) -> None:
        """CLI subprocesses should not rely on implicit localhost writes."""
        runner = CLIRunner(auth_token="test-token")
        completed = subprocess.CompletedProcess(
            args=["sibyl", "health"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("tests.conftest.subprocess.run", return_value=completed) as mock_run:
            result = runner.run("health")

        assert result.success
        env = mock_run.call_args.kwargs["env"]
        assert env["SIBYL_API_URL"] == API_BASE_URL
        assert env["SIBYL_AUTH_TOKEN"] == "test-token"
