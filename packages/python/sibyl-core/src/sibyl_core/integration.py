"""Connect instructions for a Sibyl server.

Single source of truth for every onboarding surface. The web connect card, the
agent setup document, and the CLI all build their commands here, so the one
line a person copies and the steps an agent follows never drift apart.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal
from urllib.parse import urlsplit

BREW_FORMULA = "hyperb1iss/tap/sibyl"
PYPI_PACKAGE = "sibyl-dev"
# `--upgrade` makes the line idempotent: a plain install leaves an old CLI alone.
UV_INSTALL_COMMAND = f"uv tool install --upgrade {PYPI_PACKAGE}"
BREW_INSTALL_COMMAND = f"brew install {BREW_FORMULA}"
UV_BOOTSTRAP_POSIX = "curl -LsSf https://astral.sh/uv/install.sh | sh"
UV_BOOTSTRAP_WINDOWS = 'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
# Device approval waits this long for the human to finish signing in.
LOGIN_TIMEOUT_MINUTES = 10


AGENT_PROMPT_SNIPPET = """## Sibyl - Your Persistent Memory

Sibyl is your durable memory across sessions. It is a knowledge graph of
decisions, patterns, tasks, and learnings. Reach it through the `sibyl` CLI or
the Sibyl MCP tools, whichever your setup has.

### Session start (MANDATORY)

If your client supports skills, invoke the `sibyl` skill immediately at session
start. The skill points at the version-matched CLI guidance and the current task
queue. Without skill support, run `sibyl context` to confirm the project link
and `sibyl task list --status doing` to see active work before anything else.

### The memory loop: recall, act, remember, reflect

1. **Recall** working context before you act. A past session may have solved this.
   CLI: `sibyl recall "<goal>" --intent build`. MCP: the `search` and `context` tools.
2. **Act** with that context in hand. Use IDs from recall with
   `sibyl show <id>` when a preview is not enough.
3. **Remember** durable knowledge as you learn it: decisions, gotchas, patterns.
   The next session should not have to rediscover it.
   CLI: `sibyl remember "Title" "What matters" --kind decision`. MCP: the `remember` tool.
4. **Reflect** at clean breakpoints to distill session notes into reviewable memory.
   CLI: `sibyl reflect "<notes>" --persist`. MCP: the `reflect` tool.

### Intent -> verb bridges

Recognize these prompt shapes and reach for the verb, not the file system:

- "what am I working on" / "current tasks" -> `sibyl task list --status doing,blocked`
- "where did I leave off" / "pick up from yesterday" -> `sibyl recall "<goal>"`
- "have we hit this before" / "what's our pattern for X" -> `sibyl search "<topic>"`
  first; only `sibyl show <id>` after you have an ID from search or recall
- "remember this" / "write this up" / "save this insight" / "we just learned X" -> `sibyl remember`
- "consolidate this session" / "wrap up" / "save this session for next time" -> `sibyl reflect "<notes>" --persist`
- "show me the full content" -> `sibyl show <id>`
- "what's the deal with X" / "X was mentioned" / "tell me about Y" -> `sibyl search "<X>"` before answering
- "complete this task: <learning>" without an explicit task id -> `sibyl task list -q "<topic>"` once, then `sibyl task complete <id> --learnings "..."`

If the natural-language ask sounds like memory work, it is memory work. Don't
default to `Write` for "write up what we learned"; that's `sibyl remember`. Don't
burn turns hunting for an entity by listing or showing unrelated records; `sibyl
search` and `sibyl recall` are how you discover IDs.

### What to capture

**Always:** non-obvious solutions, gotchas, config quirks, architectural decisions.
**Skip:** trivial facts, throwaway hacks, well-documented basics.

Make each memory findable and reusable later:

- Weak: "Fixed the auth bug."
- Strong: "JWT refresh fails silently when the Redis TTL expires. The token
  service does not handle WRONGTYPE. Fix: regenerate the token on that error."

