"""A bot account, end to end through the CLI.

A person creates a bot, grants it contributor on a project, issues it a key,
and the bot remembers under that key. The capture lands in the project's
default scope and is credited to the bot, which the Team page lists as a bot.
"""

from __future__ import annotations

import time

import httpx
import pytest

from tests.conftest import EVENTUAL_TIMEOUT, CLIResult, CLIRunner


def _ok(result: CLIResult, what: str) -> dict:
    assert result.success, f"{what} failed: {result.stdout}\n{result.stderr}"
    payload = result.json()
    assert isinstance(payload, dict), payload
    return payload


def _updates(client: httpx.Client, *, project_id: str, user_id: str) -> int:
    response = client.get("/activity/team", params={"window": "24h", "project_id": project_id})
    assert response.status_code == 200, response.text
    person = next((p for p in response.json()["people"] if p["user_id"] == user_id), None)
    return sum(person["counts"].values()) if person else 0


def _until(read, expected: int) -> int:
    """Poll ``read`` until it reaches ``expected``; return what it last read."""
    deadline = time.monotonic() + EVENTUAL_TIMEOUT
    while (value := read()) < expected and time.monotonic() < deadline:
        time.sleep(0.25)
    return value


@pytest.mark.cli
def test_a_bot_works_under_its_own_identity(
    cli: CLIRunner, sync_auth_client: httpx.Client, unique_id: str
) -> None:
    me = sync_auth_client.get("/auth/me").json()["user"]
    project_id = _ok(cli.project_create(f"Bot Lab {unique_id}"), "project create")["id"]
    _ok(
        cli.run("project", "update", project_id, "--default-scope", "project", "--json"),
        "project default scope",
    )

    bot = _ok(cli.run("bot", "create", f"oncall-{unique_id}", "--json"), "bot create")
    bot_id = bot["id"]
    assert bot["kind"] == "service"
    assert bot["sponsor"]["id"] == me["id"]
    granted = _ok(
        cli.run("bot", "grant", bot_id, project_id, "--role", "contributor", "--json"),
        "bot grant",
    )
    assert granted["role"] == "project_contributor"
    issued = _ok(
        cli.run(
            "bot",
            "key",
            "create",
            bot_id,
            "--name",
            f"e2e-{unique_id}",
            "--scopes",
            "mcp,api:read,api:write",
        ),
        "bot key create",
    )
    raw_key = issued["api_key"]
    assert raw_key.startswith("sk_")

    as_bot = CLIRunner(auth_token=raw_key)
    title = f"Bot capture {unique_id}"
    remembered = _ok(
        as_bot.run(
            "remember",
            title,
            "The pager fired at 03:12 and the bot took it.",
            "--project",
            project_id,
            "--json",
        ),
        "bot remember",
    )
    assert remembered.get("memory_scope") == "project", remembered

    # The write is the bot's, and the Team page says it is a bot's.
    assert _until(lambda: _updates(sync_auth_client, project_id=project_id, user_id=bot_id), 1) >= 1
    team = sync_auth_client.get(
        "/activity/team", params={"window": "24h", "project_id": project_id}
    ).json()
    person = next(p for p in team["people"] if p["user_id"] == bot_id)
    assert person["kind"] == "service"
    assert person["sponsor_user_id"] == me["id"]
    bot_items = [item for item in team["recent"] if item["actor_id"] == bot_id]
    assert bot_items, team["recent"]
    assert {item["actor_kind"] for item in bot_items} == {"service"}
    assert not any(
        item["actor_id"] == me["id"] and item["title"] == title for item in team["recent"]
    )
    updates = sum(person["counts"].values())

    # Private memory stays with its principal, both ways. The bot counts its
    # own private capture and the sponsor never sees it; the sponsor's
    # private note never reaches the bot.
    bot_client = httpx.Client(
        base_url=str(sync_auth_client.base_url),
        headers={"Authorization": f"Bearer {raw_key}"},
        timeout=30.0,
    )
    with bot_client:
        sponsor_view = _updates(bot_client, project_id=project_id, user_id=me["id"])
        _ok(
            as_bot.run(
                "remember",
                f"Bot private {unique_id}",
                "Only the bot reads this.",
                "--project",
                project_id,
                "--scope",
                "private",
                "--json",
            ),
            "bot private remember",
        )
        _ok(
            cli.run(
                "remember",
                f"Sponsor private {unique_id}",
                "Only the sponsor reads this.",
                "--project",
                project_id,
                "--scope",
                "private",
                "--json",
            ),
            "sponsor private remember",
        )
        seen_by_bot = lambda: _updates(bot_client, project_id=project_id, user_id=bot_id)  # noqa: E731
        assert _until(seen_by_bot, updates + 1) == updates + 1
        mine = lambda: _updates(sync_auth_client, project_id=project_id, user_id=me["id"])  # noqa: E731
        assert _until(mine, sponsor_view + 1) == sponsor_view + 1
        assert _updates(sync_auth_client, project_id=project_id, user_id=bot_id) == updates
        assert _updates(bot_client, project_id=project_id, user_id=me["id"]) == sponsor_view

    # A bot mints no keys and no bots.
    assert not as_bot.run("bot", "create", "spawn", "--json").success
    assert not as_bot.run("auth", "api-key", "create", "--name", "self").success

    # Deleting the bot revokes its key at once.
    deleted = _ok(cli.run("bot", "delete", bot_id, "--yes", "--json"), "bot delete")
    assert deleted["api_keys_revoked"] == 1
    refused = as_bot.run("remember", "after delete", "nope", "--project", project_id, "--json")
    assert not refused.success
