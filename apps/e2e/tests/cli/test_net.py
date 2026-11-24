"""`sibyl net` from two real CLI identities against a running Sibyl.

Two people in one organization, each with their own CLI home: the E2E user,
and a teammate invited into the E2E user's organization. Every command runs as
its own process, the way an agent's harness runs it, so a watch that resumes
does so from the cursor the previous process saved, not from memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.api.auth_flow import (
    _bearer_headers,
    _invite,
    _ipv4_loopback_url,
    _replay_client_address,
)
from tests.conftest import API_BASE_URL, CLI_COMMAND, CLIResult, CLIRunner

SETTLE_SECONDS = 2
TEAMMATE_PASSWORD = "sibylnet-teammate-password-123!"
HARNESS_VARIABLES = (
    "SIBYL_AGENT",
    "SIBYL_SESSION",
    "CODEX_THREAD_ID",
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
)


def _cwd_independent(command: tuple[str, ...]) -> tuple[str, ...]:
    """Pin a relative ``--project`` to this process's directory.

    The shared command (``uv run --project ../cli sibyl`` in CI) is written
    relative to ``apps/e2e``, but a HomeCLI runs in its own home directory.
    """
    resolved = list(command)
    for index, argument in enumerate(resolved[:-1]):
        if argument == "--project":
            resolved[index + 1] = str(Path(resolved[index + 1]).resolve())
    return tuple(resolved)


HOME_CLI_COMMAND = _cwd_independent(CLI_COMMAND)


class HomeCLI(CLIRunner):
    """A CLI identity with a home of its own, so its saved watch cursor is its own."""

    def __init__(self, auth_token: str, home: Path, agent: str) -> None:
        super().__init__(auth_token=auth_token)
        self.home = home
        self.agent = agent

    def run(self, *args: str, timeout: float = 60, env_token: bool = True) -> CLIResult:
        """Run with the paired automation variables, or only the server URL.

        ``env_token=False`` leaves SIBYL_AUTH_TOKEN unset, so the command
        uses whatever credentials this home saved.
        """
        env = os.environ.copy()
        for variable in (*HARNESS_VARIABLES, "SIBYL_AUTH_TOKEN"):
            env.pop(variable, None)
        # Keep uv's cache where it was when the command is `uv run`.
        env.setdefault("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv"))
        env["HOME"] = str(self.home)
        env["SIBYL_API_URL"] = env.get("SIBYL_API_URL", "").strip() or API_BASE_URL
        if env_token:
            env["SIBYL_AUTH_TOKEN"] = str(self.auth_token)
        env["SIBYL_AGENT"] = self.agent
        result = subprocess.run(  # noqa: S603
            [*HOME_CLI_COMMAND, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=self.home,
        )
        return CLIResult(result.returncode, result.stdout, result.stderr)


def jsonl(result: CLIResult) -> list[dict]:
    assert result.returncode == 0, result.stdout + result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def ok_json(result: CLIResult) -> dict:
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict), payload
    return payload


def invite_teammate(inviter_token: str) -> tuple[str, str, str]:
    """Sign a new teammate up into the inviter's organization; (token, email, org slug)."""
    email = f"sibylnet-{uuid.uuid4().hex[:12]}@sibyl.dev"

    async def flow() -> tuple[str, str]:
        async with httpx.AsyncClient(
            base_url=_ipv4_loopback_url(API_BASE_URL),
            timeout=30,
            headers={"X-Forwarded-For": _replay_client_address()},
        ) as client:
            me = await client.get("/auth/me", headers=_bearer_headers(inviter_token))
            assert me.status_code == 200, me.text
            slug = me.json()["organization"]["slug"]
            invite_token = await _invite(
                client,
                org_slug=slug,
                inviter_access_token=inviter_token,
                email=email,
                step="invite sibylnet teammate",
            )
            signup = await client.post(
                "/auth/local/signup",
                json={
                    "email": email,
                    "password": TEAMMATE_PASSWORD,
                    "name": "Sibylnet Teammate",
                    "invite_token": invite_token,
                },
            )
            assert signup.status_code == 201, signup.text
            assert signup.json()["organization"]["slug"] == slug
            return signup.json()["access_token"], slug

    token, slug = asyncio.run(flow())
    return token, email, slug