If your client supports skills (Claude Code, Codex), run `/sibyl` for the full
command reference. Otherwise `sibyl --help` covers it. Run `sibyl doctor` at any
time to verify the recommended agent setup is in place.
"""


Shell = Literal["posix", "powershell"]

# RFC 3986 pieces for an http(s) URL with no userinfo, query or fragment.
_UNRESERVED = r"A-Za-z0-9\-._~"
_SUB_DELIMS = r"!$&'()*+,;="
_PCT_ENCODED = r"%[0-9A-Fa-f]{2}"
_REG_NAME = rf"(?:[{_UNRESERVED}{_SUB_DELIMS}]|{_PCT_ENCODED})+"
_IP_LITERAL = (
    r"\[(?:[0-9A-Fa-f:.]+(?:%25(?:[A-Za-z0-9\-._~]|%[0-9A-Fa-f]{2})+)?"
    r"|[vV][0-9A-Fa-f]+\.[A-Za-z0-9\-._~!$&'()*+,;=:]+)\]"
)
_PCHAR = rf"(?:[{_UNRESERVED}{_SUB_DELIMS}:@]|{_PCT_ENCODED})"
_HTTP_URL = re.compile(
    rf"^[Hh][Tt][Tt][Pp][Ss]?://(?:{_IP_LITERAL}|{_REG_NAME})(?::[0-9]*)?(?:/{_PCHAR}*)*$"
)
# Characters PowerShell reads as plain text inside an unquoted argument.
_POWERSHELL_SAFE = re.compile(r"^[A-Za-z0-9_./:%+=-]+$")


def is_clean_server_url(url: str) -> bool:
    """True for any RFC 3986 http(s) URL without userinfo, query or fragment.

    Every legal path character is accepted (a proxy path such as `/team+blue`
    or `/team:v1` is ordinary). Whitespace, control characters, credentials, a
    query, a fragment, a bad port, and anything that does not parse are
    refused. Commands built from the URL stay safe by quoting, not by
    narrowing what a URL may contain.
    """
    if not _HTTP_URL.match(url):
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises for a port outside 0-65535
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)


def quote_argument(value: str, shell: Shell = "posix") -> str:
    """Quote one argument so the shell passes it through as plain text."""
    if shell == "powershell":
        if _POWERSHELL_SAFE.match(value):
            return value
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)


def setup_command(server_url: str, shell: Shell = "posix") -> str:
    """The command that connects a machine to `server_url`, quoted for `shell`."""
    return f"sibyl setup {quote_argument(server_url.rstrip('/'), shell)}"


def install_commands(server_url: str) -> dict[str, str]:
    """One copyable line per OS: install or upgrade the CLI, then run setup."""
    return {
        "macos": f"{BREW_INSTALL_COMMAND} && {setup_command(server_url)}",
        "linux": f"{UV_INSTALL_COMMAND} && {setup_command(server_url)}",
        # Windows PowerShell 5.1 has no `&&`.
        "windows": f"{UV_INSTALL_COMMAND}; {setup_command(server_url, 'powershell')}",
    }


def agent_setup_markdown(
    server_url: str,
    *,
    minimum_client_version: str | None,
    sso: bool,
) -> str:
    """Instructions an AI coding agent follows to connect this machine.

    Everything here is public: the server URL, the version floor, and whether
    sign-in goes through SSO. The steps themselves live in `sibyl setup`, so
    the agent runs the same implementation a person does.
    """
    base = server_url.rstrip("/")
    setup = setup_command(base)
    floor = (
        f"\n   This server needs version {minimum_client_version} or newer"
        " (check with `sibyl --version`)."
        if minimum_client_version
        else ""
    )
    sign_in = "with your company SSO" if sso else "with your Sibyl email and password"
    return f"""# Set up Sibyl on this machine

Connect this machine to the Sibyl server at {base}
Run each step yourself, and skip any step that is already done.

1. Install or upgrade the CLI.{floor}
   - macOS with Homebrew: `{BREW_INSTALL_COMMAND}`
   - Anywhere else: `{UV_INSTALL_COMMAND}`
   - No uv yet? Install it first: `{UV_BOOTSTRAP_POSIX}`
     (Windows: `{UV_BOOTSTRAP_WINDOWS}`)
   - If `sibyl` is not on PATH afterwards, run `uv tool update-shell` or call
     `~/.local/bin/sibyl` directly.
2. Tell the user: "A browser tab will open. Sign in {sign_in}
   and approve the code shown there." Never ask for their password yourself.
3. Run this, allowing up to {LOGIN_TIMEOUT_MINUTES} minutes while the user signs in:
   `{setup} --yes`
   It connects the CLI to this server, signs in, installs the Sibyl skill for
   your agents, and adds a SessionStart hook when Claude Code is installed, so
   each session starts with Sibyl context. Finished steps are skipped, so
   re-running it is safe. If your shell tool times out sooner, run it in the
   background and relay the sign-in code and URL it prints.
4. Confirm that `sibyl whoami` succeeds and `sibyl doctor` reports no FAIL.
5. Tell the user what you set up, and that a new agent session picks up the
   skill. Suggest a first try: `sibyl context "what's in flight"`
"""
