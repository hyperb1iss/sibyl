from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from sibyl_cli import pending_writes


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
