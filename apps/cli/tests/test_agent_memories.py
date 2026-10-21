from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli import agent_memories as am
from sibyl_cli import config_store
from sibyl_cli.client import SibylClientError
from sibyl_cli.main import app

REPO = "/work/acme"
CLAUDE_FEEDBACK = """---
name: run-tests-on-the-devbox
description: Run suites on the devbox, not the laptop
metadata:
  type: feedback
---

Heavy suites saturate the laptop.

**Why:** shared machine.
"""
CODEX_SUMMARY = f"""thread_id: 019e
updated_at: 2026-10-19T22:58:16+00:00
cwd: {REPO}
git_branch: main

# Fixed the flaky retry test

## Task 1: Fix it

Outcome: success
"""


def _claude_home(tmp_path: Path, *, repo: str = REPO) -> tuple[Path, Path]:
    home = tmp_path / ".claude"
    memory = home / "projects" / am.claude_slug(repo) / "memory"
    memory.mkdir(parents=True)
    (memory / "devbox.md").write_text(CLAUDE_FEEDBACK)
    (memory / "MEMORY.md").write_text("- [Devbox](devbox.md) — index line\n")
    (memory / "blank.md").write_text("---\nname: blank\n---\n\n")
    (memory / "linked.md").symlink_to(memory / "devbox.md")
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": {repo: {}}}))
    return home, config


def _codex_home(tmp_path: Path) -> Path:
    home = tmp_path / ".codex"
    summaries = home / "memories" / "rollout_summaries"
    summaries.mkdir(parents=True)
    (summaries / "2026-10-19-fix.md").write_text(CODEX_SUMMARY)
    (home / "memories" / "memory_summary.md").write_text("## User Profile\n\nShips fast.\n")
    (home / "memories" / "MEMORY.md").write_text("# Task Group index\n")
    (home / "memories" / "raw_memories.md").write_text("# Raw Memories\n")
    return home


class TestParsing:
    def test_frontmatter_reads_nested_type_and_keeps_the_body(self) -> None:
        meta, body = am.split_frontmatter(CLAUDE_FEEDBACK)
        assert meta["metadata"]["type"] == "feedback"
        assert body.startswith("Heavy suites")

    def test_text_without_frontmatter_is_all_body(self) -> None:
        assert am.split_frontmatter("just a note\n") == ({}, "just a note\n")

    def test_malformed_frontmatter_yields_no_metadata(self) -> None:
        meta, body = am.split_frontmatter("---\nname: [unclosed\n---\nbody\n")
        assert meta == {}
        assert body == "body"

    def test_slug_dashes_every_non_alphanumeric(self) -> None:
        assert am.claude_slug("/Users/me/dev/sibyl.ist") == "-Users-me-dev-sibyl-ist"

    def test_a_slug_two_projects_share_resolves_to_nothing(self, tmp_path: Path) -> None:
        config = tmp_path / ".claude.json"
        config.write_text(json.dumps({"projects": {"/a/b-c": {}, "/a/b/c": {}, "/x": {}}}))
        paths = am.claude_project_paths(config)
        assert paths[am.claude_slug("/a/b-c")] is None
        assert paths[am.claude_slug("/x")] == Path("/x")


