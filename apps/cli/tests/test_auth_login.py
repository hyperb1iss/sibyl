from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from sibyl_cli import auth
from sibyl_cli.main import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_top_level_auth_aliases_are_registered() -> None:
    runner = CliRunner()

    login = runner.invoke(app, ["login", "--help"])
    logout = runner.invoke(app, ["logout", "--help"])
    whoami = runner.invoke(app, ["whoami", "--help"])

    assert login.exit_code == 0
    assert "--no-browser" in _plain(login.stdout)
    assert logout.exit_code == 0
    assert "--all" in _plain(logout.stdout)
    assert whoami.exit_code == 0
    assert "Show auth status" in _plain(whoami.stdout)


def test_device_no_browser_prints_url_without_polling(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        auth,
        "_start_device_flow",
        lambda **_kwargs: ("device-code", "USER-CODE", "https://verify.test", 5, 600),
    )

    def fail_poll(**_kwargs: object) -> dict:
        raise AssertionError("no-browser must not poll for approval")

    monkeypatch.setattr(auth, "_poll_device_token", fail_poll)

    with pytest.raises(auth._NoBrowserLoginPrinted):
        auth._login_via_device_flow(
            api_url="http://testserver/api",
            no_browser=True,
            timeout_seconds=180,
        )

    output = capsys.readouterr().out
    assert "USER-CODE" in output
    assert "https://verify.test" in output


def test_login_auto_returns_after_no_browser_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def print_only(**_kwargs: object) -> dict:
        raise auth._NoBrowserLoginPrinted(
            "Login URL printed; approval polling skipped for --no-browser."
        )

    monkeypatch.setattr(auth, "_login_via_device_flow", print_only)

    auth._login_auto(
        api_url="http://testserver/api",
        no_browser=True,
        timeout_seconds=180,
        email=None,
        password=None,
    )

    output = capsys.readouterr().out
    assert "approval polling skipped" in output


def test_login_auto_oauth_preserves_access_token_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[dict[str, object]] = []

    def device_unavailable(**_kwargs: object) -> dict:
        raise RuntimeError("device auth unavailable")

    def oauth_login(**_kwargs: object) -> tuple[str, str, str, int]:
        return "access-token", "refresh-token", "http://testserver", 3600

    def persist_tokens(**kwargs: object) -> None:
        writes.append(kwargs)

    monkeypatch.setattr(auth, "_login_via_device_flow", device_unavailable)
    monkeypatch.setattr(auth, "_oauth_pkce_login", oauth_login)
    monkeypatch.setattr(auth, "_persist_tokens", persist_tokens)

    auth._login_auto(
        api_url="http://testserver/api",
        no_browser=False,
        timeout_seconds=180,
        email=None,
        password=None,
    )

    assert writes == [
        {
            "api_url": "http://testserver/api",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "credential_scope_name": None,
        }
    ]
