"""Drift comparison must degrade to silence, never to a crash or a false block."""

from __future__ import annotations

import pytest

from sibyl_core.version_contract import client_is_below_floor, parse_version, server_is_ahead


@pytest.mark.parametrize("value", ["", "   ", None, "unknown", "latest", "v1.2.3-broken-"])
def test_unusable_versions_parse_to_none(value: str | None) -> None:
    assert parse_version(value) is None


def test_prerelease_orders_below_its_release() -> None:
    # The CLI in the field is 1.0.0rc1; a 1.0.0 server is genuinely ahead of it.
    assert server_is_ahead(client="1.0.0rc1", server="1.0.0")
    assert not server_is_ahead(client="1.0.0", server="1.0.0rc1")


def test_equal_versions_are_not_drift() -> None:
    assert not server_is_ahead(client="1.1.5", server="1.1.5")


def test_client_ahead_of_server_is_not_reported_as_drift() -> None:
    # Running a dev build against an older server is normal and not something
    # to nag about on every command.
    assert not server_is_ahead(client="1.2.0", server="1.1.5")


@pytest.mark.parametrize(
    ("client", "server"),
    [("1.1.5", None), (None, "1.1.5"), ("1.1.5", "unknown"), ("0.0.0", "")],
)
def test_unreadable_pairs_report_no_drift(client: str | None, server: str | None) -> None:
    assert not server_is_ahead(client=client, server=server)


def test_floor_blocks_only_a_strictly_older_client() -> None:
    assert client_is_below_floor(client="1.0.0", minimum="1.1.0")
    assert not client_is_below_floor(client="1.1.0", minimum="1.1.0")
    assert not client_is_below_floor(client="1.2.0", minimum="1.1.0")


@pytest.mark.parametrize(
    ("client", "minimum"),
    [("1.0.0", None), ("1.0.0", ""), ("1.0.0", "garbage"), (None, "1.1.0")],
)
def test_unreadable_floor_never_blocks(client: str | None, minimum: str | None) -> None:
    # Refusing to run because a header could not be parsed would turn a
    # diagnostic into an outage.
    assert not client_is_below_floor(client=client, minimum=minimum)
