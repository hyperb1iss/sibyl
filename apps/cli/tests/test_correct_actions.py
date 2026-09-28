"""Tests for the lifecycle actions of `sibyl correct`."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli.main import app
from sibyl_cli.memory_admin import (
    CORRECTION_ACTIONS,
    CORRECTION_ALIASES,
    normalize_correction_action,
)

SOURCE_ID = "3f1c2a9e-0000-4000-8000-000000000000"


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, *_exc: object) -> None:
        return None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _flat(text: str) -> str:
    """Plain text with whitespace collapsed, so colour and wrapping cannot split a match."""
    return " ".join(_ANSI_RE.sub("", text).split())


@pytest.fixture
def interactive_stdin():
    """Let the irreversible-action prompt run, as it would at a terminal."""
    with patch("sibyl_cli.memory_admin.stdin_is_interactive", return_value=True):
        yield


def _planned(action: str, *, state: str, flags: list[str], reversible: bool) -> dict[str, Any]:
    return {
        "allowed": True,
        "applied": False,
        "action": action,
        "reason": "same_scope_write_allowed",
        "target_lifecycle_state": state,
        "target_lifecycle_flags": flags,
        "affected_derived_ids": ["decision-1", "decision-2"],
        "reversible": reversible,
    }


def _applied() -> dict[str, Any]:
    return {
        "allowed": True,
        "applied": True,
        "reason": "same_scope_write_allowed",
        "mutation_receipt": {
            "operation_id": "correct-1",
            "applied": True,
            "revision": 3,
            "affected_records": [f"raw_captures:{SOURCE_ID}"],
            "replayed": False,
        },
    }


def _client(planned: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.resolve_id_prefix = AsyncMock(return_value={"matches": [{"id": SOURCE_ID}]})

    async def correct_memory(_source_id: str, **kwargs: Any) -> dict[str, Any]:
        return planned if kwargs["preview"] else _applied()

    client.correct_memory = AsyncMock(side_effect=correct_memory)
    return client


def test_every_api_correction_action_has_a_cli_name() -> None:
    api_actions = {
        "delete",
        "hide",
        "mark_duplicate",
        "mark_sensitive",
        "mark_stale",
        "mark_wrong",
        "redact",
        "revise",
        "restore",
        "supersede",
    }
    assert set(CORRECTION_ACTIONS.values()) == api_actions


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("restore", "restore"),
        ("active", "restore"),
        ("undo", "restore"),
        ("UNDO", "restore"),
        ("hide", "hide"),
        ("mark_sensitive", "mark_sensitive"),
        ("mark-sensitive", "mark_sensitive"),
        ("sensitive", "mark_sensitive"),
        ("redact", "redact"),
        ("delete", "delete"),
        ("wrong", "wrong"),
        ("nonsense", None),
    ],
)
def test_actions_and_aliases_resolve_to_one_cli_name(typed: str, expected: str | None) -> None:
    assert normalize_correction_action(typed) == expected


@pytest.mark.parametrize(
    ("typed", "api_action", "state", "flags"),
    [
        ("undo", "restore", "active", []),
        ("active", "restore", "active", []),
        ("hide", "hide", "active", ["hidden"]),
        ("sensitive", "mark_sensitive", "active", ["sensitive"]),
    ],
)
@patch("sibyl_cli.memory_admin.get_client")
def test_reversible_lifecycle_actions_preview_then_apply_without_asking(
    mock_get_client: MagicMock,
    typed: str,
    api_action: str,
    state: str,
    flags: list[str],
) -> None:
    client = _client(_planned(api_action, state=state, flags=flags, reversible=True))
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", typed, "--reason", "Marked by mistake"]
    )

    output = _flat(result.stdout)
    assert result.exit_code == 0, result.stdout
    name = normalize_correction_action(typed)
    flag_text = ", ".join(flags) if flags else "none"
    assert (
        f"{name}: state → {state}, flags → {flag_text}; 2 derived records affected; reversible"
        in output
    )
    assert f"Memory corrected: {name}" in output
    assert [call.kwargs["preview"] for call in client.correct_memory.await_args_list] == [
        True,
        False,
    ]
    assert all(
        call.kwargs["action"] == api_action for call in client.correct_memory.await_args_list
    )


@pytest.mark.usefixtures("interactive_stdin")
@pytest.mark.parametrize("action", ["delete", "redact"])
@patch("sibyl_cli.memory_admin.get_client")
def test_irreversible_actions_ask_first_and_a_no_applies_nothing(
    mock_get_client: MagicMock, action: str
) -> None:
    client = _client(_planned(action, state="deleted", flags=[], reversible=False))
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(
        app,
        ["correct", SOURCE_ID, "--action", action, "--reason", "Leaked a secret"],
        input="n\n",
    )

    output = _flat(result.stdout)
    assert result.exit_code == 1
    assert "irreversible" in output
    assert "cannot be undone" in output
    assert "Correction not applied." in output
    assert [call.kwargs["preview"] for call in client.correct_memory.await_args_list] == [True]


@pytest.mark.usefixtures("interactive_stdin")
@pytest.mark.parametrize("action", ["delete", "redact"])
@patch("sibyl_cli.memory_admin.get_client")
def test_irreversible_actions_apply_after_a_yes_or_with_yes_flag(
    mock_get_client: MagicMock, action: str
) -> None:
    for args, stdin in (([], "y\n"), (["--yes"], None), (["-y"], None)):
        client = _client(_planned(action, state="deleted", flags=[], reversible=False))
        mock_get_client.return_value = _FakeClientContext(client)

        result = CliRunner().invoke(
            app,
            ["correct", SOURCE_ID, "--action", action, "--reason", "Leaked a secret", *args],
            input=stdin,
        )

        assert result.exit_code == 0, result.stdout
        assert f"Memory corrected: {action}" in _flat(result.stdout)
        assert [call.kwargs["preview"] for call in client.correct_memory.await_args_list] == [
            True,
            False,
        ]


@patch("sibyl_cli.memory_admin.get_client")
def test_irreversible_json_apply_requires_yes_and_sends_nothing(
    mock_get_client: MagicMock,
) -> None:
    result = CliRunner().invoke(
        app,
        ["correct", SOURCE_ID, "--action", "delete", "--reason", "Leaked", "--json"],
    )

    assert result.exit_code == 1
    assert "pass --yes" in _flat(result.stdout)
    mock_get_client.assert_not_called()


@patch("sibyl_cli.memory_admin.get_client")
def test_a_denied_preview_stops_before_the_apply(mock_get_client: MagicMock) -> None:
    denied = {**_planned("restore", state="active", flags=[], reversible=True)}
    denied.update(allowed=False, reason="not_restorable")
    client = _client(denied)
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", "restore", "--reason", "Undo"]
    )

    assert result.exit_code == 1
    assert "Correction denied: not_restorable" in _flat(result.stdout)
    assert [call.kwargs["preview"] for call in client.correct_memory.await_args_list] == [True]


@patch("sibyl_cli.memory_admin.get_client")
def test_an_unknown_action_names_every_action_and_alias(mock_get_client: MagicMock) -> None:
    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", "obliterate", "--reason", "x"]
    )

    output = _flat(result.stdout)
    assert result.exit_code == 1
    for name in CORRECTION_ACTIONS:
        assert name in output
    for alias in CORRECTION_ALIASES:
        assert alias in output
    mock_get_client.assert_not_called()


def test_correct_help_lists_every_action() -> None:
    # CI forces colour, and Rich splits a flag with escape codes; _flat strips them.
    result = CliRunner().invoke(app, ["correct", "--help"], env={"COLUMNS": "80"})

    output = _flat(result.stdout)
    assert result.exit_code == 0
    for name in CORRECTION_ACTIONS:
        assert name in output
    for alias in CORRECTION_ALIASES:
        assert alias in output
    assert "--yes" in output


@pytest.mark.parametrize("action", ["delete", "redact"])
@patch("sibyl_cli.memory_admin.stdin_is_interactive", return_value=False)
@patch("sibyl_cli.memory_admin.get_client")
def test_irreversible_actions_refuse_up_front_when_nobody_can_confirm(
    mock_get_client: MagicMock, _interactive: MagicMock, action: str
) -> None:
    """A prompt with no terminal behind it aborts unhelpfully or waits on an idle pipe."""
    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", action, "--reason", "Leaked a secret"]
    )

    output = _flat(result.stdout)
    assert result.exit_code == 1
    assert "stdin is not a terminal; pass --yes" in output
    mock_get_client.assert_not_called()


@pytest.mark.parametrize("action", ["delete", "redact"])
@patch("sibyl_cli.memory_admin.stdin_is_interactive", return_value=False)
@patch("sibyl_cli.memory_admin.get_client")
def test_yes_applies_irreversible_actions_without_a_terminal(
    mock_get_client: MagicMock, _interactive: MagicMock, action: str
) -> None:
    client = _client(_planned(action, state="deleted", flags=[], reversible=False))
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", action, "--reason", "Leaked", "--yes"]
    )

    assert result.exit_code == 0, result.stdout
    assert f"Memory corrected: {action}" in _flat(result.stdout)


@patch("sibyl_cli.memory_admin.get_client")
def test_the_apply_is_pinned_to_the_revision_the_preview_showed(
    mock_get_client: MagicMock,
) -> None:
    planned = _planned("hide", state="active", flags=["hidden"], reversible=True)
    planned["metadata"] = {"observed_revision": 7}
    client = _client(planned)
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", "hide", "--reason", "Noise"]
    )

    assert result.exit_code == 0, result.stdout
    preview_call, apply_call = client.correct_memory.await_args_list
    assert preview_call.kwargs["expected_revision"] is None
    assert apply_call.kwargs["expected_revision"] == 7


@patch("sibyl_cli.memory_admin.get_client")
def test_an_explicit_expected_revision_wins_over_the_preview(mock_get_client: MagicMock) -> None:
    planned = _planned("hide", state="active", flags=["hidden"], reversible=True)
    planned["metadata"] = {"observed_revision": 7}
    client = _client(planned)
    mock_get_client.return_value = _FakeClientContext(client)

    result = CliRunner().invoke(
        app,
        ["correct", SOURCE_ID, "--action", "hide", "--reason", "Noise", "--expected-revision", "5"],
    )

    assert result.exit_code == 0, result.stdout
    assert [call.kwargs["expected_revision"] for call in client.correct_memory.await_args_list] == [
        5,
        5,
    ]
