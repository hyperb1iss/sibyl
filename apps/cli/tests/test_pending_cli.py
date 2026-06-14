from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sibyl_cli import pending, pending_writes
from sibyl_cli.client import SibylClientError


def _create_pending(path: str = "/memory/raw") -> dict[str, Any]:
    return pending_writes.create_pending_write(
        method="POST",
        path=path,
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


def test_pending_writes_discard_read_like_removes_only_read_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    read_like = _create_pending("/search")
    durable = _create_pending("/memory/raw")

    result = CliRunner().invoke(pending.app, ["discard", "--read-like"])

    assert result.exit_code == 0
    remaining = pending_writes.list_pending_writes()
    assert [item["id"] for item in remaining] == [durable["id"]]
    assert read_like["id"] != durable["id"]
    assert pending_writes.read_pending_metrics()["discarded"] == 1


def test_pending_writes_flush_replays_and_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = _create_pending()
    calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, *, base_url: str, context_name: str | None = None) -> None:
            self.base_url = base_url
            self.context_name = context_name

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
    assert calls[0]["_idempotency_key"] == item["idempotency_key"]
    assert pending_writes.list_pending_writes() == []
    assert pending_writes.read_pending_metrics()["replayed"] == 1


def test_pending_writes_flush_skips_read_like_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    read_like = _create_pending("/search/explore")
    durable = _create_pending("/memory/raw")
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *, base_url: str, context_name: str | None = None) -> None:
            self.base_url = base_url
            self.context_name = context_name

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(path)
            pending_writes.delete_pending_write(str(kwargs["_pending_write_id"]))
            return {"ok": True}

    monkeypatch.setattr(pending, "SibylClient", FakeClient)

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 0
    assert calls == ["/memory/raw"]
    remaining = pending_writes.list_pending_writes()
    assert [item["id"] for item in remaining] == [read_like["id"]]
    assert read_like["id"] != durable["id"]
    assert "Skipped 1 read-like pending request" in result.stdout


def test_pending_writes_flush_reuses_client_per_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    _create_pending("/memory/raw")
    _create_pending("/tasks")
    instances: list[FakeClient] = []
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *, base_url: str, context_name: str | None = None) -> None:
            self.base_url = base_url
            self.context_name = context_name
            instances.append(self)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(path)
            pending_writes.delete_pending_write(str(kwargs["_pending_write_id"]))
            return {"ok": True}

    monkeypatch.setattr(pending, "SibylClient", FakeClient)

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 0
    assert len(instances) == 1
    assert sorted(calls) == ["/memory/raw", "/tasks"]


def test_pending_writes_flush_stops_after_auth_refresh_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    _create_pending("/memory/raw")
    _create_pending("/tasks")
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *, base_url: str, context_name: str | None = None) -> None:
            self.base_url = base_url
            self.context_name = context_name

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(path)
            raise SibylClientError(
                "refresh failed",
                status_code=429,
                detail="rate limited",
                error_code="token_refresh_failed",
            )

    monkeypatch.setattr(pending, "SibylClient", FakeClient)

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 1
    assert len(calls) == 1
    assert calls[0] in {"/memory/raw", "/tasks"}
    assert len(pending_writes.list_pending_writes()) == 2
    assert "Stopping flush" in result.stdout
