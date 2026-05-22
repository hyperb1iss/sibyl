"""Tests for onboarding copy."""

from unittest.mock import MagicMock, patch

from sibyl_cli.onboarding import show_first_run_message


@patch("sibyl_cli.onboarding.console.print")
def test_first_run_message_points_to_up_without_setup(mock_print: MagicMock) -> None:
    show_first_run_message()

    rendered = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)

    assert "sibyl up" in rendered
    assert "sibyl local start" not in rendered
    assert "sibyl local setup" not in rendered
    assert "sibyl setup" not in rendered
