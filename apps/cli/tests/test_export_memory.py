"""Tests for `sibyl export memory`, the `.sibyl/memory/` materializer."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.main import app

runner = CliRunner()


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def _entity(entity_id: str, entity_type: str, name: str, **metadata: object) -> dict[str, object]:
    return {
        "id": entity_id,
        "type": entity_type,
        "name": name,
        "description": "truncated preview",
        "metadata": {
            "description": f"Full body for {name}.",
            "updated_at": "2026-07-21T12:00:00Z",
            "project_id": "project_demo",
            "retrieval_count": 41,
            "last_recalled_at": "2026-07-25T23:59:59Z",
            "record_id": "entity:zk3jbstxj8mzl193fmqg",
            **metadata,
        },
    }


PROJECT = {"id": "project_demo", "name": "Demo Project", "description": "A demo."}


def _client() -> MagicMock:
    client = MagicMock()
    pools: dict[str, list[dict[str, object]]] = {
        "task": [_entity("task_one", "task", "Materialize memory", status="doing")],
        "decision": [_entity("decision_two", "decision", "Ship files-first memory")],
        "pattern": [_entity("pattern_one", "pattern", "Pool connections per org")],
    }
    by_id = {
        str(entity["id"]): entity for entities in pools.values() for entity in entities
    }

    async def explore(**kwargs: object) -> dict[str, object]:
        requested = kwargs.get("types")
        entity_type = requested[0] if isinstance(requested, list) and requested else ""
        return {"entities": list(pools.get(str(entity_type), []))}

    async def get_entity(entity_id: str) -> dict[str, object]:
        if entity_id == "project_demo":
            return dict(PROJECT)
        listed = by_id[entity_id]
        return {**listed, "content": f"Hydrated body for {listed['name']}."}

    client.explore = AsyncMock(side_effect=explore)
    client.get_entity = AsyncMock(side_effect=get_entity)
    return client


def _written_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run(tmp_path: Path, *extra: str) -> tuple[int, str, Path]:
    output = tmp_path / ".sibyl" / "memory"
    with patch(
        "sibyl_cli.export.get_client", return_value=_FakeClientContext(_client())
    ):
        result = runner.invoke(
            app,
            ["export", "memory", "--project", "project_demo", "--output", str(output), *extra],
        )
    return result.exit_code, result.stdout, output


def test_export_memory_materializes_the_documented_layout(tmp_path: Path) -> None:
    exit_code, stdout, output = _run(tmp_path)

    assert exit_code == 0, stdout
    assert (output / "README.md").is_file()
    assert (output / "index.md").is_file()
    assert (output / "recent.md").is_file()
    assert (output / "tasks.md").is_file()
    assert (output / "manifest.json").is_file()
    assert not (output / "handbook.md").exists()
    assert sorted(path.name for path in (output / "notes").iterdir()) == [
        "decision-ship-files-first-memory.md",
        "pattern-pool-connections-per-org.md",
    ]


def test_export_memory_is_byte_identical_across_runs(tmp_path: Path) -> None:
    first_code, _, output = _run(tmp_path)
    first = _written_bytes(output)
    second_code, _, _ = _run(tmp_path)
    second = _written_bytes(output)

    assert (first_code, second_code) == (0, 0)
    assert first == second


def test_export_memory_writes_read_only_files_by_default(tmp_path: Path) -> None:
    _, _, output = _run(tmp_path)

    assert stat.S_IMODE((output / "index.md").stat().st_mode) == 0o444

    _run(tmp_path, "--writable")
    assert stat.S_IMODE((output / "index.md").stat().st_mode) == 0o644


def test_export_memory_keeps_volatile_usage_counters_out_of_the_files(tmp_path: Path) -> None:
    _, _, output = _run(tmp_path)

    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        assert "retrieval_count" not in content, path
        assert "last_recalled_at" not in content, path
        assert "entity:zk3jbstxj8mzl193fmqg" not in content, path


def test_export_memory_content_is_greppable_without_the_server(tmp_path: Path) -> None:
    _, _, output = _run(tmp_path)

    index = (output / "index.md").read_text(encoding="utf-8")
    assert "`pattern_one`" in index
    assert "`decision_two`" in index

    note = (output / "notes" / "pattern-pool-connections-per-org.md").read_text(encoding="utf-8")
    assert 'sibyl_id: "pattern_one"' in note
    assert "Hydrated body for Pool connections per org." in note

    tasks = (output / "tasks.md").read_text(encoding="utf-8")
    assert "[doing] Materialize memory" in tasks
    assert "`task_one`" in tasks


def test_export_memory_json_receipt_carries_the_content_digest(tmp_path: Path) -> None:
    exit_code, stdout, output = _run(tmp_path, "--json")

    assert exit_code == 0, stdout
    receipt = json.loads(stdout)
    assert receipt["project_id"] == "project_demo"
    assert receipt["project_name"] == "Demo Project"
    assert receipt["notes"] == 2
    assert receipt["tasks"] == 1
    assert receipt["handbook"] is False
    assert receipt["read_only"] is True

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["content_digest"] == receipt["content_digest"]


def test_export_memory_requires_a_project(tmp_path: Path) -> None:
    with (
        patch("sibyl_cli.export.get_client", return_value=_FakeClientContext(_client())),
        patch("sibyl_cli.export.resolve_project_from_cwd", return_value=None),
    ):
        result = runner.invoke(
            app,
            ["export", "memory", "--output", str(tmp_path / "memory")],
        )

    assert result.exit_code == 1
    assert "sibyl project link" in result.stdout


def test_export_memory_falls_back_to_the_linked_project(tmp_path: Path) -> None:
    output = tmp_path / "memory"
    with (
        patch("sibyl_cli.export.get_client", return_value=_FakeClientContext(_client())),
        patch("sibyl_cli.export.resolve_project_from_cwd", return_value="project_demo"),
    ):
        result = runner.invoke(app, ["export", "memory", "--output", str(output), "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["project_id"] == "project_demo"


def test_export_memory_can_skip_tasks(tmp_path: Path) -> None:
    exit_code, stdout, output = _run(tmp_path, "--tasks", "0", "--json")

    assert exit_code == 0, stdout
    assert json.loads(stdout)["tasks"] == 0
    assert "No open tasks" in (output / "tasks.md").read_text(encoding="utf-8")


def test_export_memory_hydrates_full_bodies_unless_asked_not_to(tmp_path: Path) -> None:
    note = "notes/pattern-pool-connections-per-org.md"

    _, _, hydrated = _run(tmp_path / "hydrated")
    assert "Hydrated body for" in (hydrated / note).read_text(encoding="utf-8")

    _, _, preview = _run(tmp_path / "preview", "--no-hydrate")
    content = (preview / note).read_text(encoding="utf-8")
    assert "Hydrated body for" not in content
    assert "Full body for Pool connections per org." in content


def test_export_memory_spends_the_note_budget_across_every_type(tmp_path: Path) -> None:
    """One shared query lets the first-ordered type eat the whole budget."""
    client = _client()
    client.explore = AsyncMock(
        side_effect=lambda **kwargs: {
            "entities": [
                _entity(f"decision_{index}", "decision", f"Decision {index}")
                for index in range(10)
            ]
            if kwargs.get("types") == ["decision"]
            else [_entity("pattern_one", "pattern", "Pool connections per org")]
            if kwargs.get("types") == ["pattern"]
            else []
        }
    )

    output = tmp_path / "memory"
    with patch("sibyl_cli.export.get_client", return_value=_FakeClientContext(client)):
        result = runner.invoke(
            app,
            [
                "export",
                "memory",
                "--project",
                "project_demo",
                "--output",
                str(output),
                "--notes",
                "4",
                "--tasks",
                "0",
                "--no-hydrate",
                "--json",
            ],
        )

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["notes"] == 4
    types = sorted(path.name.split("-", 1)[0] for path in (output / "notes").iterdir())
    assert types == ["decision", "decision", "decision", "pattern"]
