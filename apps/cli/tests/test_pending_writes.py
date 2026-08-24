from __future__ import annotations

import json
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


def test_pending_replay_lock_is_nonblocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)

    with pending_writes.pending_replay_lock() as first:
        with pending_writes.pending_replay_lock() as second:
            assert first is True
            assert second is False


def test_corrupt_pending_write_remains_counted_as_a_structured_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    write_id = "a" * 32
    path = root / f"{write_id}.json"
    path.write_text('{"id": ', encoding="utf-8")

    writes = pending_writes.list_pending_writes()
    status = pending_writes.pending_write_status()

    assert writes == [
        {
            "id": write_id,
            "status": "corrupt",
            "filename": f"{write_id}.json",
            "error": writes[0]["error"],
        }
    ]
    assert "Invalid JSON" in writes[0]["error"]
    assert pending_writes.pending_write_count() == 1
    assert status["count"] == 1
    assert status["failures"] == [
        {"filename": f"{write_id}.json", "error": writes[0]["error"]}
    ]
    assert path.exists()


def test_pending_json_with_an_invalid_shape_is_not_silently_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    write_id = "b" * 32
    (root / f"{write_id}.json").write_text("[]", encoding="utf-8")

    item = pending_writes.read_pending_write(write_id)

    assert item["status"] == "corrupt"
    assert item["filename"] == f"{write_id}.json"
    assert "Expected a JSON object" in item["error"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("json", [], "json"),
        ("params", [], "params"),
        ("params", {"nested": {}}, "params"),
        ("attempts", True, "attempts"),
        ("method", "GET", "method"),
        ("path", "https://evil.example.com/write", "path"),
        ("base_url", "file:///tmp/socket", "base_url"),
        ("idempotency_key", "not-a-uuid", "idempotency_key"),
    ],
)
def test_invalid_pending_request_shapes_never_replay_or_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={"title": "safe"},
        params={"sync": "true"},
    )
    path = pending_writes.resolve_pending_write_path(str(item["id"]))
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored[field] = value
    path.write_text(json.dumps(stored), encoding="utf-8")
    hostile = path.read_bytes()

    loaded = pending_writes.read_pending_write(str(item["id"]))

    assert loaded["status"] == "corrupt"
    assert message in loaded["error"]
    with pytest.raises(ValueError, match="Cannot update corrupt pending write"):
        pending_writes.increment_attempts(str(item["id"]))
    assert path.read_bytes() == hostile


@pytest.mark.parametrize(
    "write_id",
    ["", "../escape", "*", "ABCDEF", "g" * 32, "a" * 33, "a/b", "a-b"],
)
def test_noncanonical_pending_ids_are_rejected_by_every_mutation_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_id: str,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    outside = tmp_path / "escape.json"
    outside.write_text("do not touch", encoding="utf-8")

    for operation in (
        pending_writes.read_pending_write,
        pending_writes.delete_pending_write,
        pending_writes.increment_attempts,
    ):
        with pytest.raises(ValueError, match="Invalid pending write ID"):
            operation(write_id)

    assert outside.read_text(encoding="utf-8") == "do not touch"


def test_canonical_id_never_matches_or_deletes_a_noncanonical_lookalike(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    write_id = "f" * 32
    lookalike = root / f"{write_id}-extra.json"
    lookalike.write_text("do not touch", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        pending_writes.read_pending_write(write_id)
    assert pending_writes.delete_pending_write(write_id) is False
    with pytest.raises(FileNotFoundError):
        pending_writes.increment_attempts(write_id)

    assert lookalike.read_text(encoding="utf-8") == "do not touch"


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
