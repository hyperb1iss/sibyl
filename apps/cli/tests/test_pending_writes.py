from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
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
    assert status["failures"] == [{"filename": f"{write_id}.json", "error": writes[0]["error"]}]
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


@pytest.mark.parametrize(
    ("category", "status_code", "expected_state"),
    [
        ("authentication", 401, "pending"),
        ("transport", None, "pending"),
        ("server", 503, "pending"),
        ("rejected", 400, "attention"),
        ("rejected", 403, "attention"),
        ("rejected", 404, "attention"),
        ("conflict", 409, "attention"),
        ("rejected", 422, "attention"),
    ],
)
def test_failure_preserves_operation_and_records_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: pending_writes.PendingFailureCategory,
    status_code: int | None,
    expected_state: str,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    original = pending_writes.create_pending_write(
        method="PATCH",
        path="/tasks/123",
        base_url="https://testserver/api",
        json_payload={"name": "Keep this exact payload"},
        params={"sync": True},
        replay_scope="identity:test",
    )
    pending_writes.record_pending_failure(
        original["id"],
        category=category,
        status_code=status_code,
        error_code="Bearer secret-value-can-never-be-persisted",
    )
    updated = pending_writes.read_pending_write(original["id"])
    assert updated["status"] == expected_state
    assert updated["last_failure"]["category"] == category
    assert updated["last_failure"]["status_code"] == status_code
    assert updated["last_failure"]["error_code"] is None
    assert updated["last_failure"]["at"]
    for field in ("id", "idempotency_key", "created_at", "json", "params", "replay_scope"):
        assert updated[field] == original[field]
    assert (
        "secret-value" not in pending_writes.resolve_pending_write_path(original["id"]).read_text()
    )
    assert pending_writes.pending_write_count() == 1
    assert pending_writes.read_pending_metrics()["dropped"] == 0


def test_explicit_retry_keeps_failure_evidence_and_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="https://testserver/api",
        json_payload={"raw_content": "Preserve me"},
        params=None,
    )
    pending_writes.increment_attempts(item["id"])
    failed = pending_writes.record_pending_failure(
        item["id"],
        category="conflict",
        status_code=409,
        error_code="conflict",
    )
    assert pending_writes.pending_write_status()["attention"] == 1
    retried = pending_writes.retry_pending_write(item["id"])
    assert retried["status"] == "pending"
    assert retried["last_failure"] == failed["last_failure"]
    assert retried["last_failure"]["error_code"] == "conflict"
    assert retried["attempts"] == 1
    assert "last_attempt_at" not in retried
    assert retried["idempotency_key"] == item["idempotency_key"]
    assert retried["json"] == item["json"]
    assert pending_writes.pending_write_status()["attention"] == 0
    assert pending_writes.pending_write_status()["pending"] == 1


def test_resource_ordering_spans_task_actions_and_entity_alias() -> None:
    resource = pending_writes.pending_write_resource
    assert (
        resource({"method": "PATCH", "path": "/tasks/123"})
        == resource({"method": "POST", "path": "/tasks/123/complete"})
        == resource({"method": "DELETE", "path": "/entities/123"})
    )
    assert resource({"method": "PATCH", "path": "/tasks/123"}) != resource(
        {"method": "PATCH", "path": "/tasks/456"}
    )
    assert resource({"method": "POST", "path": "/memory/raw", "id": "first"}) != resource(
        {"method": "POST", "path": "/memory/raw", "id": "second"}
    )


def _replay_identity() -> dict:
    return {
        "version": 1,
        "server_instance_id": "11111111-1111-1111-1111-111111111111",
        "user_id": "22222222-2222-2222-2222-222222222222",
        "organization_id": "33333333-3333-3333-3333-333333333333",
        "credential": {
            "kind": "session",
            "api_key_id": None,
            "scopes": [],
            "project_ids": None,
            "memory_space_ids": None,
            "memory_scope_keys": None,
        },
    }


