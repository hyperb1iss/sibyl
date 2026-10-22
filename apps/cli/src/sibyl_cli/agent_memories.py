"""Discover the memory files local coding agents keep, and import them.

Claude Code writes typed memory files under ``~/.claude/projects/<slug>/memory``,
and Codex writes one summary per session plus a user profile under
``~/.codex/memories``. Both are already distilled by the agent, so each file
lands whole as one raw memory carrying its kind, where recall finds it at once
and reflection can promote it, rather than as transcript turns. Raw memories
have their own identity: a graph entity is identified by its type and name, so
importing one per file would overwrite any entity that shares a title, and a
revision would leave the entity's text behind. Files are read here, on the
machine that has them, so the import works against any server.

Each file keeps a stable source ID that names the agent, the file, and the
project it attached to. A re-run therefore skips an unchanged file, revises
the memory of a changed one (the server keeps the prior content), and, once a
repository gets linked, imports a project-attached copy and marks the
unattached one superseded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from sibyl_cli.client import SibylClientError

CLAUDE_CODE = "claude-code"
CODEX = "codex"
AGENTS = (CLAUDE_CODE, CODEX)

SOURCE_PREFIX = "agent-memory"
CAPTURE_SURFACE = "agent-memory-import"
# Agent memory files are short notes; anything larger is not one of them.
MAX_FILE_BYTES = 256 * 1024

# Claude Code's memory types, mapped to the Sibyl kind that reads the same way:
# feedback is standing guidance, references point at outside resources, and
# project and user memories are notes about the work and the person.
CLAUDE_KINDS = {
    "feedback": "rule",
    "project": "note",
    "reference": "artifact",
    "user": "note",
}
# About the person rather than any project: never attached to one, never shared.
PERSONAL_TYPES = frozenset({"user", "profile"})
# Index files that only point at the memories themselves.
CLAUDE_INDEX_FILES = frozenset({"MEMORY.md"})

Outcome = Literal["created", "updated", "unchanged", "relinked", "kept", "failed"]
OUTCOMES: tuple[Outcome, ...] = (
    "created",
    "updated",
    "relinked",
    "unchanged",
    "kept",
    "failed",
)
WRITE_OUTCOMES = frozenset({"created", "updated", "relinked"})
# A contested memory is still live and can take a revision; it cannot stand in
# as the replacement a supersession points recall at.
_LIVE_STATES = frozenset({"active", "contested"})


@dataclass(frozen=True)
class AgentMemory:
    """One memory file an agent wrote."""

    agent: str
    file: Path
    # Path under the agent's home directory: the file's stable identity.
    relative: str
    repo: Path | None
    memory_type: str
    kind: str
    title: str
    content: str
    details: dict[str, str] = field(default_factory=dict)

    @property
    def personal(self) -> bool:
        return self.memory_type in PERSONAL_TYPES

    @property
    def file_key(self) -> str:
        identity = f"{self.agent}:{self.relative}".encode()
        return hashlib.sha256(identity).hexdigest()[:16]

    def source_id(self, owner: str, project_id: str | None) -> str:
        """The memory's identity: whose it is, which file, and where it attached.

        Source IDs resolve across the whole organization, and two people's agents
        keep files at the same relative paths (every Codex profile is
        ``memories/memory_summary.md``), so the owner is part of the identity.
        """
        owner_key = hashlib.sha256(owner.encode()).hexdigest()[:12]
        return (
            f"{SOURCE_PREFIX}:{owner_key}:{self.agent}:{self.file_key}:{project_id or 'unlinked'}"
        )


@dataclass(frozen=True)
class SkippedFile:
    file: Path
    reason: str


@dataclass
class Discovery:
    memories: list[AgentMemory] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)


@dataclass(frozen=True)
class PlannedImport:
    memory: AgentMemory
    project_id: str | None
    scope: str
    source_id: str
    # The unattached copy a project-attached import replaces, when there is one.
    replaces_source_id: str | None = None


@dataclass
class ImportPlan:
    imports: list[PlannedImport] = field(default_factory=list)
    # Memories whose repository is pinned to another context, by context name.
    elsewhere: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ImportResult:
    item: PlannedImport
    outcome: Outcome
    detail: str = ""


def _humanize(name: str) -> str:
    words = re.sub(r"[-_]+", " ", name).strip()
    return words[:1].upper() + words[1:] if words else name


def _read_text(path: Path, skipped: list[SkippedFile]) -> str | None:
    if path.is_symlink():
        skipped.append(SkippedFile(path, "symlink"))
        return None
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            skipped.append(SkippedFile(path, f"larger than {MAX_FILE_BYTES // 1024} KiB"))
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        skipped.append(SkippedFile(path, f"unreadable: {exc.__class__.__name__}"))
        return None


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``---`` YAML frontmatter block from a markdown body."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    try:
        meta = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        meta = None
    body = text[end + len("\n---") :]
    body = body.split("\n", 1)[1] if "\n" in body else ""
    return (meta if isinstance(meta, dict) else {}), body.strip("\n")


def claude_slug(path: str) -> str:
    """Claude Code names a project's directory by its path, non-alphanumerics dashed."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def claude_project_paths(config_file: Path) -> dict[str, Path | None]:
    """Map each Claude Code project slug to its path, from ``~/.claude.json``.

    The slug is lossy (``/a/b-c`` and ``/a/b/c`` share one), so a slug that two
    known projects produce maps to None rather than to a guess.
    """
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    paths: dict[str, Path | None] = {}
    if isinstance(projects, dict):
        for raw in projects:
            if not isinstance(raw, str):
                continue
            slug = claude_slug(raw)
            if slug in paths and paths[slug] != Path(raw):
                paths[slug] = None
            else:
                paths[slug] = Path(raw)
    return paths


