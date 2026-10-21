from __future__ import annotations

import asyncio
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
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "devbox.md").write_text(CLAUDE_FEEDBACK)
    (memory / "MEMORY.md").write_text("- [Devbox](devbox.md) — index line\n")
    (memory / "blank.md").write_text("---\nname: blank\n---\n\n")
    if not (memory / "linked.md").is_symlink():
        (memory / "linked.md").symlink_to(memory / "devbox.md")
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"projects": {repo: {}}}))
    return home, config


def _codex_home(tmp_path: Path) -> Path:
    home = tmp_path / ".codex"
    summaries = home / "memories" / "rollout_summaries"
    summaries.mkdir(parents=True, exist_ok=True)
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
        assert first.source_id("u", None) == again.source_id("u", None)
        assert first.source_id("u", None).endswith(":unlinked")
        assert first.source_id("u", "project_1").endswith(":project_1")
        assert first.source_id("u", None).startswith("agent-memory:")


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


OWNER = "user-1"


def _plan(memories: list[am.AgentMemory], **kwargs: Any) -> am.ImportPlan:
    defaults: dict[str, Any] = {
        "owner": OWNER,
        "target_context": "team",
        "known_projects": {"project_1"},
        "share": False,
        "project_for_path": lambda path: "project_1",
        "context_for_path": lambda path: None,
    }
    defaults.update(kwargs)
    return am.build_plan(memories, **defaults)


class TestPlan:
    def test_a_linked_repo_attaches_its_project_privately(self) -> None:
        item = _plan([_memory()]).imports[0]
        assert item.project_id == "project_1"
        assert item.scope == "private"
        assert item.source_id == _memory().source_id(OWNER, "project_1")
        assert item.replaces_source_id == _memory().source_id(OWNER, None)

    def test_share_makes_only_project_memories_visible(self) -> None:
        plan = _plan([_memory(), _memory(repo=None, title="Loose")], share=True)
        scopes = {item.memory.title: item.scope for item in plan.imports}
        assert scopes == {"Note": "project", "Loose": "private"}

    def test_a_repo_pinned_to_another_context_is_left_for_that_server(self) -> None:
        plan = _plan([_memory()], context_for_path=lambda path: "personal")
        assert plan.imports == []
        assert plan.elsewhere == {"personal": 1}

    def test_a_personal_memory_still_follows_its_repository_pin(self) -> None:
        # Review finding: private on another server is still a copy there.
        plan = _plan([_memory(memory_type="user")], context_for_path=lambda path: "personal")
        assert plan.imports == []
        assert plan.elsewhere == {"personal": 1}

    def test_a_repo_pinned_to_the_target_context_imports(self) -> None:
        plan = _plan([_memory()], context_for_path=lambda path: "team")
        assert len(plan.imports) == 1

    def test_a_project_the_server_does_not_know_attaches_nothing(self) -> None:
        item = _plan([_memory()], known_projects=set()).imports[0]
        assert item.project_id is None
        assert item.replaces_source_id is None

    def test_personal_memories_never_attach_or_share(self) -> None:
        item = _plan([_memory(memory_type="user")], share=True).imports[0]
        assert item.project_id is None
        assert item.scope == "private"

    def test_the_same_file_twice_plans_one_import(self) -> None:
        assert len(_plan([_memory(), _memory()]).imports) == 1

    def test_two_people_never_share_a_source_id(self) -> None:
        # Review finding: every Codex profile sits at the same relative path.
        memory = _memory(repo=None)
        assert memory.source_id("alice", None) != memory.source_id("bob", None)

    def test_asking_for_an_agent_twice_discovers_it_once(self, tmp_path: Path) -> None:
        found = am.discover(
            [am.CODEX, am.CODEX],
            claude_home=tmp_path / "none",
            claude_config=tmp_path / "none.json",
            codex_home=_codex_home(tmp_path),
        )
        assert len(found.memories) == 2