class TestDiscovery:
    def test_claude_code_memories_are_typed_titled_and_tied_to_their_repo(
        self, tmp_path: Path
    ) -> None:
        home, config = _claude_home(tmp_path)
        found = am.discover_claude_code(home, config)
        assert [memory.file.name for memory in found.memories] == ["devbox.md"]
        memory = found.memories[0]
        assert memory.kind == "rule"
        assert memory.memory_type == "feedback"
        assert memory.title == "Run tests on the devbox"
        assert memory.repo == Path(REPO)
        assert memory.content.startswith("Run suites on the devbox, not the laptop\n\nHeavy")
        reasons = {skip.file.name: skip.reason for skip in found.skipped}
        assert reasons == {"blank.md": "empty", "linked.md": "symlink"}

    def test_an_unknown_slug_leaves_the_repo_unresolved(self, tmp_path: Path) -> None:
        home, config = _claude_home(tmp_path)
        config.write_text(json.dumps({"projects": {}}))
        found = am.discover_claude_code(home, config)
        assert found.memories[0].repo is None

    def test_codex_sessions_carry_their_cwd_and_the_profile_stays_personal(
        self, tmp_path: Path
    ) -> None:
        found = am.discover_codex(_codex_home(tmp_path))
        by_type = {memory.memory_type: memory for memory in found.memories}
        assert set(by_type) == {"profile", "session"}
        session = by_type["session"]
        assert session.kind == "session"
        assert session.title == "Fixed the flaky retry test"
        assert session.repo == Path(REPO)
        assert session.details == {
            "thread_id": "019e",
            "updated_at": "2026-10-19T22:58:16+00:00",
            "git_branch": "main",
        }
        assert not session.content.startswith("thread_id")
        assert by_type["profile"].personal
        assert by_type["profile"].repo is None

    def test_missing_agent_homes_find_nothing(self, tmp_path: Path) -> None:
        found = am.discover(
            am.AGENTS,
            claude_home=tmp_path / "no-claude",
            claude_config=tmp_path / "no.json",
            codex_home=tmp_path / "no-codex",
        )
        assert found.memories == []

    def test_source_ids_are_stable_and_name_the_project(self, tmp_path: Path) -> None:
        home, config = _claude_home(tmp_path)
        first = am.discover_claude_code(home, config).memories[0]
        again = am.discover_claude_code(home, config).memories[0]
        assert first.source_id(None) == again.source_id(None)
        assert first.source_id(None).endswith(":unlinked")
        assert first.source_id("project_1").endswith(":project_1")
        assert first.source_id(None).startswith("agent-memory:claude-code:")


def _memory(
    *,
    repo: str | None = REPO,
    memory_type: str = "project",
    content: str = "body",
    title: str = "Note",
) -> am.AgentMemory:
    return am.AgentMemory(
        agent=am.CLAUDE_CODE,
        file=Path(f"/home/me/.claude/projects/x/memory/{title}.md"),
        relative=f"projects/x/memory/{title}.md",
        repo=Path(repo) if repo else None,
        memory_type=memory_type,
        kind=am.CLAUDE_KINDS.get(memory_type, "note"),
        title=title,
        content=content,
    )


class TestPlan:
    def _plan(self, memories: list[am.AgentMemory], **kwargs: Any) -> am.ImportPlan:
        defaults: dict[str, Any] = {
            "target_context": "team",
            "known_projects": {"project_1"},
            "share": False,
            "project_for_path": lambda path: "project_1",
            "context_for_path": lambda path: None,
        }
        defaults.update(kwargs)
        return am.build_plan(memories, **defaults)

    def test_a_linked_repo_attaches_its_project_privately(self) -> None:
        item = self._plan([_memory()]).imports[0]
        assert item.project_id == "project_1"
        assert item.scope == "private"
        assert item.source_id.endswith(":project_1")
        assert item.replaces_source_id is not None
        assert item.replaces_source_id.endswith(":unlinked")

    def test_share_makes_only_project_memories_visible(self) -> None:
        plan = self._plan([_memory(), _memory(repo=None, title="Loose")], share=True)
        scopes = {item.memory.title: item.scope for item in plan.imports}
        assert scopes == {"Note": "project", "Loose": "private"}

    def test_a_repo_pinned_to_another_context_is_left_for_that_server(self) -> None:
        plan = self._plan([_memory()], context_for_path=lambda path: "personal")
        assert plan.imports == []
        assert plan.elsewhere == {"personal": 1}

    def test_a_repo_pinned_to_the_target_context_imports(self) -> None:
        plan = self._plan([_memory()], context_for_path=lambda path: "team")
        assert len(plan.imports) == 1

    def test_a_project_the_server_does_not_know_attaches_nothing(self) -> None:
        item = self._plan([_memory()], known_projects=set()).imports[0]
        assert item.project_id is None
        assert item.replaces_source_id is None

    def test_personal_memories_never_attach_or_share(self) -> None:
        item = self._plan([_memory(memory_type="user")], share=True).imports[0]
        assert item.project_id is None
        assert item.scope == "private"


def _not_found() -> SibylClientError:
    return SibylClientError("memory_source_not_found", status_code=404)


def _source(content: str, *, state: str = "active", revision: int = 3) -> dict[str, Any]:
    return {
        "source": {
            "raw_content": content,
            "revision": revision,
            "lifecycle": {"state": state},
        }
    }