def discover_claude_code(claude_home: Path, config_file: Path) -> Discovery:
    found = Discovery()
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return found
    slugs = claude_project_paths(config_file)
    for memory_dir in sorted(projects_dir.glob("*/memory")):
        if not memory_dir.is_dir() or memory_dir.is_symlink():
            continue
        repo = slugs.get(memory_dir.parent.name)
        for file in sorted(memory_dir.glob("*.md")):
            if file.name in CLAUDE_INDEX_FILES:
                continue
            text = _read_text(file, found.skipped)
            if text is None:
                continue
            meta, body = split_frontmatter(text)
            nested = meta.get("metadata")
            memory_type = str(
                (nested.get("type") if isinstance(nested, dict) else None)
                or meta.get("type")
                or "project"
            ).lower()
            description = str(meta.get("description") or "").strip()
            content = f"{description}\n\n{body}".strip() if description else body.strip()
            if not content:
                found.skipped.append(SkippedFile(file, "empty"))
                continue
            name = str(meta.get("name") or file.stem)
            found.memories.append(
                AgentMemory(
                    agent=CLAUDE_CODE,
                    file=file,
                    relative=file.relative_to(claude_home).as_posix(),
                    repo=repo,
                    memory_type=memory_type,
                    kind=CLAUDE_KINDS.get(memory_type, "note"),
                    title=_humanize(name),
                    content=content,
                    details={"memory_name": name},
                )
            )
    return found


def _codex_header(text: str) -> tuple[dict[str, str], str]:
    """Split the ``key: value`` header Codex writes above a rollout summary."""
    header: dict[str, str] = {}
    lines = text.split("\n")
    index = 0
    while index < len(lines) and lines[index].strip():
        key, sep, value = lines[index].partition(": ")
        if not sep or not re.fullmatch(r"[a-z_]+", key):
            return {}, text
        header[key] = value.strip()
        index += 1
    return header, "\n".join(lines[index:]).strip("\n")


def discover_codex(codex_home: Path) -> Discovery:
    found = Discovery()
    memories_dir = codex_home / "memories"
    if not memories_dir.is_dir():
        return found
    profile = memories_dir / "memory_summary.md"
    if profile.is_file():
        text = _read_text(profile, found.skipped)
        if text is not None and text.strip():
            found.memories.append(
                AgentMemory(
                    agent=CODEX,
                    file=profile,
                    relative=profile.relative_to(codex_home).as_posix(),
                    repo=None,
                    memory_type="profile",
                    kind="note",
                    title="Codex user profile",
                    content=text.strip(),
                )
            )
    summaries = memories_dir / "rollout_summaries"
    if not summaries.is_dir():
        return found
    for file in sorted(summaries.glob("*.md")):
        text = _read_text(file, found.skipped)
        if text is None:
            continue
        header, body = _codex_header(text)
        if not body.strip():
            found.skipped.append(SkippedFile(file, "empty"))
            continue
        heading = next(
            (line[2:].strip() for line in body.split("\n") if line.startswith("# ")),
            "",
        )
        cwd = header.get("cwd")
        details = {
            key: header[key] for key in ("thread_id", "updated_at", "git_branch") if key in header
        }
        found.memories.append(
            AgentMemory(
                agent=CODEX,
                file=file,
                relative=file.relative_to(codex_home).as_posix(),
                repo=Path(cwd) if cwd else None,
                memory_type="session",
                kind="session",
                title=heading or _humanize(file.stem),
                content=body.strip(),
                details=details,
            )
        )
    return found