class FakeServer:
    """Raw memory sources as the API serves them, with its correction rules.

    Stateful so a test reads back what persisted rather than which calls were
    made. It answers the way the real serializer does (a deleted source comes
    back redacted, its state under ``visibility``; a superseded one names its
    replacement), refuses a correction the way the API does (200 with
    ``applied`` false), and keys creates the way the idempotency layer does:
    the same key replays for the same body and conflicts for another.
    """

    def __init__(self) -> None:
        self.sources: dict[str, dict[str, Any]] = {}
        self.records = 0
        self.keys: dict[str, tuple[str, str]] = {}
        self.refuse: set[str] = set()
        self.interrupt: str | None = None  # "after" or "before" the store
        # Keys a reservation holds without a record, or another import holds now.
        self.burned: set[str] = set()
        self.in_progress: set[str] = set()

    def add(
        self,
        source_id: str,
        content: str,
        *,
        state: str = "active",
        scope: str = "private",
        replacement: str | None = None,
    ) -> None:
        lifecycle: dict[str, Any] = {"state": state}
        if replacement:
            # The API records a replacement by its raw memory ID, not its source ID.
            target = self.sources.get(replacement)
            lifecycle["replacement_source_id"] = target["id"] if target else replacement
        self.sources[source_id] = {
            "id": f"raw:{len(self.sources) + 1}",
            "source_id": source_id,
            "raw_content": content,
            "revision": 1,
            "memory_scope": scope,
            "lifecycle": lifecycle,
        }

    def find(self, reference: str) -> str | None:
        """Resolve a source ID or raw memory ID, the way blame does."""
        if reference in self.sources:
            return reference
        return next(
            (key for key, source in self.sources.items() if source["id"] == reference), None
        )

    def state(self, source_id: str) -> str:
        return self.sources[source_id]["lifecycle"]["state"]

    def client(self) -> MagicMock:
        client = MagicMock()

        async def memory_blame(source_id: str) -> dict[str, Any]:
            await asyncio.sleep(0)
            found = self.find(source_id)
            if found is None:
                raise SibylClientError("memory_source_not_found", status_code=404)
            source = dict(self.sources[found])
            if source["lifecycle"]["state"] == "deleted":
                source.update(
                    raw_content=None, lifecycle={}, visibility={"lifecycle_state": "deleted"}
                )
            return {"source": source}

        async def remember_raw_memory(**kwargs: Any) -> dict[str, Any]:
            key = kwargs["_idempotency_key"]
            body = (kwargs["source_id"], kwargs["raw_content"])
            if key in self.in_progress:
                raise SibylClientError(
                    "still in progress", status_code=409, error_code="idempotency_in_progress"
                )
            if key in self.burned:
                raise SibylClientError(
                    "write may have landed",
                    status_code=409,
                    error_code="idempotency_reconciliation_required",
                )
            if key in self.keys:
                if self.keys[key] != body:
                    raise SibylClientError(
                        "Idempotency-Key was already used for a different request",
                        status_code=409,
                    )
                return {"id": "replayed"}
            self.keys[key] = body
            if self.interrupt == "before":
                raise SibylClientError(
                    "write may have landed",
                    status_code=409,
                    error_code="idempotency_reconciliation_required",
                )
            self.records += 1
            self.add(kwargs["source_id"], kwargs["raw_content"], scope=kwargs["memory_scope"])
            if self.interrupt == "after":
                raise SibylClientError(
                    "write may have landed",
                    status_code=409,
                    error_code="idempotency_reconciliation_required",
                )
            return {"id": f"raw-{self.records}"}

        async def correct_memory(source_id: str, **kwargs: Any) -> dict[str, Any]:
            action = kwargs["action"]
            source = self.sources[source_id]
            if action in self.refuse:
                return {"applied": False, "reason": "refused"}
            if action == "supersede":
                target = self.find(kwargs["replacement_source_id"])
                replacement = self.sources.get(target) if target else None
                if replacement is None or replacement["lifecycle"]["state"] != "active":
                    return {"applied": False, "reason": "replacement is not recallable"}
                source["lifecycle"] = {
                    "state": "superseded",
                    "replacement_source_id": replacement["id"],
                }
            elif action == "revise":
                if kwargs["expected_revision"] != source["revision"]:
                    raise SibylClientError("revision conflict", status_code=409)
                source["raw_content"] = kwargs["revised_content"]
                source["revision"] += 1
            return {"applied": True}

        client.memory_blame = AsyncMock(side_effect=memory_blame)
        client.remember_raw_memory = AsyncMock(side_effect=remember_raw_memory)
        client.correct_memory = AsyncMock(side_effect=correct_memory)
        return client