def test_identity_migration_requires_original_credential_and_never_reassigns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url="https://testserver/api",
        json_payload={"raw_content": "Preserve me"},
        params=None,
        replay_scope="credential:original",
    )
    identity = _replay_identity()
    with pytest.raises(ValueError, match="another credential lineage"):
        pending_writes.bind_pending_write_identity(
            item["id"], identity, replay_scope="credential:new"
        )
    assert "replay_identity" not in pending_writes.read_pending_write(item["id"])
    bound = pending_writes.bind_pending_write_identity(
        item["id"],
        identity,
        replay_scope="credential:original",
    )
    assert bound["replay_identity"] == identity
    assert bound["idempotency_key"] == item["idempotency_key"]
    assert (
        pending_writes.bind_pending_write_identity(
            item["id"],
            identity,
            replay_scope="credential:new",
        )
        == bound
    )
    other = {**identity, "user_id": "44444444-4444-4444-4444-444444444444"}
    with pytest.raises(ValueError, match="another identity"):
        pending_writes.bind_pending_write_identity(
            item["id"], other, replay_scope="credential:original"
        )


def test_invalid_identity_is_never_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    with pytest.raises(ValueError, match="Invalid pending write replay identity"):
        pending_writes.create_pending_write(
            method="POST",
            path="/memory/raw",
            base_url="https://testserver/api",
            json_payload={},
            params=None,
            replay_identity={"user_id": "someone"},
        )
    assert pending_writes.list_pending_writes() == []
    assert pending_writes.read_pending_metrics()["attempted"] == 0


def test_bulk_resources_are_ordering_barriers() -> None:
    assert pending_writes.pending_write_resource({"path": "/entities/bulk"}) == "*"
    assert (
        pending_writes.pending_write_resource(
            {"path": "/entities", "method": "POST", "json": {"id": "123"}}
        )
        == "entity:123"
    )


@pytest.mark.parametrize(
    "path",
    ["/context/reflect", "/sources", "/memory/promote", "/teams", "/orgs", "/projects"],
)
def test_ordinary_resource_fallback_does_not_form_a_global_barrier(path: str) -> None:
    resource = pending_writes.pending_write_resource({"method": "POST", "path": path})
    assert resource == f"path:{path.lstrip('/')}"
    assert resource != pending_writes.pending_write_resource(
        {"method": "PATCH", "path": "/tasks/123"}
    )


def test_fallback_groups_actions_on_the_same_source() -> None:
    resource = pending_writes.pending_write_resource
    assert resource({"path": "/sources/123/sync"}) == resource({"path": "/sources/123"})
    assert resource({"path": "/sources/123"}) != resource({"path": "/sources/456"})


CURRENT_BASE_URL = "https://testserver/api"
OTHER_BASE_URL = "https://elsewhere/api"