def discover(
    agents: Iterable[str],
    *,
    claude_home: Path,
    claude_config: Path,
    codex_home: Path,
) -> Discovery:
    found = Discovery()
    for agent in dict.fromkeys(agents):
        part = (
            discover_claude_code(claude_home, claude_config)
            if agent == CLAUDE_CODE
            else discover_codex(codex_home)
        )
        found.memories.extend(part.memories)
        found.skipped.extend(part.skipped)
    return found


def build_plan(
    memories: Iterable[AgentMemory],
    *,
    owner: str,
    target_context: str | None,
    known_projects: set[str],
    share: bool,
    project_for_path: Callable[[Path], str | None],
    context_for_path: Callable[[Path], str | None],
) -> ImportPlan:
    """Route each memory the way commands run in its repository would.

    A repository pinned to another context belongs to that server and is left
    for a run against it, personal memories included: private on one server is
    still a copy on a server the person did not pick. A linked project the
    target server does not know (a link meant for another server) attaches
    nothing, and personal memories never attach.
    """
    plan = ImportPlan()
    seen: set[str] = set()
    for memory in memories:
        project_id: str | None = None
        if memory.repo is not None:
            pinned = context_for_path(memory.repo)
            if pinned and pinned != target_context:
                plan.elsewhere[pinned] = plan.elsewhere.get(pinned, 0) + 1
                continue
            if not memory.personal:
                linked = project_for_path(memory.repo)
                project_id = linked if linked in known_projects else None
        source_id = memory.source_id(owner, project_id)
        if source_id in seen:
            continue
        seen.add(source_id)
        scope = "project" if share and project_id else "private"
        plan.imports.append(
            PlannedImport(
                memory=memory,
                project_id=project_id,
                scope=scope,
                source_id=source_id,
                replaces_source_id=memory.source_id(owner, None) if project_id else None,
            )
        )
    return plan


async def _existing_source(client: Any, source_id: str) -> dict[str, Any] | None:
    try:
        blame = await client.memory_blame(source_id)
    except SibylClientError as exc:
        if exc.status_code == 404:
            return None
        raise
    source = blame.get("source") if isinstance(blame, dict) else None
    return source if isinstance(source, dict) else None


def _state(source: dict[str, Any]) -> str:
    """The source's lifecycle state.

    A source whose content is redacted (a deleted one, say) comes back with an
    empty ``lifecycle`` and its state under ``visibility`` instead.
    """
    lifecycle = source.get("lifecycle")
    state = lifecycle.get("state") if isinstance(lifecycle, dict) else None
    if not state:
        visibility = source.get("visibility")
        state = visibility.get("lifecycle_state") if isinstance(visibility, dict) else None
    return str(state) if state else "active"


def _replacement(source: dict[str, Any]) -> str | None:
    lifecycle = source.get("lifecycle")
    value = lifecycle.get("replacement_source_id") if isinstance(lifecycle, dict) else None
    return str(value) if value else None


def _file_prefix(item: PlannedImport) -> str:
    """Every source ID this file can have, whatever project it attached to."""
    return item.source_id.rsplit(":", 1)[0] + ":"


def _live(source: dict[str, Any]) -> bool:
    return _state(source) in _LIVE_STATES


def _metadata(item: PlannedImport) -> dict[str, Any]:
    memory = item.memory
    return {
        "agent_memory": {
            "agent": memory.agent,
            "type": memory.memory_type,
            "file": memory.relative,
            "repo": str(memory.repo) if memory.repo else None,
            **memory.details,
        }
    }


# Another import holds this key's lock right now, so its write may still land.
_IN_PROGRESS = "idempotency_in_progress"
_STRANDED_MESSAGE = (
    "the server did not store this memory and another import may be writing it; "
    "run the import again"
)


# How many keys one source may burn through before its create gives up. A
# key burns only through a real event (a reservation interrupted before the
# write, or the receipt of a memory since purged), and walking past one costs
# a single request, so this bound exists only to guarantee the walk ends.
_MAX_CAPTURE_KEYS = 1024


