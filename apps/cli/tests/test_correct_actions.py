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
        "metadata": {"observed_revision": 3},
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


def test_a_change_between_preview_and_apply_is_refused_and_leaves_the_rerun_free(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real client: preview at 3, the memory moves to 4, the apply conflicts.

    The refusal names both revisions, nothing is queued, and a rerun previews
    the current state and applies.
    """
    import json

    import httpx

    import sibyl_cli.client_transport as client_transport_module
    from sibyl_cli import pending_writes
    from sibyl_cli.client import SibylClient

    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    client_transport_module._FAILURE_WINDOWS.clear()
    revision = {"current": 3}
    applied: list[int | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/replay-identity":
            return httpx.Response(404, json={"detail": "Not Found"})
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/corrections/preview"):
            preview = _planned("hide", state="active", flags=["hidden"], reversible=True)
            preview["metadata"] = {"observed_revision": revision["current"]}
            # Someone else corrects the memory right after this preview.
            if revision["current"] == 3:
                revision["current"] = 4
            return httpx.Response(200, json=preview)
        if request.url.path.endswith("/corrections"):
            applied.append(body.get("expected_revision"))
            if body.get("expected_revision") != revision["current"]:
                return httpx.Response(
                    409,
                    json={
                        "error": "revision_conflict",
                        "message": "The memory changed; rerun to see the current state.",
                        "details": {
                            "expected": str(body.get("expected_revision")),
                            "actual": str(revision["current"]),
                        },
                    },
                )
            return httpx.Response(200, json=_applied())
        return httpx.Response(200, json={"matches": [{"id": SOURCE_ID}]})

    def real_client() -> SibylClient:
        client = SibylClient(base_url="http://testserver/api", auth_token="token")
        client._client = httpx.AsyncClient(
            base_url=client.base_url,
            transport=httpx.MockTransport(respond),
            headers=client._default_headers(),
        )
        return client

    args = ["correct", SOURCE_ID, "--action", "hide", "--reason", "Noise"]
    with patch("sibyl_cli.memory_admin.get_client", side_effect=lambda *_a, **_k: real_client()):
        first = CliRunner().invoke(app, args)
        queued_after_first = pending_writes.list_pending_writes()
        second = CliRunner().invoke(app, args)

    assert first.exit_code == 1
    assert (
        "The memory changed since the preview (revision 3 → 4); rerun to preview the current state."
        in _flat(first.stdout)
    )
    assert queued_after_first == []
    assert second.exit_code == 0, second.stdout
    assert "Memory corrected: hide" in _flat(second.stdout)
    assert applied == [3, 4]
    assert pending_writes.list_pending_writes() == []


@patch("sibyl_cli.memory_admin.get_client")
def test_explicit_preview_displays_the_lifecycle_plan(mock_get_client: MagicMock) -> None:
    client = _client(_planned("delete", state="deleted", flags=[], reversible=False))
    mock_get_client.return_value = _FakeClientContext(client)
    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", "delete", "--reason", "Cleanup", "--preview"]
    )
    assert result.exit_code == 0, result.stdout
    assert "delete: state → deleted" in _flat(result.stdout)
    assert "2 derived records affected; irreversible" in _flat(result.stdout)
    assert [call.kwargs["preview"] for call in client.correct_memory.await_args_list] == [True]


@patch("sibyl_cli.memory_admin.get_client")
def test_a_lifecycle_apply_requires_an_observed_revision(mock_get_client: MagicMock) -> None:
    planned = _planned("delete", state="deleted", flags=[], reversible=False)
    planned.pop("metadata")
    client = _client(planned)
    mock_get_client.return_value = _FakeClientContext(client)
    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", "delete", "--reason", "Cleanup", "--yes"]
    )
    assert result.exit_code == 1
    assert "no observed revision" in _flat(result.stdout)
    assert [call.kwargs["preview"] for call in client.correct_memory.await_args_list] == [True]


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("impact", [{"partially_applied": True}, {"propagation_complete": False}])
@patch("sibyl_cli.memory_admin.get_client")
def test_partial_corrections_return_failure_and_preserve_the_receipt(
    mock_get_client: MagicMock, json_output: bool, impact: dict[str, object]
) -> None:
    data = {**_applied(), "recall_impact": impact}
    client = _client(_planned("delete", state="deleted", flags=[], reversible=False))
    client.correct_memory = AsyncMock(
        side_effect=lambda _id, **kw: (
            _planned("delete", state="deleted", flags=[], reversible=False)
            if kw["preview"]
            else data
        )
    )
    mock_get_client.return_value = _FakeClientContext(client)
    args = ["correct", SOURCE_ID, "--action", "delete", "--reason", "Cleanup", "--yes"]
    result = CliRunner().invoke(app, args + (["--json"] if json_output else []))
    assert result.exit_code == 1
    assert "Memory corrected" not in result.stdout
    assert "correct-1" in result.stdout
    if not json_output:
        assert "partially applied" in _flat(result.stdout)


@pytest.mark.parametrize("receipt", [None, {"applied": False}])
@patch("sibyl_cli.memory_admin.get_client")
def test_applied_response_requires_an_applied_receipt(
    mock_get_client: MagicMock, receipt: dict[str, object] | None
) -> None:
    client = _client(_planned("mark_wrong", state="contested", flags=[], reversible=True))
    client.correct_memory = AsyncMock(return_value={**_applied(), "mutation_receipt": receipt})
    mock_get_client.return_value = _FakeClientContext(client)
    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", "wrong", "--reason", "Incorrect"]
    )
    assert result.exit_code == 1
    assert "outcome is unconfirmed" in _flat(result.stdout)


@pytest.mark.parametrize("derived_ids", [[], ["visible-child"]])
@patch("sibyl_cli.memory_admin.get_client")
def test_explicit_preview_reports_incomplete_derived_impact(
    mock_get_client: MagicMock, derived_ids: list[str]
) -> None:
    planned = _planned("delete", state="deleted", flags=[], reversible=False)
    planned["affected_derived_ids"] = derived_ids
    planned["metadata"]["derived_lookup_complete"] = False
    client = _client(planned)
    mock_get_client.return_value = _FakeClientContext(client)
    result = CliRunner().invoke(
        app, ["correct", SOURCE_ID, "--action", "delete", "--preview", "--reason", "Withdraw"]
    )
    assert result.exit_code == 0, result.stdout
    output = _flat(result.stdout)
    assert "incomplete" in output
    assert "0 derived records affected" not in output
    if derived_ids:
        assert "at least 1 derived record found" in output
    else:
        assert "derived impact unknown" in output
    assert len(client.correct_memory.await_args_list) == 1
