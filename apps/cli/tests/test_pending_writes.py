from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from sibyl_cli import client_transport, pending_writes


def test_pending_write_store_uses_secure_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)

    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={"title": "Private note", "raw_content": "Sensitive body"},
        params=None,
    )

    path = pending_writes.resolve_pending_write_path(item["id"])
    assert path.exists()
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_pending_write_list_and_prefix_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)

    item = pending_writes.create_pending_write(
        method="PATCH",
        path="/tasks/task_123",
        base_url="http://testserver/api",
        json_payload={"name": "Task update", "entity_type": "task"},
        params={"sync": "true"},
    )

    assert pending_writes.list_pending_writes()[0]["id"] == item["id"]
    assert pending_writes.read_pending_write(item["id"])["path"] == "/tasks/task_123"
    assert pending_writes.pending_write_status()["metrics"]["attempted"] == 1
    assert pending_writes.delete_pending_write(item["id"][:8]) is True
    assert pending_writes.list_pending_writes() == []


def test_corrupt_pending_write_remains_counted_as_a_structured_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    path = root / "broken-write.json"
    path.write_text('{"id": ', encoding="utf-8")

    writes = pending_writes.list_pending_writes()
    status = pending_writes.pending_write_status()

    assert writes == [
        {
            "id": "broken-write",
            "status": "corrupt",
            "filename": "broken-write.json",
            "error": writes[0]["error"],
        }
    ]
    assert "Invalid JSON" in writes[0]["error"]
    assert pending_writes.pending_write_count() == 1
    assert status["count"] == 1
    assert status["failures"] == [
        {"filename": "broken-write.json", "error": writes[0]["error"]}
    ]
    assert path.exists()


def test_pending_json_with_an_invalid_shape_is_not_silently_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    (root / "array.json").write_text("[]", encoding="utf-8")

    item = pending_writes.read_pending_write("array")

    assert item["status"] == "corrupt"
    assert item["filename"] == "array.json"
    assert "Expected a JSON object" in item["error"]


def test_pending_write_label_avoids_raw_content() -> None:
    title, kind = pending_writes.pending_write_label(
        {
            "json": {
                "title": "Public title",
                "raw_content": "Do not show this body",
                "memory_scope": "private",
            }
        }
    )

    assert title == "Public title"
    assert kind == "private"


def test_read_like_posts_are_not_buffered() -> None:
    # Read-like POSTs must never enter the pending-write buffer: a failed read
    # is re-run, not replayed.
    for path in (
        "/search",
        "/search/explore",
        "/search/temporal",
        "/rag/search",
        "/rag/hybrid-search",
        "/rag/code-examples",
        "/context/pack",
        "/memory/raw/recall",
    ):
        assert client_transport._should_buffer_request("POST", path) is False, path


def test_durable_writes_are_still_buffered() -> None:
    # Genuine writes (including persist-capable reflect and raw memory) keep
    # their offline buffer + replay semantics.
    for path in ("/memory/raw", "/context/reflect", "/tasks", "/entities"):
        assert client_transport._should_buffer_request("POST", path) is True, path
    # Reads and auth never buffer.
    assert client_transport._should_buffer_request("GET", "/search") is False
    assert client_transport._should_buffer_request("POST", "/auth/login") is False
