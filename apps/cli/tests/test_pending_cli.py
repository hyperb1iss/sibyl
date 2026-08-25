from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sibyl_cli import pending, pending_writes
from sibyl_cli.client import SibylClientError

TEST_REPLAY_SCOPE = "token-sha256:test"


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
        replay_scope=TEST_REPLAY_SCOPE,
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
    write_id = "c" * 32
    (root / f"{write_id}.json").write_text("{", encoding="utf-8")

    result = CliRunner().invoke(pending.app, ["list", "--json"])

    assert result.exit_code == 0
    item = json.loads(result.stdout)["pending_writes"][0]
    assert item["id"] == write_id
    assert item["status"] == "corrupt"
    assert item["filename"] == f"{write_id}.json"
    assert "Invalid JSON" in item["error"]


def test_pending_writes_flush_refuses_corrupt_entry_with_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    write_id = "d" * 32
    path = root / f"{write_id}.json"
    path.write_text("{", encoding="utf-8")

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 1
    assert f"Cannot replay {write_id}.json" in result.stdout
    assert "Repair" in result.stdout
    assert f"sibyl pending-writes discard {write_id}" in result.stdout
    assert path.exists()
    assert pending_writes.pending_write_count() == 1


def test_pending_writes_flush_replays_valid_entries_but_keeps_corrupt_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    valid = _create_pending()
    root = pending_writes.pending_writes_dir()
    corrupt_path = root / f"{'e' * 32}.json"
    corrupt_path.write_text("{", encoding="utf-8")
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *, base_url: str, context_name: str | None = None) -> None:
            self.base_url = base_url
            self.context_name = context_name
            self._replay_scope = TEST_REPLAY_SCOPE

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


@pytest.mark.parametrize(("field", "value"), [("json", []), ("params", {"nested": {}})])
def test_pending_writes_flush_never_sends_an_invalid_request_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = _create_pending()
    path = pending_writes.resolve_pending_write_path(str(item["id"]))
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored[field] = value
    path.write_text(json.dumps(stored), encoding="utf-8")
    hostile = path.read_bytes()

    class ForbiddenClient:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("invalid pending body reached the HTTP client")

    monkeypatch.setattr(pending, "SibylClient", ForbiddenClient)

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 1
    assert result.exception is not None
    assert not isinstance(result.exception, AssertionError)
    assert "Cannot replay" in result.stdout
    assert path.read_bytes() == hostile


def test_noncanonical_queue_filename_gets_manual_exact_path_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    root = pending_writes.pending_writes_dir()
    root.mkdir(parents=True)
    path = root / "not-a-queue-id.json"
    path.write_text("{", encoding="utf-8")

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 1
    assert "Inspect and remove this exact file manually" in result.stdout
    assert str(path) in "".join(result.stdout.split())
    assert "pending-writes discard not-a-queue-id" not in result.stdout
    assert path.exists()


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


def test_pending_writes_claim_binds_and_retries_legacy_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={"title": "Legacy", "raw_content": "Sensitive body"},
        params=None,
    )
    pending_writes.increment_attempts(str(item["id"]))
    replayed: list[str] = []
    replay_options: list[bool] = []

    class FakeClient:
        def __init__(self, *, context_name: str | None = None) -> None:
            self.context_name = context_name
            self.base_url = "http://testserver/api"
            self._replay_scope = TEST_REPLAY_SCOPE

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, path: str) -> dict[str, Any]:
            assert path == "/auth/me"
            return {
                "user": {"email": "bliss@example.test"},
                "organization": {"slug": "silkcircuit"},
            }

        async def _maybe_replay_pending_writes(self, *, ignore_backoff: bool = False) -> None:
            replay_options.append(ignore_backoff)
            for queued in pending_writes.list_pending_writes():
                if queued.get("replay_scope") == self._replay_scope:
                    replayed.append(str(queued["id"]))
                    pending_writes.delete_pending_write(str(queued["id"]))

    monkeypatch.setattr(pending, "SibylClient", FakeClient)
    monkeypatch.setattr(
        "sibyl_cli.config_store.resolve_context_name",
        lambda: "local",
    )

    result = CliRunner().invoke(pending.app, ["claim", "--yes"])

    assert result.exit_code == 0
    assert "bliss@example.test" in result.stdout
    assert "silkcircuit" in result.stdout
    assert replayed == [item["id"]]
    assert replay_options == [True]
    assert pending_writes.list_pending_writes() == []