def _capture_keys(item: PlannedImport) -> Iterator[str]:
    """The idempotency keys one file's create may use, in order.

    Every key names the source and an attempt number, never the content, and
    every import walks the same sequence. Two imports of one file, whatever
    text each read, therefore always contend for the same lock: a write under
    any key makes the source exist for whoever comes next. A key is skipped
    only once the server shows that nothing holds it and nothing was stored
    under it (a reservation interrupted before the write, or the receipt of a
    memory since purged).
    """
    for attempt in range(_MAX_CAPTURE_KEYS):
        identity = item.source_id if attempt == 0 else f"{item.source_id}#{attempt}"
        yield f"agent-memory-import-{hashlib.sha256(identity.encode()).hexdigest()[:48]}"


async def _capture(client: Any, item: PlannedImport) -> None:
    """Store one memory file as one raw memory under its stable source ID.

    Interrupted writes are never re-run blindly, and every create is confirmed
    by reading the memory back, so a replayed or refused write never counts as
    stored.
    """
    memory = item.memory
    metadata: dict[str, Any] = {
        "capture_mode": "import",
        "capture_surface": CAPTURE_SURFACE,
        "remember_kind": memory.kind,
        **_metadata(item),
    }
    if item.project_id:
        metadata["project_id"] = item.project_id
    for key in _capture_keys(item):
        try:
            await client.remember_raw_memory(
                title=memory.title,
                raw_content=memory.content,
                source_id=item.source_id,
                memory_scope=item.scope,
                scope_key=item.project_id if item.scope == "project" else None,
                tags=[f"agent:{memory.agent}"],
                metadata=metadata,
                provenance={"remember_kind": memory.kind},
                capture_surface=CAPTURE_SURFACE,
                _idempotency_key=key,
            )
        except SibylClientError as exc:
            if exc.status_code != 409:
                raise
            if exc.error_code == _IN_PROGRESS:
                raise ValueError(_STRANDED_MESSAGE) from exc
        landed = await _existing_source(client, item.source_id)
        if landed is None:
            # Nothing holds this key and nothing was stored under it. Every
            # import moves on to the same next key, so they still contend.
            continue
        if str(landed.get("raw_content") or "").strip() != memory.content.strip():
            msg = "another import stored a different version of this file; run the import again"
            raise ValueError(msg)
        return
    raise ValueError(_STRANDED_MESSAGE)


async def _correct(client: Any, source_id: str, **kwargs: Any) -> None:
    """Apply a correction, failing when the server declined to apply it.

    A refused correction still answers 200, with ``applied`` false.
    """
    receipt = await client.correct_memory(source_id, **kwargs)
    if isinstance(receipt, dict) and receipt.get("applied") is False:
        reason = receipt.get("reason") or "correction not applied"
        msg = f"{kwargs.get('action')} on {source_id} was not applied: {reason}"
        raise ValueError(msg)


async def _supersede(client: Any, source_id: str, item: PlannedImport, reason: str) -> None:
    await _correct(
        client,
        source_id,
        action="supersede",
        reason=reason,
        replacement_source_id=item.source_id,
    )


# Moves between projects chain one copy to the next; this bounds the walk.
_MAX_CHAIN = 32


async def _current_copy(client: Any, item: PlannedImport) -> tuple[str, dict[str, Any] | None]:
    """The newest earlier copy of this file, following its own moves.

    Starts at the unattached copy and follows each supersession whose
    replacement is another import of the same file (the server records a
    replacement by its raw memory ID, so each hop is read back). It stops at
    a copy that is not superseded, or that something other than this file's
    import superseded; that copy's state then says whether a person retired
    it.
    """
    if item.replaces_source_id is None:
        return item.source_id, None
    current_id = item.replaces_source_id
    current = await _existing_source(client, current_id)
    for _ in range(_MAX_CHAIN):
        if current is None or _state(current) != "superseded":
            break
        replacement = _replacement(current)
        if not replacement:
            break
        following = await _existing_source(client, replacement)
        following_id = str(following.get("source_id") or "") if following else ""
        if following is None or not following_id.startswith(_file_prefix(item)):
            break
        current_id, current = following_id, following
    return current_id, current