def test_a_rejection_keeps_the_servers_own_reason_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the message the operator sees a refusal but never what to repair."""
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/entities",
        base_url=CURRENT_BASE_URL,
        json_payload={"name": "x" * 216},
        params=None,
    )

    updated = pending_writes.record_pending_failure(
        item["id"],
        category="rejected",
        status_code=422,
        error_code="validation_error",
        message="name\n  must have at most 200 characters " + "y" * 400,
    )

    message = updated["last_failure"]["message"]
    assert message.startswith("name must have at most 200 characters y")
    assert len(message) == pending_writes.PENDING_FAILURE_MESSAGE_MAX
    assert "\n" not in message


@pytest.mark.parametrize("category", ["authentication", "transport", "server", "dependency"])
def test_a_non_rejection_never_stores_a_response_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: pending_writes.PendingFailureCategory,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=CURRENT_BASE_URL,
        json_payload={"raw_content": "keep"},
        params=None,
    )

    updated = pending_writes.record_pending_failure(
        item["id"],
        category=category,
        status_code=503,
        error_code="service_unavailable",
        message="internal detail nobody should persist",
    )

    assert "message" not in updated["last_failure"]
    assert (
        "internal detail" not in pending_writes.resolve_pending_write_path(item["id"]).read_text()
    )


def test_an_ownerless_write_records_why_nobody_owns_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)

    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=CURRENT_BASE_URL,
        json_payload={"raw_content": "keep"},
        params=None,
        ownership_reason="No credential is signed in for " + "z" * 400,
    )

    stored = pending_writes.read_pending_write(item["id"])
    assert len(stored["ownership_reason"]) == pending_writes.PENDING_OWNERSHIP_REASON_MAX
    assert stored["ownership_reason"].startswith("No credential is signed in for z")


def test_a_recorded_owner_leaves_no_ownership_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)

    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=CURRENT_BASE_URL,
        json_payload={"raw_content": "keep"},
        params=None,
        replay_identity=_replay_identity(),
        ownership_reason="should be ignored",
    )

    assert "ownership_reason" not in pending_writes.read_pending_write(item["id"])


def test_an_empty_ownership_reason_is_a_corrupt_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    item = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=CURRENT_BASE_URL,
        json_payload={"raw_content": "keep"},
        params=None,
    )
    path = pending_writes.resolve_pending_write_path(item["id"])
    path.write_text(json.dumps({**item, "ownership_reason": ""}), encoding="utf-8")

    assert pending_writes.is_corrupt_pending_write(pending_writes.read_pending_write(item["id"]))


def _classified(
    item: dict,
    *,
    identity: dict | None = None,
    replay_scope: str | None = None,
) -> str:
    return pending_writes.classify_pending_write(
        item,
        base_url=CURRENT_BASE_URL,
        replay_scope=replay_scope,
        identity=identity,
    )


def test_classification_separates_the_queues_five_real_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    identity = _replay_identity()
    other_identity = {**identity, "user_id": "44444444-4444-4444-4444-444444444444"}

    def queued(**kwargs: object) -> dict:
        defaults: dict = {
            "method": "POST",
            "path": "/memory/raw",
            "base_url": CURRENT_BASE_URL,
            "json_payload": {"raw_content": "keep"},
            "params": None,
        }
        return pending_writes.create_pending_write(**{**defaults, **kwargs})

    owned = queued(replay_identity=identity)
    attention = pending_writes.record_pending_failure(
        queued(replay_identity=identity)["id"],
        category="rejected",
        status_code=422,
        error_code="validation_error",
    )
    legacy = queued(replay_scope="credential:a-login-that-rotated-away")
    other_owner = queued(replay_identity=other_identity)
    foreign = queued(base_url=OTHER_BASE_URL, replay_identity=identity)
    read_like = queued(path="/search", replay_identity=identity)

    assert _classified(owned, identity=identity) == "retrying"
    assert _classified(attention, identity=identity) == "needs_attention"
    assert _classified(legacy, identity=identity) == "unowned"
    assert _classified(other_owner, identity=identity) == "unowned"
    assert _classified(foreign, identity=identity) == "foreign_server"
    assert _classified(read_like, identity=identity) == "read_like"
    # A legacy write still owned by the live credential lineage keeps retrying.
    assert (
        _classified(legacy, identity=identity, replay_scope="credential:a-login-that-rotated-away")
        == "retrying"
    )


def test_triage_counts_classes_and_ages_only_the_retrying_ones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    identity = _replay_identity()
    young = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=CURRENT_BASE_URL,
        json_payload={"raw_content": "young"},
        params=None,
        replay_identity=identity,
    )
    stale = pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=CURRENT_BASE_URL,
        json_payload={"raw_content": "stale"},
        params=None,
        replay_identity=identity,
    )
    aged = datetime.now(UTC) - timedelta(hours=2)
    pending_writes.resolve_pending_write_path(stale["id"]).write_text(
        json.dumps({**stale, "created_at": aged.isoformat()}), encoding="utf-8"
    )
    pending_writes.create_pending_write(
        method="POST",
        path="/memory/raw",
        base_url=OTHER_BASE_URL,
        json_payload={"raw_content": "foreign"},
        params=None,
        replay_identity=identity,
    )

    triage = pending_writes.pending_write_triage(
        base_url=CURRENT_BASE_URL, replay_scope=None, identity=identity
    )

    assert triage["count"] == 3
    assert triage["counts"]["retrying"] == 2
    assert triage["counts"]["foreign_server"] == 1
    assert triage["foreign_base_urls"] == ["https://elsewhere/api"]
    assert triage["stale_retrying"] == 1
    assert 7000 < triage["oldest_stale_retrying_seconds"] < 7400
    assert young["id"] != stale["id"]
