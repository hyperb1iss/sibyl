"""Tests for the shared connect instructions."""

import shlex
import shutil
import subprocess

import pytest

from sibyl_core.integration import (
    AGENT_PROMPT_SNIPPET,
    agent_setup_markdown,
    install_commands,
    is_clean_server_url,
    setup_command,
)


class TestInstallCommands:
    """The one line each OS copies."""

    def test_every_line_ends_with_setup_for_the_server(self) -> None:
        commands = install_commands("https://sibyl.example.com/")
        assert set(commands) == {"macos", "linux", "windows"}
        for line in commands.values():
            assert line.endswith("sibyl setup https://sibyl.example.com")

    def test_macos_uses_homebrew_and_others_use_an_upgrading_uv_install(self) -> None:
        commands = install_commands("https://sibyl.example.com")
        assert commands["macos"].startswith("brew install hyperb1iss/tap/sibyl && ")
        assert commands["linux"].startswith("uv tool install --upgrade sibyl-dev && ")
        # Windows PowerShell 5.1 has no `&&`.
        assert commands["windows"].startswith("uv tool install --upgrade sibyl-dev; ")

    def test_setup_command_strips_trailing_slash(self) -> None:
        assert setup_command("http://localhost:3334/") == "sibyl setup http://localhost:3334"


class TestAgentSetupMarkdown:
    """The document an agent follows to connect a machine."""

    def test_tailors_url_floor_and_sso_sign_in(self) -> None:
        doc = agent_setup_markdown(
            "https://sibyl.example.com/", minimum_client_version="1.5.0", sso=True
        )
        assert "Sibyl server at https://sibyl.example.com\n" in doc
        assert "`sibyl setup https://sibyl.example.com --yes`" in doc
        assert "version 1.5.0 or newer" in doc
        assert "company SSO" in doc
        assert "`brew install hyperb1iss/tap/sibyl`" in doc
        assert "`uv tool install --upgrade sibyl-dev`" in doc
        assert "sibyl whoami" in doc
        assert "sibyl doctor" in doc

    def test_local_auth_server_signs_in_with_password_and_no_floor(self) -> None:
        doc = agent_setup_markdown("http://localhost:3334", minimum_client_version=None, sso=False)
        assert "email and password" in doc
        assert "SSO" not in doc
        assert "or newer" not in doc

    def test_stays_short(self) -> None:
        doc = agent_setup_markdown(
            "https://sibyl.example.com", minimum_client_version="1.5.0", sso=True
        )
        assert len(doc.splitlines()) <= 40


class TestAgentPromptSnippet:
    """The client-agnostic agent prompt snippet."""

    def test_is_client_agnostic(self) -> None:
        # Mentions the loop and both access paths, not a single client.
        assert "recall" in AGENT_PROMPT_SNIPPET
        assert "CLI" in AGENT_PROMPT_SNIPPET
        assert "MCP" in AGENT_PROMPT_SNIPPET

    def test_does_not_mandate_a_claude_only_skill_invocation(self) -> None:
        # The snippet must not open with a Claude-specific "/sibyl" mandate.
        first_line = AGENT_PROMPT_SNIPPET.strip().splitlines()[0]
        assert "/sibyl" not in first_line

    def test_includes_intent_to_verb_bridges(self) -> None:
        # The eval-proven section that bridges natural-language asks to CLI verbs.
        assert "Intent -> verb bridges" in AGENT_PROMPT_SNIPPET
        assert "sibyl remember" in AGENT_PROMPT_SNIPPET
        assert "sibyl reflect" in AGENT_PROMPT_SNIPPET
        assert "sibyl task list" in AGENT_PROMPT_SNIPPET

    def test_mentions_mandatory_session_start(self) -> None:
        # Section heading naming the MANDATORY framing.
        assert "Session start (MANDATORY)" in AGENT_PROMPT_SNIPPET

    def test_points_at_the_doctor_command(self) -> None:
        # Users should know how to verify their setup.
        assert "sibyl doctor" in AGENT_PROMPT_SNIPPET


HOSTILE_URLS = [
    "https://sibyl.example.com/$(printf QUOTING_PROBE)",
    "https://sibyl.example.com/`touch /tmp/probe`",
    "https://sibyl.example.com/a;rm -rf x",
    "https://sibyl.example.com/it's",
    'https://sibyl.example.com/"x"',
    "https://sibyl.example.com/a&b|c",
]


class TestShellQuoting:
    """Configured URLs reach shells as plain text, never as syntax."""

    @pytest.mark.parametrize("url", HOSTILE_URLS)
    def test_posix_lines_pass_the_url_through_verbatim(self, url: str) -> None:
        for os_name in ("macos", "linux"):
            line = install_commands(url)[os_name]
            argument = line.split(" && sibyl setup ", 1)[1]
            result = subprocess.run(
                ["/bin/sh", "-c", f"printf '%s' {argument}"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout == url

    @pytest.mark.parametrize("url", HOSTILE_URLS)
    def test_powershell_line_single_quotes_and_doubles_quotes(self, url: str) -> None:
        argument = install_commands(url)["windows"].split("; sibyl setup ", 1)[1]
        assert argument.startswith("'") and argument.endswith("'")
        inner = argument[1:-1]
        assert "'" not in inner.replace("''", "")
        assert inner.replace("''", "'") == url

    @pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is not installed")
    @pytest.mark.parametrize("url", HOSTILE_URLS)
    def test_powershell_passes_the_url_through_verbatim(self, url: str) -> None:
        argument = install_commands(url)["windows"].split("; sibyl setup ", 1)[1]
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", f"[Console]::Write({argument})"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == url

    def test_clean_urls_stay_unquoted_everywhere(self) -> None:
        url = "https://sibyl.example.com:8443/team"
        assert all(line.endswith(f"sibyl setup {url}") for line in install_commands(url).values())

    def test_the_agent_document_quotes_the_setup_command(self) -> None:
        url = HOSTILE_URLS[0]
        doc = agent_setup_markdown(url, minimum_client_version=None, sso=True)
        assert f"`sibyl setup {shlex.quote(url)} --yes`" in doc


class TestCleanServerUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://sibyl.example.com",
            "http://localhost:3334",
            "https://sibyl.example.com:8443/team/",
            "http://10.0.0.5:3334",
            "https://[::1]:3334",
        ],
    )
    def test_accepts_plain_http_urls(self, url: str) -> None:
        assert is_clean_server_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            *HOSTILE_URLS,
            "https://user:pass@sibyl.example.com",
            "https://sibyl.example.com?x=1",
            "https://sibyl.example.com#frag",
            "ftp://sibyl.example.com",
            "https://<your-sibyl-host>",
            "sibyl.example.com",
            "",
        ],
    )
    def test_rejects_anything_a_shell_or_browser_could_misread(self, url: str) -> None:
        assert not is_clean_server_url(url)
