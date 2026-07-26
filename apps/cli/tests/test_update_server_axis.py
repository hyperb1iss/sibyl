"""`sibyl update` must report the server it talks to, not just PyPI."""

from __future__ import annotations

import pytest

from sibyl_cli import config_store, update


def test_server_version_read_from_health(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {"version": "1.2.0"}

    monkeypatch.setattr(config_store, "get_server_url", lambda: "https://sibyl.test/api")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Response())

    assert update.get_server_version() == "1.2.0"


@pytest.mark.parametrize("status", [404, 500, 503])
def test_non_200_reports_no_version(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    class _Response:
        status_code = status

        @staticmethod
        def json() -> dict[str, str]:
            return {"version": "1.2.0"}

    monkeypatch.setattr(config_store, "get_server_url", lambda: "https://sibyl.test/api")
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Response())

    assert update.get_server_version() is None


def test_unreachable_server_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # `sibyl update` must still report the CLI/PyPI axis when the server is
    # down; an offline server is not a reason to fail the command.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(config_store, "get_server_url", lambda: "https://sibyl.test/api")
    monkeypatch.setattr("httpx.get", _boom)

    assert update.get_server_version() is None


def test_no_configured_server_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_store, "get_server_url", lambda: "")

    assert update.get_server_version() is None
