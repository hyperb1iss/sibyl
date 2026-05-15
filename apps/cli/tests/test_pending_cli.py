from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sibyl_cli import pending, pending_writes


def _create_pending() -> dict[str, Any]:
    return pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={
            "title": "Visible title",
            "raw_content": "Sensitive body",
            "memory_scope": "private",
        },
        params=None,
    )


def test_pending_writes_list_redacts_payload_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    _create_pending()

    result = CliRunner().invoke(pending.app, ["list"])

    assert result.exit_code == 0
    assert "Visible title" in result.stdout
    assert "Sensitive body" not in result.stdout


def test_pending_writes_discard_removes_by_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = _create_pending()

    result = CliRunner().invoke(pending.app, ["discard", item["id"][:8]])

    assert result.exit_code == 0
    assert pending_writes.list_pending_writes() == []
    assert pending_writes.read_pending_metrics()["discarded"] == 1


def test_pending_writes_flush_replays_and_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = _create_pending()
    calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, *, base_url: str) -> None:
            self.base_url = base_url

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"method": method, "path": path, **kwargs})
            pending_writes.delete_pending_write(str(kwargs["_pending_write_id"]))
            return {"ok": True}

    monkeypatch.setattr(pending, "SibylClient", FakeClient)

    result = CliRunner().invoke(pending.app, ["flush", item["id"][:8]])

    assert result.exit_code == 0
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/memory/raw"
    assert calls[0]["_buffer_pending"] is False
    assert pending_writes.list_pending_writes() == []
    assert pending_writes.read_pending_metrics()["replayed"] == 1
