from __future__ import annotations

import json
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


def test_pending_writes_list_keeps_corrupt_entry_visible_in_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    (root / "broken.json").write_text("{", encoding="utf-8")

    result = CliRunner().invoke(pending.app, ["list", "--json"])

    assert result.exit_code == 0
    item = json.loads(result.stdout)["pending_writes"][0]
    assert item["id"] == "broken"
    assert item["status"] == "corrupt"
    assert item["filename"] == "broken.json"
    assert "Invalid JSON" in item["error"]


def test_pending_writes_flush_refuses_corrupt_entry_with_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    path = root / "broken.json"
    path.write_text("{", encoding="utf-8")

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 1
    assert "Cannot replay broken.json" in result.stdout
    assert "Repair" in result.stdout
    assert "sibyl pending-writes discard broken" in result.stdout
    assert path.exists()
    assert pending_writes.pending_write_count() == 1


def test_pending_writes_flush_replays_valid_entries_but_keeps_corrupt_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    valid = _create_pending()
    root = pending_writes.pending_writes_dir()
    corrupt_path = root / "broken.json"
    corrupt_path.write_text("{", encoding="utf-8")
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

    assert result.exit_code == 1
    assert calls == ["/memory/raw"]
    with pytest.raises(FileNotFoundError):
        pending_writes.resolve_pending_write_path(str(valid["id"]))
    remaining = pending_writes.list_pending_writes()
    assert len(remaining) == 1
    assert remaining[0]["status"] == "corrupt"
    assert corrupt_path.exists()
    assert "1 replayed, 1 failed" in result.stdout


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


def test_pending_writes_flush_failure_summary_names_both_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    _create_pending("/memory/raw")
    _create_pending("/tasks")

    class FakeClient:
        def __init__(self, *, base_url: str, context_name: str | None = None) -> None:
            self.base_url = base_url
            self.context_name = context_name

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            if path == "/tasks":
                raise SibylClientError(
                    "conflict",
                    status_code=409,
                    detail="An identical idempotent request is still in progress.",
                )
            pending_writes.delete_pending_write(str(kwargs["_pending_write_id"]))
            return {"ok": True}

    monkeypatch.setattr(pending, "SibylClient", FakeClient)

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 1
    remaining = pending_writes.list_pending_writes()
    assert len(remaining) == 1
    assert remaining[0]["path"] == "/tasks"
    assert "1 replayed, 1 failed" in result.stdout
    assert "safe to flush again" in result.stdout
    assert "flush again shortly" in result.stdout


def test_pending_writes_discard_exits_nonzero_when_an_id_matches_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A named id that matched nothing is a refusal, and the write is still queued."""
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    _create_pending()

    result = CliRunner().invoke(pending.app, ["discard", "deadbeef"])

    assert result.exit_code == 1
    assert len(pending_writes.list_pending_writes()) == 1


def test_pending_writes_discard_exits_zero_when_it_removes_what_was_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = _create_pending()

    result = CliRunner().invoke(pending.app, ["discard", str(item["id"])])

    assert result.exit_code == 0
    assert pending_writes.list_pending_writes() == []


def test_pending_writes_discard_read_like_exits_zero_when_nothing_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No read-like leftovers is an empty result, not a refusal."""
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    _create_pending()

    result = CliRunner().invoke(pending.app, ["discard", "--read-like"])

    assert result.exit_code == 0


def test_pending_writes_discard_counts_a_repeated_id_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same id twice discards once; the second pass is not a miss."""
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = _create_pending()
    write_id = str(item["id"])

    result = CliRunner().invoke(pending.app, ["discard", write_id, write_id])

    assert result.exit_code == 0
    assert pending_writes.list_pending_writes() == []


def test_pending_writes_discard_reports_what_it_removed_on_a_partial_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = _create_pending()

    result = CliRunner().invoke(pending.app, ["discard", str(item["id"]), "deadbeef"])

    assert result.exit_code == 1
    assert "Discarded 1 pending write" in result.stdout
    assert "No pending write matched 1" in result.stdout
