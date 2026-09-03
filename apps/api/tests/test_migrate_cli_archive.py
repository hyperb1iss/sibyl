"""Archive export serialization and emptiness guard (#459)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from surrealdb import RecordID

from sibyl.cli.migrate import _archive_json_default


def test_archive_default_renders_record_ids_canonically() -> None:
    payload = {"link": RecordID("raw_captures", "abc123")}
    encoded = json.dumps(payload, default=_archive_json_default)
    assert json.loads(encoded) == {"link": "raw_captures:abc123"}


def test_archive_default_renders_datetimes_iso() -> None:
    moment = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
    encoded = json.dumps({"at": moment}, default=_archive_json_default)
    assert json.loads(encoded) == {"at": "2026-09-03T01:02:03+00:00"}


def test_archive_default_falls_back_to_str() -> None:
    class Odd:
        def __str__(self) -> str:
            return "odd-value"

    encoded = json.dumps({"v": Odd()}, default=_archive_json_default)
    assert json.loads(encoded) == {"v": "odd-value"}