def _item(
    memory: am.AgentMemory, project_id: str | None = "project_1", scope: str = "private"
) -> am.PlannedImport:
    return am.PlannedImport(
        memory=memory,
        project_id=project_id,
        scope=scope,
        source_id=memory.source_id(OWNER, project_id),
        replaces_source_id=memory.source_id(OWNER, None) if project_id else None,
    )


ATTACHED = _memory().source_id(OWNER, "project_1")
UNATTACHED = _memory().source_id(OWNER, None)


class TestImportOne:
    async def test_a_new_memory_is_stored_raw_with_its_source_and_kind(self) -> None:
        server = FakeServer()
        client = server.client()
        item = _item(_memory(memory_type="feedback"))
        result = await am.import_one(client, item, apply=True)
        assert result.outcome == "created"
        assert server.sources[item.source_id]["raw_content"] == "body"
        kwargs = client.remember_raw_memory.await_args.kwargs
        assert kwargs["memory_scope"] == "private"
        assert kwargs["scope_key"] is None
        assert kwargs["capture_surface"] == am.CAPTURE_SURFACE
        assert kwargs["_idempotency_key"].startswith("agent-memory-import-")
        metadata = kwargs["metadata"]
        assert metadata["remember_kind"] == "rule"
        assert metadata["project_id"] == "project_1"
        assert metadata["capture_mode"] == "import"
        assert metadata["agent_memory"]["type"] == "feedback"

    async def test_a_shared_memory_is_scoped_to_its_project(self) -> None:
        client = FakeServer().client()
        await am.import_one(client, _item(_memory(), scope="project"), apply=True)
        kwargs = client.remember_raw_memory.await_args.kwargs
        assert kwargs["memory_scope"] == "project"
        assert kwargs["scope_key"] == "project_1"

    async def test_an_unchanged_memory_writes_nothing(self) -> None:
        server = FakeServer()
        server.add(ATTACHED, "body\n")
        result = await am.import_one(server.client(), _item(_memory()), apply=True)
        assert result.outcome == "unchanged"
        assert server.records == 0
        assert server.sources[ATTACHED]["revision"] == 1

    async def test_a_changed_memory_is_revised_in_place(self) -> None:
        server = FakeServer()
        server.add(ATTACHED, "old body")
        result = await am.import_one(
            server.client(), _item(_memory(content="new body")), apply=True
        )
        assert result.outcome == "updated"
        assert server.sources[ATTACHED]["raw_content"] == "new body"
        assert server.sources[ATTACHED]["revision"] == 2
        assert server.records == 0

    async def test_a_private_run_never_revises_a_shared_memory(self) -> None:
        # Review finding: the revision would publish new text to the project.
        server = FakeServer()
        server.add(ATTACHED, "old body", scope="project")
        result = await am.import_one(
            server.client(), _item(_memory(content="secret addition")), apply=True
        )
        assert result.outcome == "kept"
        assert server.sources[ATTACHED]["raw_content"] == "old body"

    async def test_a_memory_retired_in_sibyl_is_left_alone(self) -> None:
        server = FakeServer()
        server.add(ATTACHED, "old", state="deleted")
        result = await am.import_one(server.client(), _item(_memory(content="new")), apply=True)
        assert result.outcome == "kept"
        assert server.sources[ATTACHED]["raw_content"] == "old"

    async def test_attaching_a_project_supersedes_the_unattached_copy(self) -> None:
        server = FakeServer()
        server.add(UNATTACHED, "body")
        result = await am.import_one(server.client(), _item(_memory()), apply=True)
        assert result.outcome == "relinked"
        assert server.state(ATTACHED) == "active"
        assert server.state(UNATTACHED) == "superseded"

    async def test_an_interrupted_relink_finishes_on_the_next_run(self) -> None:
        server = FakeServer()
        server.add(ATTACHED, "body")
        server.add(UNATTACHED, "body")
        result = await am.import_one(server.client(), _item(_memory()), apply=True)
        assert result.outcome == "relinked"
        assert server.state(UNATTACHED) == "superseded"
        assert server.records == 0

    async def test_a_contested_attached_copy_is_not_used_as_a_replacement(self) -> None:
        # Review finding: the server refuses a non-recallable replacement.
        server = FakeServer()
        server.add(ATTACHED, "body", state="contested")
        server.add(UNATTACHED, "body")
        result = await am.import_one(server.client(), _item(_memory()), apply=True)
        assert result.outcome == "unchanged"
        assert server.state(UNATTACHED) == "active"

    async def test_a_refused_correction_is_a_failure_not_a_success(self) -> None:
        # Review finding: a refused correction still answers 200.
        server = FakeServer()
        server.add(ATTACHED, "old")
        server.refuse.add("revise")
        result = await am.import_one(server.client(), _item(_memory(content="new")), apply=True)
        assert result.outcome == "failed"
        assert "not applied" in result.detail
        assert server.sources[ATTACHED]["raw_content"] == "old"

    async def test_a_retired_unattached_copy_keeps_the_link_from_recreating_it(self) -> None:
        # Review finding: linking the repository brought deleted memories back.
        server = FakeServer()
        server.add(UNATTACHED, "body", state="deleted")
        result = await am.import_one(server.client(), _item(_memory()), apply=True)
        assert result.outcome == "kept"
        assert ATTACHED not in server.sources
        assert server.records == 0

    async def test_two_imports_racing_on_one_memory_land_one_record(self) -> None:
        # Review finding: both saw it missing; the shared key collapses them.
        server = FakeServer()
        item = _item(_memory(), project_id=None)
        results = await asyncio.gather(
            am.import_one(server.client(), item, apply=True),
            am.import_one(server.client(), item, apply=True),
        )
        assert server.records == 1
        assert {result.outcome for result in results} <= {"created", "unchanged"}

    async def test_racing_imports_of_two_versions_of_a_file_land_one_record(self) -> None:
        # Review finding: the key used to carry the content, so each snapshot won.
        server = FakeServer()
        old = _item(_memory(content="old"), project_id=None)
        new = _item(_memory(content="new"), project_id=None)
        results = await asyncio.gather(
            am.import_one(server.client(), old, apply=True),
            am.import_one(server.client(), new, apply=True),
        )
        # Whichever lands first wins; the other either fails asking for a
        # re-run or revises it in place. Never a second record.
        assert server.records == 1
        assert server.sources[old.source_id]["raw_content"] in {"old", "new"}
        failed = [result for result in results if result.outcome == "failed"]
        assert all("run the import again" in result.detail for result in failed)

    async def test_an_interrupted_write_that_landed_counts_as_created(self) -> None:
        server = FakeServer()
        server.interrupt = "after"
        result = await am.import_one(server.client(), _item(_memory()), apply=True)
        assert result.outcome == "created"
        assert server.records == 1

    async def test_an_interrupted_write_is_never_repeated_blindly(self) -> None:
        # Review finding: taking over the reservation wrote a second record.
        server = FakeServer()
        server.interrupt = "before"
        result = await am.import_one(server.client(), _item(_memory()), apply=True)
        assert result.outcome == "failed"
        assert server.records == 0

    async def test_a_purged_memory_can_be_imported_again(self) -> None:
        # Review finding: the first key's receipt outlives the memory, and a
        # replay of it stores nothing, so the content key writes instead.
        server = FakeServer()
        item = _item(_memory(), project_id=None)
        assert (await am.import_one(server.client(), item, apply=True)).outcome == "created"
        del server.sources[item.source_id]
        result = await am.import_one(server.client(), item, apply=True)
        assert result.outcome == "created"
        assert server.records == 2
        assert server.state(item.source_id) == "active"

    async def test_a_reservation_that_never_wrote_does_not_strand_the_file(self) -> None:
        # Review finding: every retry used to hit the same burned key.
        server = FakeServer()
        item = _item(_memory(), project_id=None)
        server.burned.add(am._capture_keys(item)[0])
        result = await am.import_one(server.client(), item, apply=True)
        assert result.outcome == "created"
        assert server.records == 1

    async def test_racing_recoveries_of_two_versions_land_one_record(self) -> None:
        # Review finding: content-keyed fallbacks took independent locks.
        server = FakeServer()
        old = _item(_memory(content="old"), project_id=None)
        new = _item(_memory(content="new"), project_id=None)
        server.burned.add(am._capture_keys(old)[0])
        await asyncio.gather(
            am.import_one(server.client(), old, apply=True),
            am.import_one(server.client(), new, apply=True),
        )
        assert server.records == 1
        assert am._capture_keys(old) == am._capture_keys(new)

    async def test_repeated_purges_never_strand_an_unchanged_file(self) -> None:
        # Review finding: two fixed keys ran out after two purges.
        server = FakeServer()
        item = _item(_memory(), project_id=None)
        for round_ in range(4):
            result = await am.import_one(server.client(), item, apply=True)
            assert result.outcome == "created", round_
            del server.sources[item.source_id]
        assert server.records == 4

    async def test_a_write_still_in_progress_is_never_raced_with_another_key(self) -> None:
        server = FakeServer()
        item = _item(_memory(), project_id=None)
        server.in_progress.add(am._capture_keys(item)[0])
        result = await am.import_one(server.client(), item, apply=True)
        assert result.outcome == "failed"
        assert "run the import again" in result.detail
        assert server.records == 0

    async def test_a_deleted_unattached_copy_as_the_server_serves_it_blocks_the_link(
        self,
    ) -> None:
        # Review finding: a redacted source carries its state under visibility.
        server = FakeServer()
        server.add(UNATTACHED, "body", state="deleted")
        client = server.client()
        blame = await client.memory_blame(UNATTACHED)
        assert blame["source"]["lifecycle"] == {}
        result = await am.import_one(client, _item(_memory()), apply=True)
        assert result.outcome == "kept"
        assert ATTACHED not in server.sources

    async def test_moving_to_another_project_follows_the_earlier_import(self) -> None:
        # Review finding: the importer's own supersession read as retirement.
        server = FakeServer()
        server.add(ATTACHED, "body")
        server.add(UNATTACHED, "body", state="superseded", replacement=ATTACHED)
        moved = _item(_memory(), project_id="project_2")
        result = await am.import_one(server.client(), moved, apply=True)
        assert result.outcome == "relinked"
        assert server.state(moved.source_id) == "active"
        assert server.state(ATTACHED) == "superseded"
        replacement = server.sources[ATTACHED]["lifecycle"]["replacement_source_id"]
        assert server.find(replacement) == moved.source_id

    async def test_a_project_copy_someone_deleted_is_not_recreated_elsewhere(self) -> None:
        # Review finding: following the move must still respect its retirement.
        server = FakeServer()
        server.add(ATTACHED, "body", state="deleted")
        server.add(UNATTACHED, "body", state="superseded", replacement=ATTACHED)
        moved = _item(_memory(), project_id="project_2")
        result = await am.import_one(server.client(), moved, apply=True)
        assert result.outcome == "kept"
        assert moved.source_id not in server.sources

    async def test_each_move_retires_the_copy_before_it(self) -> None:
        # Review finding: the walk stopped after one hop and left two live copies.
        server = FakeServer()
        second = _item(_memory(), project_id="project_2")
        server.add(second.source_id, "body")
        server.add(ATTACHED, "body", state="superseded", replacement=second.source_id)
        server.add(UNATTACHED, "body", state="superseded", replacement=ATTACHED)
        third = _item(_memory(), project_id="project_3")
        result = await am.import_one(server.client(), third, apply=True)
        assert result.outcome == "relinked"
        assert server.state(third.source_id) == "active"
        assert server.state(second.source_id) == "superseded"

    async def test_a_rerun_repairs_two_live_copies(self) -> None:
        server = FakeServer()
        second = _item(_memory(), project_id="project_2")
        third = _item(_memory(), project_id="project_3")
        server.add(second.source_id, "body")
        server.add(third.source_id, "body")
        server.add(ATTACHED, "body", state="superseded", replacement=second.source_id)
        server.add(UNATTACHED, "body", state="superseded", replacement=ATTACHED)
        result = await am.import_one(server.client(), third, apply=True)
        assert result.outcome == "relinked"
        assert server.state(second.source_id) == "superseded"
        assert server.state(third.source_id) == "active"
        assert server.records == 0

    async def test_a_supersession_someone_else_chose_still_blocks_the_link(self) -> None:
        server = FakeServer()
        server.add(UNATTACHED, "body", state="superseded", replacement="note:hand-written")
        result = await am.import_one(server.client(), _item(_memory()), apply=True)
        assert result.outcome == "kept"
        assert ATTACHED not in server.sources

    async def test_a_preview_reports_without_writing(self) -> None:
        server = FakeServer()
        server.add(UNATTACHED, "body")
        result = await am.import_one(server.client(), _item(_memory()), apply=False)
        assert result.outcome == "relinked"
        assert server.records == 0
        assert server.state(UNATTACHED) == "active"

    async def test_a_server_error_is_reported_as_a_failure(self) -> None:
        client = FakeServer().client()
        client.memory_blame = AsyncMock(side_effect=SibylClientError("boom", status_code=500))
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
        client._request = AsyncMock(return_value={"user": {"id": OWNER}})
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
        client = FakeServer().client()
        result = self._run(tmp_path, client, "--dry-run")
        assert result.exit_code == 0, result.output
        payload = _payload(result.output)
        assert payload["found"] == 3
        assert payload["outcomes"]["created"] == 3
        assert payload["applied"] is False
        assert payload["attached_to_projects"] == 2
        client.remember_raw_memory.assert_not_awaited()

    def test_a_run_imports_new_memories_and_a_rerun_changes_nothing(self, tmp_path: Path) -> None:
        server = FakeServer()
        result = self._run(tmp_path, server.client(), "--yes")
        assert result.exit_code == 0, result.output
        assert _payload(result.output)["applied"] is True
        assert server.records == 3
        again = self._run(tmp_path, server.client(), "--yes")
        assert again.exit_code == 0, again.output
        assert _payload(again.output)["outcomes"]["unchanged"] == 3
        assert server.records == 3

    def test_the_import_never_buffers_writes_and_restores_the_client(self, tmp_path: Path) -> None:
        client = FakeServer().client()
        client.buffer_writes = True
        seen: list[bool] = []
        store = client.remember_raw_memory.side_effect

        async def record(**kwargs: Any) -> dict[str, Any]:
            seen.append(client.buffer_writes)
            return await store(**kwargs)

        client.remember_raw_memory = AsyncMock(side_effect=record)
        result = self._run(tmp_path, client, "--yes")
        assert result.exit_code == 0, result.output
        assert seen == [False, False, False]
        assert client.buffer_writes is True

    def test_an_unknown_agent_is_refused(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["ingest", "memories", "--from", "cursor"])
        assert result.exit_code == 1
        assert "Unknown agent" in result.output
