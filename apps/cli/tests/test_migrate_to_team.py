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

SOURCE_ORG = "source-org"
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
    return directory


def _target(monkeypatch: pytest.MonkeyPatch, current: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(
        return_value={"organization": {k: current[k] for k in ("id", "slug", "name")}}
    )
    client.list_orgs = AsyncMock(return_value={"orgs": [PERSONAL, TEAM]})
    client.get_entity = AsyncMock(return_value={"id": PROJECT, "name": "V2"})
    client.remember_raw_memory = AsyncMock(
        side_effect=[{"id": f"target-{n}"} for n in range(len(ROWS))]
    )
    client.memory_inspect = AsyncMock(return_value={"id": "found"})
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
    ledger = ledger_dir / f"{SOURCE_ORG}--team--{TEAM['id']}--{PROJECT}.json"
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
    assert "Adopted 2 receipts" in result.stdout
    assert "Migrated 1 raw memories (2 already in ledger)" in result.stdout
    assert client.remember_raw_memory.await_count == 1
    assert not legacy.exists()
    assert legacy.with_suffix(".adopted.json").exists()


def test_a_legacy_ledger_from_another_org_is_refused(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _legacy_ledger(ledger_dir, {row["uuid"]: f"t-{row['uuid']}" for row in ROWS})
    client = _target(monkeypatch, TEAM)
    client.memory_inspect = AsyncMock(
        side_effect=SibylClientError("API error: not found", status_code=404)
    )

    result = _run()

    assert result.exit_code == 1
    assert "they are not in Acme (acme)" in result.stdout
    assert "Move it aside" in result.stdout
    client.remember_raw_memory.assert_not_awaited()
    assert legacy.exists()


def test_a_dry_run_reports_adoption_without_touching_the_ledgers(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _legacy_ledger(ledger_dir, {ROWS[0]["uuid"]: "t-0"})
    before = sorted(p.name for p in ledger_dir.iterdir())
    _target(monkeypatch, TEAM)

    result = _run("--dry-run")

    assert result.exit_code == 0, result.stdout
    assert "Would adopt 1 receipts" in result.stdout
    assert "Would migrate 2 raw memories (1 already in ledger)" in result.stdout
    assert sorted(p.name for p in ledger_dir.iterdir()) == before
    assert legacy.exists()
