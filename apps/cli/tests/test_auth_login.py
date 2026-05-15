from __future__ import annotations

import pytest

from sibyl_cli import auth


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