def _client(blame: dict[str, Any]) -> MagicMock:
    """A client whose blame answers per source suffix (``:unlinked`` or the project)."""
    client = MagicMock()

    async def memory_blame(source_id: str) -> dict[str, Any]:
        for suffix, answer in blame.items():
            if source_id.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise _not_found()

    client.memory_blame = AsyncMock(side_effect=memory_blame)
    client.correct_memory = AsyncMock(return_value={})
    client.remember_raw_memory = AsyncMock(return_value={"id": "raw-1"})
    return client


def _item(memory: am.AgentMemory, project_id: str | None = "project_1") -> am.PlannedImport:
    return am.PlannedImport(
        memory=memory,
        project_id=project_id,
        scope="private",
        source_id=memory.source_id(project_id),
        replaces_source_id=memory.source_id(None) if project_id else None,
    )


class TestImportOne:
    async def test_a_new_memory_is_stored_raw_with_its_source_and_kind(self) -> None:
        client = _client({})
        result = await am.import_one(client, _item(_memory(memory_type="feedback")), apply=True)
        assert result.outcome == "created"
        kwargs = client.remember_raw_memory.await_args.kwargs
        assert kwargs["source_id"].endswith(":project_1")
        assert kwargs["raw_content"] == "body"
        assert kwargs["memory_scope"] == "private"
        assert kwargs["scope_key"] is None
        assert kwargs["capture_surface"] == am.CAPTURE_SURFACE
        metadata = kwargs["metadata"]
        assert metadata["remember_kind"] == "rule"
        assert metadata["project_id"] == "project_1"
        assert metadata["capture_mode"] == "import"
        assert metadata["agent_memory"]["type"] == "feedback"
        client.correct_memory.assert_not_awaited()

    async def test_a_shared_memory_is_scoped_to_its_project(self) -> None:
        client = _client({})
        item = am.PlannedImport(
            memory=_memory(),
            project_id="project_1",
            scope="project",
            source_id=_memory().source_id("project_1"),
        )
        await am.import_one(client, item, apply=True)
        kwargs = client.remember_raw_memory.await_args.kwargs
        assert kwargs["memory_scope"] == "project"
        assert kwargs["scope_key"] == "project_1"

    async def test_an_unchanged_memory_writes_nothing(self) -> None:
        client = _client({":project_1": _source("body\n")})
        result = await am.import_one(client, _item(_memory()), apply=True)
        assert result.outcome == "unchanged"
        client.remember_raw_memory.assert_not_awaited()
        client.correct_memory.assert_not_awaited()

    async def test_a_changed_memory_is_revised_at_its_revision(self) -> None:
        client = _client({":project_1": _source("old body", revision=4)})
        result = await am.import_one(client, _item(_memory(content="new body")), apply=True)
        assert result.outcome == "updated"
        client.remember_raw_memory.assert_not_awaited()
        args = client.correct_memory.await_args
        assert args.args[0].endswith(":project_1")
        assert args.kwargs["action"] == "revise"
        assert args.kwargs["revised_content"] == "new body"
        assert args.kwargs["expected_revision"] == 4

    async def test_a_memory_retired_in_sibyl_is_left_alone(self) -> None:
        client = _client({":project_1": _source("old", state="deleted")})
        result = await am.import_one(client, _item(_memory(content="new")), apply=True)
        assert result.outcome == "retired"
        client.remember_raw_memory.assert_not_awaited()
        client.correct_memory.assert_not_awaited()

    async def test_attaching_a_project_supersedes_the_unattached_copy(self) -> None:
        client = _client({":unlinked": _source("body")})
        result = await am.import_one(client, _item(_memory()), apply=True)
        assert result.outcome == "relinked"
        client.remember_raw_memory.assert_awaited_once()
        args = client.correct_memory.await_args
        assert args.args[0].endswith(":unlinked")
        assert args.kwargs["action"] == "supersede"
        assert args.kwargs["replacement_source_id"].endswith(":project_1")

    async def test_an_attached_copy_already_in_place_still_retires_the_unattached_one(
        self,
    ) -> None:
        # An import that stopped between the two writes finishes on the next run.
        client = _client({":project_1": _source("body"), ":unlinked": _source("body")})
        result = await am.import_one(client, _item(_memory()), apply=True)
        assert result.outcome == "relinked"
        client.remember_raw_memory.assert_not_awaited()
        args = client.correct_memory.await_args
        assert args.args[0].endswith(":unlinked")
        assert args.kwargs["action"] == "supersede"

    async def test_a_retired_unattached_copy_is_not_superseded_again(self) -> None:
        client = _client({":unlinked": _source("body", state="superseded")})
        result = await am.import_one(client, _item(_memory()), apply=True)
        assert result.outcome == "created"
        client.correct_memory.assert_not_awaited()

    async def test_a_preview_reports_without_writing(self) -> None:
        client = _client({":unlinked": _source("body")})
        result = await am.import_one(client, _item(_memory()), apply=False)
        assert result.outcome == "relinked"
        client.remember_raw_memory.assert_not_awaited()
        client.correct_memory.assert_not_awaited()

    async def test_a_server_error_is_reported_as_a_failure(self) -> None:
        client = _client({":project_1": SibylClientError("boom", status_code=500)})
        result = await am.import_one(client, _item(_memory()), apply=True)
        assert result.outcome == "failed"
        assert "boom" in result.detail