@pytest.fixture(scope="module")
def pair(
    e2e_auth_token: str, tmp_path_factory: pytest.TempPathFactory
) -> tuple[HomeCLI, HomeCLI, str]:
    teammate_token, _, slug = invite_teammate(e2e_auth_token)
    ada = HomeCLI(e2e_auth_token, tmp_path_factory.mktemp("ada-home"), "e2e-ada")
    grace = HomeCLI(teammate_token, tmp_path_factory.mktemp("grace-home"), "e2e-grace")
    return ada, grace, slug


def wait_settled() -> None:
    time.sleep(SETTLE_SECONDS + 0.5)


@pytest.mark.cli
def test_whoami_json_names_both_identities_in_one_organization(
    pair: tuple[HomeCLI, HomeCLI, str],
) -> None:
    ada, grace, slug = pair
    for cli in (ada, grace):
        saved = cli.run("auth", "set-token", str(cli.auth_token), env_token=False)
        assert saved.returncode == 0, saved.stdout + saved.stderr

    mine = ok_json(ada.run("whoami", "--json", env_token=False))
    theirs = ok_json(grace.run("whoami", "--json", env_token=False))

    for me in (mine, theirs):
        assert {"id", "email"} <= set(me["user"]), me
        assert me["organization"]["slug"] == slug, me
    assert mine["user"]["id"] != theirs["user"]["id"]
    assert mine["organization"]["id"] == theirs["organization"]["id"]
    assert theirs["user"]["name"] == "Sibylnet Teammate"
    # `auth status --json` is the same document.
    status = ok_json(grace.run("auth", "status", "--json", env_token=False))
    assert status["user"] == theirs["user"]


@pytest.mark.cli
def test_whoami_json_answers_for_the_automation_token_net_commands_use(
    pair: tuple[HomeCLI, HomeCLI, str], tmp_path: Path
) -> None:
    """A dispatcher that runs on SIBYL_API_URL + SIBYL_AUTH_TOKEN can ask who it is.

    Every `net` command in this file authenticates that way, and the guide
    sends a dispatcher to `whoami --json` to recognize its own person's
    messages, so the same pairing has to answer it too.
    """
    _, grace, slug = pair
    fresh = HomeCLI(str(grace.auth_token), tmp_path, "e2e-dispatcher")

    me = ok_json(fresh.run("whoami", "--json"))

    assert me["organization"]["slug"] == slug
    assert me["user"]["name"] == "Sibylnet Teammate"


@pytest.mark.cli
def test_read_limit_zero_returns_only_the_end_cursor(pair: tuple[HomeCLI, HomeCLI, str]) -> None:
    ada, _, _ = pair
    # A room of its own keeps the run isolated; an @inbox must be a member's handle.
    inbox = f"#inbox-{uuid.uuid4().hex[:8]}"
    ada.run("net", "post", inbox, "history before the cursor")
    wait_settled()

    page = ok_json(ada.run("net", "read", inbox, "--limit", "0", "--json"))

    assert page["messages"] == []
    assert page["more"] is False
    cursor = page["next_since"]
    # Opaque to clients: a URL-safe token, nothing to parse.
    assert isinstance(cursor, str)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", cursor), cursor
    # Reading after it returns nothing older.
    after = ada.run(
        "net", "watch", "-c", inbox, "--since", cursor, "--once", "--json", "--wait", "1"
    )
    assert jsonl(after) == []


