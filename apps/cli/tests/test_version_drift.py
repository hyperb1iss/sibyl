"""Drift notices must be quiet, stderr-only, and never block on a bad header."""

from __future__ import annotations

import pytest

from sibyl_cli import version_drift
from sibyl_cli.version_drift import ClientTooOldError, check_response_headers
from sibyl_core.version_contract import (
    CLIENT_VERSION_HEADER,
    MIN_CLIENT_HEADER,
    SERVER_VERSION_HEADER,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path):
    version_drift.reset_process_state()
    saved: dict[str, object] = {}
    monkeypatch.setattr(version_drift.config_store, "get", lambda k, d=None: saved.get(k, d))
    monkeypatch.setattr(
        version_drift.config_store,
        "set_value",
        lambda k, v: saved.__setitem__(k, v),
    )
    return saved


def _headers(**values: str) -> dict[str, str]:
    return values


def test_drift_warns_on_stderr_not_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A notice on stdout would corrupt `--json` output that a script or an
    # agent is parsing.
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")

    check_response_headers(_headers(**{SERVER_VERSION_HEADER: "1.1.5"}), base_url="https://a")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "1.1.5" in captured.err
    assert "sibyl update" in captured.err


def test_only_one_notice_per_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")
    headers = _headers(**{SERVER_VERSION_HEADER: "1.1.5"})

    for _ in range(5):
        check_response_headers(headers, base_url="https://a")

    assert capsys.readouterr().err.count("sibyl update") == 1


def test_repeat_notice_suppressed_within_a_day(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")
    headers = _headers(**{SERVER_VERSION_HEADER: "1.1.5"})

    check_response_headers(headers, base_url="https://a", now=1000.0)
    capsys.readouterr()

    version_drift.reset_process_state()  # simulate a second command
    check_response_headers(headers, base_url="https://a", now=1000.0 + 3600)

    assert capsys.readouterr().err == ""


def test_notice_returns_after_a_day(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")
    headers = _headers(**{SERVER_VERSION_HEADER: "1.1.5"})

    check_response_headers(headers, base_url="https://a", now=1000.0)
    capsys.readouterr()

    version_drift.reset_process_state()
    check_response_headers(headers, base_url="https://a", now=1000.0 + 86_401)

    assert "sibyl update" in capsys.readouterr().err


def test_new_server_version_warns_immediately(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A server upgrade inside the quiet window is news, not noise.
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")

    check_response_headers(
        _headers(**{SERVER_VERSION_HEADER: "1.1.5"}), base_url="https://a", now=1000.0
    )
    capsys.readouterr()

    version_drift.reset_process_state()
    check_response_headers(
        _headers(**{SERVER_VERSION_HEADER: "1.2.0"}), base_url="https://a", now=1000.0 + 60
    )

    assert "1.2.0" in capsys.readouterr().err


def test_separate_servers_are_tracked_separately(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")
    headers = _headers(**{SERVER_VERSION_HEADER: "1.1.5"})

    check_response_headers(headers, base_url="https://a", now=1000.0)
    check_response_headers(headers, base_url="https://b", now=1000.0)

    assert capsys.readouterr().err.count("sibyl update") == 2


def test_no_notice_when_versions_match(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.1.5")

    check_response_headers(_headers(**{SERVER_VERSION_HEADER: "1.1.5"}), base_url="https://a")

    assert capsys.readouterr().err == ""


def test_no_notice_from_a_server_that_sends_no_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Talking to a server older than this feature must stay silent.
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")

    check_response_headers(_headers(), base_url="https://a")

    assert capsys.readouterr().err == ""


def test_floor_raises_for_an_older_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")

    with pytest.raises(ClientTooOldError) as exc_info:
        check_response_headers(
            _headers(**{SERVER_VERSION_HEADER: "1.2.0", MIN_CLIENT_HEADER: "1.1.0"}),
            base_url="https://a",
        )

    assert exc_info.value.minimum == "1.1.0"
    assert exc_info.value.client == "1.0.0"


def test_floor_allows_an_equal_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.1.0")

    check_response_headers(
        _headers(**{SERVER_VERSION_HEADER: "1.2.0", MIN_CLIENT_HEADER: "1.1.0"}),
        base_url="https://a",
    )


def test_unparseable_floor_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Turning an unreadable header into a hard stop would be an outage.
    monkeypatch.setattr(version_drift, "client_version", lambda: "1.0.0")

    check_response_headers(
        _headers(**{SERVER_VERSION_HEADER: "1.2.0", MIN_CLIENT_HEADER: "garbage"}),
        base_url="https://a",
    )


def test_unknown_client_version_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    # pkg_version() returns "unknown" when the CLI runs from a source tree
    # with no installed distribution.
    monkeypatch.setattr(version_drift, "client_version", lambda: "unknown")

    check_response_headers(
        _headers(**{SERVER_VERSION_HEADER: "1.2.0", MIN_CLIENT_HEADER: "1.1.0"}),
        base_url="https://a",
    )


def test_client_identifies_itself_on_every_request() -> None:
    from sibyl_cli.client import SibylClient

    client = SibylClient(base_url="https://example.test/api")
    headers = client._default_headers()

    assert headers[CLIENT_VERSION_HEADER] == version_drift.client_version()
    assert headers["User-Agent"].startswith("sibyl-dev/")