class TestPathResolution:
    def test_project_and_context_resolve_for_any_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "sub").mkdir(parents=True)
        with (
            patch.object(config_store, "get_path_mappings", return_value={str(repo): "proj"}),
            patch.object(
                config_store, "get_path_context_mappings", return_value={str(repo): "team"}
            ),
        ):
            assert config_store.resolve_project_for_path(repo / "sub") == "proj"
            assert config_store.resolve_context_for_path(repo / "sub") == "team"
            assert config_store.resolve_project_for_path(tmp_path) is None


def _payload(output: str) -> dict[str, Any]:
    # The CLI prints its command banner before the JSON document.
    return json.loads(output[output.index("{") :])


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, *exc: object) -> None:
        return None


class TestCommand:
    def _run(self, tmp_path: Path, client: MagicMock, *args: str) -> Any:
        claude_home, _ = _claude_home(tmp_path)
        codex_home = _codex_home(tmp_path)
        client.explore = AsyncMock(return_value={"entities": [{"id": "project_1"}]})
        with (
            patch("sibyl_cli.ingest.get_client", return_value=_FakeClientContext(client)),
            patch("sibyl_cli.ingest.resolve_client_context_name", return_value="team"),
            patch.object(config_store, "get_path_mappings", return_value={REPO: "project_1"}),
            patch.object(config_store, "get_path_context_mappings", return_value={}),
        ):
            return CliRunner().invoke(
                app,
                [
                    "ingest",
                    "memories",
                    "--claude-dir",
                    str(claude_home),
                    "--codex-dir",
                    str(codex_home),
                    "--json",
                    *args,
                ],
            )

    def test_a_dry_run_previews_every_memory_and_writes_nothing(self, tmp_path: Path) -> None:
        client = _client({})
        result = self._run(tmp_path, client, "--dry-run")
        assert result.exit_code == 0, result.output
        payload = _payload(result.output)
        assert payload["found"] == 3
        assert payload["outcomes"]["created"] == 3
        assert payload["applied"] is False
        assert payload["attached_to_projects"] == 2
        client.remember_raw_memory.assert_not_awaited()

    def test_a_run_imports_new_memories(self, tmp_path: Path) -> None:
        client = _client({})
        result = self._run(tmp_path, client, "--yes")
        assert result.exit_code == 0, result.output
        payload = _payload(result.output)
        assert payload["applied"] is True
        assert client.remember_raw_memory.await_count == 3

    def test_the_import_never_buffers_writes_and_restores_the_client(self, tmp_path: Path) -> None:
        client = _client({})
        client.buffer_writes = True
        seen: list[bool] = []

        async def record(**_kwargs: object) -> dict[str, str]:
            seen.append(client.buffer_writes)
            return {"id": "raw"}

        client.remember_raw_memory = AsyncMock(side_effect=record)
        result = self._run(tmp_path, client, "--yes")
        assert result.exit_code == 0, result.output
        assert seen == [False, False, False]
        assert client.buffer_writes is True

    def test_an_unknown_agent_is_refused(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["ingest", "memories", "--from", "cursor"])
        assert result.exit_code == 1
        assert "Unknown agent" in result.output
