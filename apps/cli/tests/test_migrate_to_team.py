"""`sibyl migrate to-team` must name its target org and never trust a ledger across orgs.

A real migration replayed a project into the caller's personal org because
the team context's login acted there, and the ledger, keyed only by context
and project, would then have skipped those memories when replaying into the
right org.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from sibyl_cli import migrate
from sibyl_cli.client import SibylClientError
from sibyl_cli.main import app

SOURCE_ORG = "5f0e0b8a-1c2d-4e3f-9a8b-7c6d5e4f3a2b"
PROJECT = "project_v2"
PERSONAL = {"id": "org-personal", "slug": "u-alice", "name": "Alice", "is_personal": True}
TEAM = {"id": "org-team", "slug": "acme", "name": "Acme", "is_personal": False}
ROWS = [
    {
        "uuid": f"src-{n}",
        "title": f"Memory {n}",
        "raw_content": f"content {n}",
        "memory_scope": "project",
        "scope_key": PROJECT,
        "tags": [],
        "metadata": {},
        "provenance": {},
        "source_id": None,
        "capture_surface": "cli",
        "created_at": "2026-09-01T00:00:00Z",
    }
    for n in range(3)
]


@pytest.fixture
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "migrations"
    monkeypatch.setattr(migrate, "_LEDGER_DIR", directory)

    def _source_sql(*, statement: str, **_kwargs: Any) -> list[Any]:
        return [ROWS if "START 0" in statement else []]

    monkeypatch.setattr(migrate, "_source_sql", _source_sql)
    # The graph pass has its own tests; these cover the raw replay and the
    # org and ledger guards around it.
    monkeypatch.setattr(migrate, "_read_source_graph", lambda **_kwargs: ([], []))
    return directory


def _target(monkeypatch: pytest.MonkeyPatch, current: dict[str, Any]) -> MagicMock:
    client = MagicMock()

    async def _get(path: str) -> dict[str, Any]:
        if path == "/auth/replay-identity":
            return {
                "capabilities": ["migration_replay_policy_v1", "migration_graph_writes_v1"],
                "server_instance_id": "server-a",
                "user_id": "alice",
                "organization_id": current["id"],
            }
        return {"organization": {k: current[k] for k in ("id", "slug", "name")}}

    client.get = AsyncMock(side_effect=_get)
    client.list_orgs = AsyncMock(return_value={"orgs": [PERSONAL, TEAM]})
    client.get_entity = AsyncMock(return_value={"id": PROJECT, "name": "V2"})
    client.remember_raw_memory = AsyncMock(
        side_effect=[{"id": f"target-{n}"} for n in range(len(ROWS))]
    )
    client.memory_blame = AsyncMock(
        return_value={
            "source": {
                "id": "found",
                "principal_id": "alice",
                "organization_id": current["id"],
                "scope_key": PROJECT,
            }
        }
    )
    monkeypatch.setattr(migrate, "get_client", lambda *_a, **_k: client)
    return client


def _run(*extra: str) -> Any:
    return CliRunner().invoke(
        app,
        [
            "migrate",
            "to-team",
            "--target-context",
            "team",
            "--project",
            PROJECT,
            "--source-org",
            SOURCE_ORG,
            *extra,
        ],
    )


def _legacy_ledger(directory: Path, receipts: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    route = {"source_org": SOURCE_ORG, "target_context": "team", "target_project_id": PROJECT}
    path = directory / f"{SOURCE_ORG}--team--{PROJECT}.json"
    path.write_text(json.dumps({"route": route, "receipts": receipts}), encoding="utf-8")
    return path


def test_a_personal_target_org_is_refused(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _target(monkeypatch, PERSONAL)

    result = _run()

    assert result.exit_code == 1
    assert "signed in to your personal org" in result.stdout
    assert "org switch <team-slug>" in result.stdout
    client.remember_raw_memory.assert_not_awaited()


def test_a_personal_target_is_refused_even_for_a_dry_run(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _target(monkeypatch, PERSONAL)

    result = _run("--dry-run")

    assert result.exit_code == 1
    assert "signed in to your personal org" in result.stdout


def test_allow_personal_org_is_an_explicit_override(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _target(monkeypatch, PERSONAL)

    result = _run("--allow-personal-org")

    assert result.exit_code == 0, result.stdout
    assert client.remember_raw_memory.await_count == len(ROWS)


def test_the_ledger_is_keyed_by_the_target_org(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _target(monkeypatch, TEAM)

    result = _run()

    assert result.exit_code == 0, result.stdout
    assert "Target org: Acme (acme)" in result.stdout
    ledger = next(path for path in ledger_dir.glob("*.json") if len(path.stem) == 64)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["route"]["target_org_id"] == TEAM["id"]
    assert sorted(data["receipts"]) == [row["uuid"] for row in ROWS]


def test_receipts_for_another_org_do_not_suppress_this_org(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_dir.mkdir(parents=True)
    other = {
        "source_org": SOURCE_ORG,
        "target_context": "team",
        "target_org_id": PERSONAL["id"],
        "target_project_id": PROJECT,
    }
    (ledger_dir / f"{SOURCE_ORG}--team--{PERSONAL['id']}--{PROJECT}.json").write_text(
        json.dumps({"route": other, "receipts": {row["uuid"]: "x" for row in ROWS}}),
        encoding="utf-8",
    )
    client = _target(monkeypatch, TEAM)

    result = _run()

    assert result.exit_code == 0, result.stdout
    assert client.remember_raw_memory.await_count == len(ROWS)


def test_a_legacy_ledger_is_adopted_when_its_receipts_are_in_this_org(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _legacy_ledger(ledger_dir, {row["uuid"]: f"t-{row['uuid']}" for row in ROWS[:2]})
    client = _target(monkeypatch, TEAM)

    result = _run()

    assert result.exit_code == 0, result.stdout
    assert "Adopted 2 of 2 receipts" in result.stdout
    assert "Migrated 1 raw memories (2 already in ledger)" in result.stdout
    assert client.remember_raw_memory.await_count == 1
    # Kept for a later run into whichever org its other receipts belong to.
    assert legacy.exists()


def test_every_legacy_receipt_is_checked_and_only_confirmed_ones_adopted(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A context that switched orgs between runs leaves one ledger holding both
    # orgs' receipts; a sample of the first few would vouch for all of them.
    receipts = {
        "src-0": "in-this-org-0",
        "src-1": "in-this-org-1",
        "src-extra": "in-this-org-2",
        "src-2": "in-another-org",
    }
    _legacy_ledger(ledger_dir, receipts)
    client = _target(monkeypatch, TEAM)

    async def _blame(target_id: str) -> dict[str, Any]:
        if target_id == "in-another-org":
            raise SibylClientError("API error: not found", status_code=404)
        return {
            "source": {
                "id": target_id,
                "principal_id": "alice",
                "organization_id": TEAM["id"],
                "scope_key": PROJECT,
            }
        }

    client.memory_blame = AsyncMock(side_effect=_blame)

    result = _run()

    assert result.exit_code == 0, result.stdout
    assert client.memory_blame.await_count == len(receipts)
    assert "Adopted 3 of 4 receipts" in result.stdout
    assert "1 are not in this org and will be replayed" in result.stdout
    assert client.remember_raw_memory.await_count == 1
    ledger = next(path for path in ledger_dir.glob("*.json") if len(path.stem) == 64)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["receipts"]["src-2"] == "target-0"
    assert "in-another-org" not in data["receipts"].values()


def test_receipts_from_another_org_are_replayed_not_adopted(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _legacy_ledger(ledger_dir, {row["uuid"]: f"t-{row['uuid']}" for row in ROWS})
    client = _target(monkeypatch, TEAM)
    client.memory_blame = AsyncMock(
        side_effect=SibylClientError("API error: not found", status_code=404)
    )

    result = _run()

    assert result.exit_code == 0, result.stdout
    assert "Adopted 0 of 3 receipts" in result.stdout
    assert client.remember_raw_memory.await_count == len(ROWS)
    assert legacy.exists()


def test_an_unverifiable_receipt_stops_the_run(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_ledger(ledger_dir, {ROWS[0]["uuid"]: "t-0"})
    client = _target(monkeypatch, TEAM)
    client.memory_blame = AsyncMock(
        side_effect=SibylClientError("API error: forbidden", status_code=403)
    )

    result = _run()

    assert result.exit_code == 1
    assert "could not check receipt t-0" in result.stdout
    client.remember_raw_memory.assert_not_awaited()


def test_a_dry_run_reports_adoption_without_touching_the_ledgers(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _legacy_ledger(ledger_dir, {ROWS[0]["uuid"]: "t-0"})
    before = sorted(p.name for p in ledger_dir.iterdir())
    _target(monkeypatch, TEAM)

    result = _run("--dry-run")

    assert result.exit_code == 0, result.stdout
    assert "Would adopt 1 of 1 receipts" in result.stdout
    assert "Would migrate 2 raw memories (1 already in ledger)" in result.stdout
    assert sorted(p.name for p in ledger_dir.iterdir()) == before
    assert legacy.exists()


async def test_bound_routes_separate_actor_server_and_source() -> None:
    client = MagicMock()
    identity = {
        "capabilities": ["migration_replay_policy_v1", "migration_graph_writes_v1"],
        "server_instance_id": "server-a",
        "user_id": "alice",
        "organization_id": "team",
    }
    client.get = AsyncMock(side_effect=lambda _: dict(identity))
    base = {
        "source_org": "source",
        "target_context": "team",
        "target_org_id": "team",
        "target_project_id": "project_target",
    }
    original = await migrate._bind_route(
        client, base, source_url="ws://localhost:8000/rpc", source_project="project_a"
    )
    routes = [original]
    for key, value in (("server_instance_id", "server-b"), ("user_id", "bob")):
        previous = identity[key]
        identity[key] = value
        routes.append(
            await migrate._bind_route(
                client, base, source_url="ws://localhost:8000/rpc", source_project="project_a"
            )
        )
        identity[key] = previous
    routes.append(
        await migrate._bind_route(
            client, base, source_url="ws://localhost:8001/rpc", source_project="project_a"
        )
    )
    routes.append(
        await migrate._bind_route(
            client, base, source_url="ws://localhost:8000/rpc", source_project="project_b"
        )
    )
    assert len({migrate._ledger_path(route) for route in routes}) == 5
    identity["organization_id"] = "other"
    with pytest.raises(RuntimeError, match="write identity"):
        await migrate._bind_route(
            client, base, source_url="ws://localhost:8000/rpc", source_project="project_a"
        )


def test_legacy_receipts_owned_by_another_member_do_not_suppress_writes(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_ledger(ledger_dir, {ROWS[0]["uuid"]: "foreign"})
    client = _target(monkeypatch, TEAM)
    client.memory_blame = AsyncMock(
        return_value={
            "source": {
                "principal_id": "bob",
                "organization_id": TEAM["id"],
                "scope_key": PROJECT,
            }
        }
    )
    result = _run()
    assert result.exit_code == 0, result.stdout
    assert client.remember_raw_memory.await_count == len(ROWS)


def test_raw_retry_reuses_operation_key_after_lost_acknowledgment(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _target(monkeypatch, TEAM)
    client.remember_raw_memory.side_effect = [
        SibylClientError("response lost", status_code=503),
        {"id": "target-1"},
        {"id": "target-2"},
        {"id": "target-0"},
    ]
    assert _run().exit_code == 1
    assert _run().exit_code == 0
    calls = client.remember_raw_memory.await_args_list
    assert calls[0].kwargs["_idempotency_key"] == calls[3].kwargs["_idempotency_key"]
    assert len({call.kwargs["_idempotency_key"] for call in calls}) == 3


async def test_raw_client_forwards_migration_key_without_replacing_it() -> None:
    from sibyl_cli.client_memory import ClientMemoryMixin

    class Client(ClientMemoryMixin):
        _request = AsyncMock(return_value={"id": "raw"})

    client = Client()
    await client.remember_raw_memory(title="title", raw_content="body", _idempotency_key="stable")
    assert client._request.await_args.kwargs["_idempotency_key"] == "stable"
    assert client._request.await_args.kwargs["_buffer_pending"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"review_state": "redacted"},
        {"deleted_at": "2026-01-01T00:00:00Z"},
        {"metadata": {"lifecycle_state": "archived"}},
        {"metadata": {"lifecycle_flags": ["hidden"]}},
        {"capture_surface": "reflection_candidate", "review_state": "pending"},
    ],
)
def test_raw_source_exclusions_are_not_reactivated(change: dict[str, Any]) -> None:
    assert migrate._raw_migratable(ROWS[0])
    assert not migrate._raw_migratable({**ROWS[0], **change})


def test_raw_dry_run_validates_content_before_claiming_it_can_migrate(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _target(monkeypatch, TEAM)
    monkeypatch.setattr(
        migrate,
        "_fetch_source_page",
        lambda **_: [{**ROWS[0], "raw_content": ""}] if _["start"] == 0 else [],
    )
    result = _run("--dry-run")
    assert result.exit_code == 1
    assert "empty content" in result.stdout
    client.remember_raw_memory.assert_not_awaited()


def test_retry_ledger_payloads_are_private_and_leave_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "migrations" / "ledger.json"
    migrate._save_migration_ledger(path, {"private": "body"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {"private": "body"}
    assert list(path.parent.iterdir()) == [path]


def test_old_server_is_refused_before_migration_writes(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _target(monkeypatch, TEAM)
    client.get = AsyncMock(
        return_value={
            "organization": TEAM,
            "organization_id": TEAM["id"],
            "server_instance_id": "old",
            "user_id": "alice",
        }
    )
    result = _run()
    assert result.exit_code == 1
    assert "upgrade the target server" in result.stdout
    client.remember_raw_memory.assert_not_awaited()
    assert not ledger_dir.exists()


@pytest.mark.parametrize("graph", [False, True])
def test_replay_only_server_allows_raw_but_refuses_graph_before_writes(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch, graph: bool
) -> None:
    client = _target(monkeypatch, TEAM)
    original_get = client.get.side_effect

    async def replay_only_identity(path: str) -> dict[str, Any]:
        result = await original_get(path)
        if path == "/auth/replay-identity":
            result["capabilities"] = ["migration_replay_policy_v1"]
        return result

    client.get.side_effect = replay_only_identity
    result = _run("--graph" if graph else "--no-graph")
    if graph:
        assert result.exit_code == 1
        assert "upgrade the target server before migrating graph data" in result.stdout
        client.remember_raw_memory.assert_not_awaited()
        client.post.assert_not_called()
        assert not ledger_dir.exists()
    else:
        assert result.exit_code == 0, result.stdout
        assert client.remember_raw_memory.await_count == len(ROWS)


class _Captures:
    """Raw captures on a team server; corrections address them by bare id and revision."""

    def __init__(self, revisions: dict[str, int]) -> None:
        self.revisions = dict(revisions)
        self.sent: list[tuple[str, bool]] = []

    @property
    def ids(self) -> set[str]:
        return set(self.revisions)

    async def correct_memory(
        self,
        source_id: str,
        *,
        action: str,
        reason: str,
        expected_revision: int | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        assert action == "delete"
        self.sent.append((source_id, preview))
        if source_id not in self.revisions:
            raise SibylClientError("API error: not_found: memory_source_not_found", status_code=404)
        if expected_revision != self.revisions[source_id]:
            raise SibylClientError("API error: revision_conflict", status_code=409)
        if not preview:
            del self.revisions[source_id]
        return {"allowed": True, "applied": not preview}


async def _raw_undo(
    target: _Captures,
    ledger: dict[str, str],
    revisions: dict[str, int],
    path: Path,
    *,
    dry_run: bool = False,
) -> list[str]:
    return await migrate._undo_raw(
        target,
        ledger_file=path,
        route={"r": "1"},
        ledger=ledger,
        revisions=revisions,
        dry_run=dry_run,
    )


async def test_raw_undo_deletes_each_replayed_capture_by_its_bare_id(tmp_path: Path) -> None:
    target = _Captures({"cap-1": 1, "cap-2": 1})
    ledger = {"src-1": "cap-1", "src-2": "cap-2"}
    path = tmp_path / "raw.json"

    failures = await _raw_undo(target, ledger, {"src-1": 1, "src-2": 1}, path)

    assert failures == [] and target.ids == set() and ledger == {}
    assert {source_id for source_id, _ in target.sent} == {"cap-1", "cap-2"}
    assert json.loads(path.read_text())["receipts"] == {}


async def test_raw_undo_dry_run_previews_without_deleting(tmp_path: Path) -> None:
    target = _Captures({"cap-1": 1, "cap-2": 1})
    ledger = {"src-1": "cap-1", "src-2": "cap-2"}
    path = tmp_path / "raw.json"

    failures = await _raw_undo(target, ledger, {"src-1": 1, "src-2": 1}, path, dry_run=True)

    assert failures == [] and target.ids == {"cap-1", "cap-2"}
    assert ledger == {"src-1": "cap-1", "src-2": "cap-2"}
    assert all(preview for _, preview in target.sent) and not path.exists()


async def test_raw_undo_reports_captures_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(migrate, "warn", warnings.append)
    target = _Captures({"cap-1": 1})
    ledger = {"src-1": "cap-1", "src-2": "cap-2"}

    failures = await _raw_undo(target, ledger, {"src-1": 1, "src-2": 1}, tmp_path / "raw.json")

    assert failures == [] and target.ids == set() and ledger == {}
    assert warnings == ["1 raw memories in the ledger were already gone from the team server"]


async def test_raw_undo_keeps_captures_it_did_not_create_or_that_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(migrate, "warn", warnings.append)
    # cap-edited was corrected after the migration; cap-adopted landed on an
    # existing capture; cap-unrecorded came from a run that kept no revisions.
    target = _Captures({"cap-edited": 2, "cap-adopted": 3, "cap-unrecorded": 1, "cap-fresh": 1})
    ledger = {
        "src-edited": "cap-edited",
        "src-adopted": "cap-adopted",
        "src-unrecorded": "cap-unrecorded",
        "src-fresh": "cap-fresh",
    }
    revisions = {"src-edited": 1, "src-adopted": 3, "src-fresh": 1}

    failures = await _raw_undo(target, ledger, revisions, tmp_path / "raw.json")

    assert failures == []
    assert target.ids == {"cap-edited", "cap-adopted", "cap-unrecorded"}
    assert set(ledger) == {"src-edited", "src-adopted", "src-unrecorded"}
    assert warnings[0] == "Kept 3 raw memories:"


async def test_undo_refuses_a_server_without_guarded_deletes() -> None:
    client = MagicMock()
    identity = {
        "capabilities": ["migration_replay_policy_v1", "migration_graph_writes_v1"],
        "server_instance_id": "server-a",
        "user_id": "alice",
        "organization_id": "team",
    }
    client.get = AsyncMock(side_effect=lambda _: dict(identity))
    base = {
        "source_org": "source",
        "target_context": "team",
        "target_org_id": "team",
        "target_project_id": "project_target",
    }
    bind = {"source_url": "ws://localhost:8000/rpc", "source_project": "project_a"}

    with pytest.raises(RuntimeError, match="before undoing a migration"):
        await migrate._bind_route(client, base, require_undo=True, **bind)

    identity["capabilities"].append("migration_guarded_delete_v1")
    # The undo binds the same route the migration wrote its ledgers under.
    assert await migrate._bind_route(
        client, base, require_undo=True, **bind
    ) == await migrate._bind_route(client, base, **bind)


def _local_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str]]:
    from sibyl_cli import local

    env = tmp_path / ".env"
    env.write_text(
        "# SurrealDB\nSIBYL_SURREAL_USERNAME=root\nSIBYL_SURREAL_PASSWORD=generated-pw\n"
    )
    monkeypatch.setattr(local, "SIBYL_LOCAL_ENV", env)
    tried: list[tuple[str, str]] = []
    monkeypatch.setattr(migrate.httpx, "post", MagicMock(side_effect=AssertionError("no probe")))
    return tried


def _server_accepting(monkeypatch: pytest.MonkeyPatch, tried: list, accepted: tuple) -> None:
    def post(url: str, *, auth: tuple[str, str], **_kwargs: Any) -> Any:
        assert url == "http://localhost:8000/sql"
        tried.append(auth)
        return MagicMock(status_code=200 if auth == accepted else 401)

    monkeypatch.setattr(migrate.httpx, "post", post)


def test_a_sibyl_local_source_uses_its_generated_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tried = _local_env(monkeypatch, tmp_path)
    _server_accepting(monkeypatch, tried, ("root", "generated-pw"))

    resolved = migrate._resolve_source_credentials("ws://localhost:8000/rpc", None, None)

    assert resolved == ("root", "generated-pw") and tried == [("root", "generated-pw")]


def test_a_dev_server_beside_an_old_local_install_still_takes_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tried = _local_env(monkeypatch, tmp_path)
    _server_accepting(monkeypatch, tried, ("root", "root"))

    resolved = migrate._resolve_source_credentials("ws://localhost:8000/rpc", None, None)

    assert resolved == ("root", "root")
    assert tried == [("root", "generated-pw"), ("root", "root")]


@pytest.mark.parametrize(
    ("url", "user", "password"),
    [
        ("ws://localhost:8000/rpc", "admin", None),
        ("ws://localhost:8000/rpc", None, "secret"),
        ("ws://admin:secret@localhost:8000/rpc", None, None),
        ("wss://db.example.com/rpc", None, None),
    ],
)
def test_given_or_remote_sources_never_receive_the_local_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str, user: Any, password: Any
) -> None:
    _local_env(monkeypatch, tmp_path)

    assert migrate._resolve_source_credentials(url, user, password) == (user, password)


def test_without_a_local_install_the_defaults_stand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sibyl_cli import local

    monkeypatch.setattr(local, "SIBYL_LOCAL_ENV", tmp_path / "missing.env")
    monkeypatch.setattr(migrate.httpx, "post", MagicMock(side_effect=AssertionError("no probe")))

    assert migrate._resolve_source_credentials("ws://localhost:8000/rpc", None, None) == (
        None,
        None,
    )


def test_an_undo_moves_the_route_to_fresh_operation_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrate, "_LEDGER_DIR", tmp_path)
    route = {
        "source_org": "s",
        "target_org_id": "t",
        "target_user_id": "u",
        "target_project_id": "p",
    }
    other = {**route, "target_project_id": "q"}
    before = migrate._key_namespace(route)
    # A route that was never undone keeps the keys earlier runs used.
    assert before == migrate._route_fingerprint(route)

    migrate._advance_epoch(route)
    after = migrate._key_namespace(route)
    migrate._advance_epoch(route)

    assert after != before
    assert migrate._key_namespace(route) not in {before, after}
    assert migrate._key_namespace(other) == migrate._route_fingerprint(other)