def test_pending_writes_claim_fails_when_replay_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={"title": "Legacy"},
        params=None,
    )

    class FakeClient:
        def __init__(self, *, context_name: str | None = None) -> None:
            self.context_name = context_name
            self.base_url = "http://testserver/api"
            self._replay_scope = TEST_REPLAY_SCOPE

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, path: str) -> dict[str, Any]:
            assert path == "/auth/me"
            return {
                "user": {"email": "bliss@example.test"},
                "organization": {"slug": "silkcircuit"},
            }

        async def _maybe_replay_pending_writes(self, *, ignore_backoff: bool = False) -> None:
            raise AssertionError("claim replay ran without the replay lock")

    monkeypatch.setattr(pending, "SibylClient", FakeClient)
    monkeypatch.setattr(pending, "pending_replay_lock", lambda: nullcontext(False))
    monkeypatch.setattr("sibyl_cli.config_store.resolve_context_name", lambda: "local")

    result = CliRunner().invoke(pending.app, ["claim", "--yes"])

    assert result.exit_code == 1
    assert "claim did not run" in result.stdout
    assert pending_writes.read_pending_write(str(item["id"]))["replay_scope"] is None


@pytest.mark.parametrize(
    "claim_error",
    [
        FileNotFoundError("Pending write disappeared"),
        ValueError("Pending write already belongs to another credential"),
    ],
)
def test_pending_writes_claim_reports_a_concurrent_queue_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claim_error: Exception,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={"title": "Legacy"},
        params=None,
    )

    class FakeClient:
        def __init__(self, *, context_name: str | None = None) -> None:
            self.context_name = context_name
            self.base_url = "http://testserver/api"
            self._replay_scope = TEST_REPLAY_SCOPE

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, path: str) -> dict[str, Any]:
            assert path == "/auth/me"
            return {
                "user": {"email": "bliss@example.test"},
                "organization": {"slug": "silkcircuit"},
            }

        async def _maybe_replay_pending_writes(self, *, ignore_backoff: bool = False) -> None:
            raise AssertionError("an unclaimed write reached replay")

    def fail_claim(_write_id: str, _replay_scope: str) -> dict[str, Any]:
        raise claim_error

    monkeypatch.setattr(pending, "SibylClient", FakeClient)
    monkeypatch.setattr(pending, "claim_pending_write_replay_scope", fail_claim)
    monkeypatch.setattr("sibyl_cli.config_store.resolve_context_name", lambda: "local")

    result = CliRunner().invoke(pending.app, ["claim", "--yes"])

    assert result.exit_code == 1
    assert "Could not claim" in result.stdout
    assert "Traceback" not in result.stdout
    assert pending_writes.resolve_pending_write_path(str(item["id"])).exists()


def test_pending_writes_flush_refuses_an_unclaimed_legacy_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="http://testserver/api",
        json_payload={"title": "Legacy"},
        params=None,
    )

    class FakeClient:
        _replay_scope = TEST_REPLAY_SCOPE

        def __init__(self, *, base_url: str, context_name: str | None = None) -> None:
            self.base_url = base_url
            self.context_name = context_name

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def _request(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            raise AssertionError("unclaimed write reached the server")

    monkeypatch.setattr(pending, "SibylClient", FakeClient)

    result = CliRunner().invoke(pending.app, ["flush"])

    assert result.exit_code == 1
    assert "run sibyl pending-writes claim first" in result.stdout
    assert pending_writes.pending_write_count() == 1


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
            self._replay_scope = TEST_REPLAY_SCOPE

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
            self._replay_scope = TEST_REPLAY_SCOPE

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
            self._replay_scope = TEST_REPLAY_SCOPE
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
            self._replay_scope = TEST_REPLAY_SCOPE

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
            self._replay_scope = TEST_REPLAY_SCOPE

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