async def import_one(client: Any, item: PlannedImport, *, apply: bool) -> ImportResult:
    """Create, revise, or leave one memory; with apply False, only report which.

    Whenever the project-attached copy is in place and active, an earlier copy
    of the same file that is still live (the unattached one, or the copy for a
    project the repository was linked to before) is superseded, so an import
    interrupted between the two writes finishes on the next run. A copy
    someone retired stays retired, and an import never changes a memory whose
    visibility differs from this run's.
    """
    memory = item.memory
    try:
        existing = await _existing_source(client, item.source_id)
        earlier: list[str] = []
        if item.replaces_source_id is not None:
            current_id, current = await _current_copy(client, item)
            if current is not None and current_id != item.source_id:
                if _live(current):
                    earlier.append(current_id)
                elif existing is None:
                    state = _state(current)
                    return ImportResult(item, "kept", f"{state} in Sibyl; left as it is")

        if existing is None:
            if apply:
                await _capture(client, item)
                for source_id in earlier:
                    await _supersede(client, source_id, item, "imported again attached elsewhere")
            return ImportResult(item, "relinked" if earlier else "created")

        if not _live(existing):
            return ImportResult(item, "kept", f"{_state(existing)} in Sibyl; left as it is")
        stored = existing.get("raw_content")
        if stored is None:
            return ImportResult(item, "kept", "content not readable; left as it is")
        current_scope = existing.get("memory_scope")
        if current_scope and current_scope != item.scope:
            return ImportResult(
                item,
                "kept",
                f"{current_scope} in Sibyl, this run imports {item.scope}; left as it is",
            )
        changed = str(stored).strip() != memory.content.strip()
        relink = bool(earlier) and _state(existing) == "active"
        if apply:
            if changed:
                await _correct(
                    client,
                    item.source_id,
                    action="revise",
                    reason=f"{memory.agent} memory file {memory.relative} changed",
                    revised_content=memory.content,
                    expected_revision=existing.get("revision"),
                )
            if relink:
                for source_id in earlier:
                    await _supersede(client, source_id, item, "imported again attached elsewhere")
        if relink:
            return ImportResult(item, "relinked")
        return ImportResult(item, "updated" if changed else "unchanged")
    except (SibylClientError, ValueError) as exc:
        return ImportResult(item, "failed", str(exc))


async def run_imports(
    client: Any,
    items: Iterable[PlannedImport],
    *,
    apply: bool,
    concurrency: int = 4,
) -> list[ImportResult]:
    gate = asyncio.Semaphore(concurrency)

    async def guarded(item: PlannedImport) -> ImportResult:
        async with gate:
            return await import_one(client, item, apply=apply)

    return list(await asyncio.gather(*(guarded(item) for item in items)))


def tally(results: Iterable[ImportResult]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(OUTCOMES, 0)
    for result in results:
        counts[result.outcome] += 1
    return counts


@dataclass
class MemoryImport:
    """A planned import: what was found, where it routes, and what would change."""

    discovery: Discovery
    plan: ImportPlan
    preview: list[ImportResult]
    results: list[ImportResult] = field(default_factory=list)

    @property
    def pending(self) -> list[PlannedImport]:
        return [result.item for result in self.preview if result.outcome in WRITE_OUTCOMES]

    def outcomes(self) -> list[ImportResult]:
        """Applied results where they exist, the preview for everything else."""
        applied = {result.item.source_id: result for result in self.results}
        return [applied.get(result.item.source_id, result) for result in self.preview]


async def plan_memory_import(
    client: Any,
    *,
    agents: Iterable[str],
    claude_home: Path,
    claude_config: Path,
    codex_home: Path,
    target_context: str | None,
    share: bool,
) -> MemoryImport:
    """Discover, route, and preview an import without writing anything."""
    from sibyl_cli import config_store
    from sibyl_cli.project_refs import list_accessible_projects

    found = discover(
        agents,
        claude_home=claude_home,
        claude_config=claude_config,
        codex_home=codex_home,
    )
    known: set[str] = set()
    owner = ""
    if found.memories:
        me = await client._request("GET", "/auth/me", _replay_pending=False)
        user = me.get("user") if isinstance(me, dict) else None
        owner = str(user.get("id") or "") if isinstance(user, dict) else ""
        if not owner:
            msg = "could not tell who is signed in; run sibyl auth login"
            raise ValueError(msg)
        projects = await list_accessible_projects(client, limit=500)
        known = {str(project["id"]) for project in projects if project.get("id")}
    plan = build_plan(
        found.memories,
        owner=owner,
        target_context=target_context,
        known_projects=known,
        share=share,
        project_for_path=config_store.resolve_project_for_path,
        context_for_path=config_store.resolve_context_for_path,
    )
    preview = await run_imports(client, plan.imports, apply=False)
    return MemoryImport(discovery=found, plan=plan, preview=preview)


async def apply_memory_import(client: Any, job: MemoryImport) -> MemoryImport:
    """Write what the preview found new or changed."""
    job.results = await run_imports(client, job.pending, apply=True)
    return job