@pytest.mark.cli
def test_watch_resumes_across_processes_with_no_replay_and_no_gap(
    pair: tuple[HomeCLI, HomeCLI, str],
) -> None:
    ada, grace, _ = pair
    # A room of its own keeps the run isolated; an @inbox must be a member's handle.
    inbox = f"#grace-{uuid.uuid4().hex[:8]}"
    thread = f"gh:acme/app#{uuid.uuid4().int % 10_000}"
    ada.run("net", "post", inbox, "posted before anyone watched", "-t", thread)
    wait_settled()

    # The first watch starts from now: history is not replayed.
    first = grace.run("net", "watch", "-c", inbox, "--once", "--json", "--wait", "1")
    assert jsonl(first) == []

    sent: list[str] = []
    for body in ("Rebased on main; another look?", "Fixture fix is in.\nSecond line of detail."):
        posted = ok_json(ada.run("net", "post", inbox, body, "-t", thread, "--json"))
        assert posted["message"]["author"]["agent"] == "e2e-ada"
        assert posted["message"]["thread"] == thread
        sent.append(posted["message"]["id"])
    wait_settled()

    received: list[str] = []
    for _ in range(6):
        batch = jsonl(grace.run("net", "watch", "-c", inbox, "--once", "--json", "--wait", "5"))
        received.extend(message["id"] for message in batch)
        if set(sent) <= set(received):
            break
    assert received == sent, (received, sent)

    # A fresh process with nothing new prints nothing: the cursor was saved.
    assert jsonl(grace.run("net", "watch", "-c", inbox, "--once", "--json", "--wait", "1")) == []

    later = ok_json(ada.run("net", "post", inbox, "One more thing", "-t", thread, "--json"))
    wait_settled()
    resumed = jsonl(grace.run("net", "watch", "-c", inbox, "--once", "--json", "--wait", "5"))
    assert [message["id"] for message in resumed] == [later["message"]["id"]]

    # Reading the inbox shows the whole conversation, oldest first.
    page = ok_json(grace.run("net", "read", inbox, "-n", "10", "--json"))
    assert [m["body"] for m in page["messages"]] == [
        "posted before anyone watched",
        "Rebased on main; another look?",
        "Fixture fix is in.\nSecond line of detail.",
        "One more thing",
    ]
    threads = ok_json(grace.run("net", "threads", inbox, "--json"))["threads"]
    assert [(row["thread"], row["message_count"]) for row in threads] == [(thread, 4)]
    channels = ok_json(grace.run("net", "channels", "--json"))["channels"]
    assert any(row["channel"] == inbox and row["message_count"] == 4 for row in channels)


@pytest.mark.cli
def test_a_marked_decision_is_recallable_by_its_author_only(
    pair: tuple[HomeCLI, HomeCLI, str],
) -> None:
    ada, grace, _ = pair
    marker = f"lyrebird{uuid.uuid4().hex[:8]}"
    statement = f"Decided: the {marker} migration ships in two phases."

    posted = ok_json(
        ada.run(
            "net",
            "post",
            "#reviews",
            "--decision",
            "--all-projects",
            "-t",
            "gh:acme/app#42",
            statement,
            "--json",
        )
    )

    memory_id = posted["decision_memory_id"]
    assert memory_id
    assert posted["message"]["decision_memory_id"] == memory_id
    assert posted["message"]["kind"] == "decision"
    items = ada.wait_for_context_items(
        f"{marker} migration phases", match=lambda item: memory_id in str(item.get("id"))
    )
    assert any(marker in json.dumps(item) for item in items)
    # Unscoped, the decision is Ada's private memory: her teammate reads the
    # channel but cannot recall it.
    recalled = grace.context(f"{marker} migration phases", limit=10)
    assert recalled.returncode == 0, recalled.stdout + recalled.stderr
    sections = ok_json(recalled).get("sections") or []
    assert not any(marker in json.dumps(section.get("items")) for section in sections)
    wait_settled()
    page = ok_json(grace.run("net", "read", "#reviews", "-t", "gh:acme/app#42", "--json"))
    assert any(m["id"] == posted["message"]["id"] for m in page["messages"])
