"""Move a project's authored graph into a team server through the public API.

The raw replay carries a project's verbatim captures; this pass carries the
graph built on top of them: tasks, epics, decisions, error patterns,
procedures and the rest, with the links between them. Every write goes
through the ordinary authenticated API as the caller, so ownership lands on
the caller's target identity and no cluster access is involved.

Rows the server derives (topics and mention edges from the memory
projection, passages from the passage projection, projected facts) are left
behind, because the target re-derives them from what is written here. Rows
retired from recall stay behind too.

An entity declares links to targets that already exist. The plan orders
creation so most links land with the entity:
epics and milestones first, then tasks, then everything else, with typed
links and task dependencies as hard ordering constraints. An untyped link
carries no direction worth keeping, so it is declared from whichever end is
created later.

A ledger maps source ids to target ids and skips finished work. Durable
create receipts recover a completed write whose response was lost. A row
written while one of its link targets had failed receives only its missing
links once that target exists.
The link operation checks the actual target revision atomically, so edits
made on the team server win without rewriting the row's body or status.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable

# Categories the server stamps on rows it derives itself.
DERIVED_CATEGORIES = frozenset(
    {"memory_projection", "passage_projection", "memory_fact_projection"}
)
# Types that are always derived or that the target already holds.
SKIPPED_TYPES = frozenset({"topic", "passage", "project"})
# Lifecycle states that took a row out of recall on the source.
RETIRED_STATES = frozenset({"contested", "retired", "quarantined", "superseded", "withdrawn"})
# Edges the target re-derives or implies, never declared by this pass.
IMPLIED_EDGES = frozenset({"PART_OF", "MENTIONS"})
# Predicates the API accepts on `related_to`, keyed by stored edge name.
DECLARABLE = {
    "SUPERSEDES": "supersedes",
    "CONTRADICTS": "contradicts",
    "REQUIRES": "requires",
    "SUPPORTS": "supports",
    "DECIDES": "decides",
}
# Creation classes: lower classes are created first when nothing else decides.
_CLASS = {"epic": 0, "milestone": 0, "task": 1, "session": 2}
_OTHER_CLASS = 3
_TASK_STATUSES = frozenset({"backlog", "todo", "doing", "blocked", "review", "done", "archived"})
# Statuses older writers stored that the workflow no longer names.
_STATUS_ALIASES = {"completed": "done", "in_progress": "doing", "cancelled": "archived"}
_MAX_NAME = 200
# The server hashes each id part truncated to this many characters, so two
# titles that agree this far mint the same id however they end.
_ID_PART_CHARS = 100
_SAVE_EVERY = 100
_PROGRESS_EVERY = 250
_PROJECT_ID = re.compile(r"^(project|proj)_[0-9a-z]+$")
_ORGANIZATION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def validate_project_scope(project: str) -> str:
    """The source queries interpolate the scope key, so only a project id shape passes."""
    if not _PROJECT_ID.fullmatch(project):
        raise ValueError(f"'{project}' is not a project id (project_... or proj_...)")
    return project


def validate_organization_id(organization_id: str) -> str:
    """The source queries interpolate the org id, so only a UUID passes."""
    if not _ORGANIZATION_ID.fullmatch(organization_id.lower()):
        raise ValueError(f"'{organization_id}' is not an organization UUID")
    return organization_id.lower()


@dataclass(frozen=True)
class SourceEntity:
    uuid: str
    entity_type: str
    name: str
    memory_scope: str | None
    attributes: Mapping[str, Any]
    created_at: str | None = None
    updated_at: str | None = None
    status: str | None = None
    priority: str | None = None
    tags: tuple[str, ...] = ()
    summary: str | None = None
    content: str | None = None
    description: str | None = None

    @property
    def category(self) -> str | None:
        value = self.attributes.get("category")
        return str(value) if value else None

    @property
    def sensitive(self) -> bool:
        return bool(self.attributes.get("contains_sensitive"))


@dataclass(frozen=True)
class SourceEdge:
    name: str
    source_id: str
    target_id: str


@dataclass
class PlannedEntity:
    source: SourceEntity
    name: str
    scope: str | None
    layer: int = 0
    # (predicate or "", source id of the target)
    declares: list[tuple[str, str]] = field(default_factory=list)
    epic: str | None = None
    parent: str | None = None
    depends_on: list[str] = field(default_factory=list)
    coerced: list[dict[str, str]] = field(default_factory=list)


@dataclass
class GraphPlan:
    entities: list[PlannedEntity]
    skipped: dict[str, int]
    dropped_edges: list[str]
    edge_counts: dict[str, int]
    kept_private: int = 0

    @property
    def layers(self) -> list[list[PlannedEntity]]:
        grouped: dict[int, list[PlannedEntity]] = defaultdict(list)
        for planned in self.entities:
            grouped[planned.layer].append(planned)
        return [grouped[layer] for layer in sorted(grouped)]

    def limited(self, count: int | None, *, done: set[str] | None = None) -> GraphPlan:
        """Keep completed rows and at most `count` new rows in dependency order."""
        if count is None:
            return self
        done = done or set()
        remaining = max(count, 0)
        selected = []
        for node in self.entities:
            if node.source.uuid in done:
                selected.append(node)
            elif remaining:
                selected.append(node)
                remaining -= 1
        return GraphPlan(
            entities=selected,
            skipped=self.skipped,
            dropped_edges=self.dropped_edges,
            edge_counts=self.edge_counts,
            kept_private=self.kept_private,
        )


def _skip_reason(entity: SourceEntity, project: str) -> str | None:
    if entity.entity_type in SKIPPED_TYPES:
        return f"{entity.entity_type} (re-derived or already on the target)"
    if entity.category in DERIVED_CATEGORIES:
        return f"{entity.entity_type} {entity.category} (re-derived by the target)"
    if entity.uuid == project:
        return "the project itself"
    if not graph_metadata_recallable(entity.attributes) or (
        str(entity.attributes.get("lifecycle_state") or "") in RETIRED_STATES
    ):
        return f"{entity.entity_type} excluded from recall on the source"
    return None


def _target_scope(entity: SourceEntity, *, share_private: bool) -> str | None:
    # A row flagged as holding a credential or token never widens to the team.
    if entity.sensitive:
        return "private"
    if entity.memory_scope == "private":
        return "project" if share_private else "private"
    return entity.memory_scope


def _id_key(entity: SourceEntity, name: str) -> tuple[str, str]:
    """What the server hashes into an entity id: type, title, and category."""
    category = entity.category or "general"
    return (entity.entity_type, f"{name[:_ID_PART_CHARS]}:{category[:_ID_PART_CHARS]}")


def _unique_names(entities: Iterable[SourceEntity]) -> dict[str, str]:
    """Titles that mint distinct ids on the target.

    Two rows that would hash to the same id would land on one entity, the
    second replacing the first, so a repeat gets a counter placed where the
    hash still sees it.
    """
    taken: set[tuple[str, str]] = set()
    names: dict[str, str] = {}
    for entity in sorted(entities, key=lambda e: (e.created_at or "", e.uuid)):
        base = (entity.name or entity.uuid).strip()[:_MAX_NAME] or entity.uuid
        name = base
        counter = 1
        while _id_key(entity, name) in taken:
            counter += 1
            suffix = f" ({counter})"
            name = base[: _ID_PART_CHARS - len(suffix)].rstrip() + suffix
        taken.add(_id_key(entity, name))
        names[entity.uuid] = name
    return names


def _creation_order(
    selected: Mapping[str, SourceEntity], hard: Mapping[str, set[str]]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Topological order over hard constraints, breaking ties by class then age.

    Returns the order and the hard constraints dropped to break cycles
    (dependent, prerequisite): one edge per cycle, chosen on the cycle itself,
    so entities merely waiting on a cycle keep their links.
    """
    pending = {uuid: set(hard.get(uuid, ())) for uuid in selected}
    dependents: dict[str, set[str]] = defaultdict(set)
    for uuid, prereqs in pending.items():
        for prereq in prereqs:
            dependents[prereq].add(uuid)

    def rank(uuid: str) -> tuple[int, str, str]:
        entity = selected[uuid]
        return (_CLASS.get(entity.entity_type, _OTHER_CLASS), entity.created_at or "", uuid)

    ready = [rank(uuid) for uuid in selected if not pending[uuid]]
    heapq.heapify(ready)
    order: list[str] = []
    placed: set[str] = set()
    dropped: list[tuple[str, str]] = []
    while len(order) < len(selected):
        if not ready:
            # Every unplaced entity waits on another unplaced one, so walking
            # prerequisites from any of them must revisit a node: that loop
            # is a cycle, and one of its own edges is what has to go.
            walk: list[str] = []
            seen: dict[str, int] = {}
            node = min((u for u in selected if u not in placed), key=rank)
            while node not in seen:
                seen[node] = len(walk)
                walk.append(node)
                node = min(pending[node], key=rank)
            cycle = walk[seen[node] :]
            breaker = min(cycle, key=rank)
            successor = cycle[(cycle.index(breaker) + 1) % len(cycle)]
            dropped.append((breaker, successor))
            pending[breaker].discard(successor)
            dependents[successor].discard(breaker)
            if not pending[breaker]:
                heapq.heappush(ready, rank(breaker))
            continue
        _, _, uuid = heapq.heappop(ready)
        if uuid in placed:
            continue
        placed.add(uuid)
        order.append(uuid)
        for dependent in sorted(dependents.get(uuid, ())):
            pending[dependent].discard(uuid)
            if not pending[dependent] and dependent not in placed:
                heapq.heappush(ready, rank(dependent))
    return order, dropped


def build_plan(
    entities: Iterable[SourceEntity],
    edges: Iterable[SourceEdge],
    *,
    project: str,
    share_private: bool = False,
) -> GraphPlan:
    """Choose what to create, in what order, declaring which links."""
    skipped: dict[str, int] = defaultdict(int)
    selected: dict[str, SourceEntity] = {}
    for entity in entities:
        reason = _skip_reason(entity, project)
        if reason:
            skipped[reason] += 1
        else:
            selected[entity.uuid] = entity

    names = _unique_names(selected.values())
    planned = {
        uuid: PlannedEntity(
            source=entity,
            name=names[uuid],
            scope=_target_scope(entity, share_private=share_private),
        )
        for uuid, entity in selected.items()
    }

    hard: dict[str, set[str]] = defaultdict(set)
    untyped: list[SourceEdge] = []
    edge_counts: dict[str, int] = defaultdict(int)
    dropped_edges: list[str] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for edge in edges:
        if edge.name in IMPLIED_EDGES:
            continue
        subject, target = edge.source_id, edge.target_id
        if subject not in selected or target not in selected or subject == target:
            continue
        key = (edge.name, subject, target)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        edge_counts[edge.name] += 1
        subject_type = selected[subject].entity_type
        target_type = selected[target].entity_type
        if edge.name == "BELONGS_TO" and subject_type == "task" and target_type == "epic":
            planned[subject].epic = target
            hard[subject].add(target)
        elif edge.name == "BELONGS_TO" and subject_type == "task" and target_type == "task":
            planned[subject].parent = target
            hard[subject].add(target)
        elif edge.name == "DEPENDS_ON" and subject_type == "task" and target_type == "task":
            planned[subject].depends_on.append(target)
            hard[subject].add(target)
        elif edge.name in DECLARABLE:
            planned[subject].declares.append((DECLARABLE[edge.name], target))
            hard[subject].add(target)
        else:
            untyped.append(edge)

    order, broken = _creation_order(selected, hard)
    for dependent, prereq in broken:
        node = planned[dependent]
        dropped_edges.append(f"{dependent} -> {prereq}: ordering cycle")
        node.declares = [(p, t) for p, t in node.declares if t != prereq]
        node.depends_on = [t for t in node.depends_on if t != prereq]
        if node.epic == prereq:
            node.epic = None
        if node.parent == prereq:
            node.parent = None

    position = {uuid: index for index, uuid in enumerate(order)}
    declared_untyped: set[frozenset[str]] = set()
    for edge in untyped:
        first, second = sorted((edge.source_id, edge.target_id), key=position.__getitem__)
        pair = frozenset((first, second))
        later = planned[second]
        # The later end declares an untyped link to the earlier one. A
        # pair already linked untyped (or by a typed predicate) needs no
        # second declaration.
        already = any(t == first for _, t in later.declares) or any(
            t == second for _, t in planned[first].declares
        )
        if pair not in declared_untyped and not already:
            later.declares.append(("", first))
            declared_untyped.add(pair)
        if edge.name != "RELATED_TO":
            later.coerced.append(
                {
                    "type": edge.name,
                    "origin_source": edge.source_id,
                    "origin_target": edge.target_id,
                }
            )

    for uuid in order:
        node = planned[uuid]
        targets = [t for _, t in node.declares] + node.depends_on
        targets += [t for t in (node.epic, node.parent) if t]
        node.layer = 1 + max((planned[t].layer for t in targets), default=-1)

    return GraphPlan(
        entities=[planned[uuid] for uuid in order],
        skipped=dict(skipped),
        dropped_edges=dropped_edges,
        edge_counts=dict(edge_counts),
        kept_private=sum(
            1 for node in planned.values() if node.source.sensitive and node.scope == "private"
        ),
    )


def _link_targets(node: PlannedEntity) -> list[str]:
    targets = [t for _, t in node.declares] + node.depends_on
    return targets + [t for t in (node.epic, node.parent) if t]


def _payload(
    node: PlannedEntity,
    *,
    ids: Mapping[str, str],
    target_project_id: str,
    origin_org: str,
) -> tuple[dict[str, Any], list[str]]:
    """The create body for one entity, plus link targets that are not on the target yet."""
    source = node.source
    attributes = dict(source.attributes)
    content = str(
        source.content
        or attributes.get("content")
        or source.summary
        or source.description
        or attributes.get("description")
        or ""
    )
    missing = [target for target in _link_targets(node) if target not in ids]
    related: list[str] = []
    for predicate, target in node.declares:
        if target in ids:
            related.append(f"{predicate}:{ids[target]}" if predicate else ids[target])

    migration: dict[str, Any] = {
        "tool": "sibyl migrate to-team",
        "origin_org": origin_org,
        "origin_entity_id": source.uuid,
        "origin_created_at": source.created_at,
        "origin_updated_at": source.updated_at,
        "origin_memory_scope": source.memory_scope,
    }
    if node.name != source.name:
        migration["origin_name"] = source.name
    if node.coerced:
        migration["coerced_edges"] = node.coerced
    metadata: dict[str, Any] = {"project_id": target_project_id, "migration": migration}
    if node.scope:
        metadata["memory_scope"] = node.scope
    for key in ("learnings", "assignees", "technologies", "branch_name", "feature", "complexity"):
        value = attributes.get(key)
        if value not in (None, "", []):
            metadata[key] = value
    for key in ("started_at", "completed_at", "due_date"):
        if attributes.get(key):
            migration[f"origin_{key}"] = attributes[key]
    container_status = source.status or attributes.get("status")
    if source.entity_type == "milestone" and container_status:
        metadata["status"] = container_status
    elif source.entity_type == "epic" and container_status:
        # An epic's status derives from its tasks on the target.
        migration["origin_status"] = container_status
    priority = source.priority or attributes.get("priority")
    if priority:
        metadata["priority"] = priority
    if node.epic and node.epic in ids:
        metadata["epic_id"] = ids[node.epic]
    if node.parent and node.parent in ids:
        metadata["parent_task_id"] = ids[node.parent]
        if ids[node.parent] not in related:
            related.append(ids[node.parent])
    depends = [ids[target] for target in node.depends_on if target in ids]
    if depends:
        metadata["depends_on"] = depends

    body: dict[str, Any] = {
        "name": node.name,
        "content": content or node.name,
        "entity_type": source.entity_type,
        "metadata": metadata,
        "skip_conflicts": True,
    }
    description = str(source.description or attributes.get("description") or "")
    if description and description not in content:
        if source.entity_type == "task":
            # A task stores its body as its description, so a separate
            # description would be overwritten: both texts go in the body.
            body["content"] = (
                description if content in description else f"{content}\n\n{description}"
            )
        else:
            # Other types derive their description from the body, so a
            # distinct one is kept with the provenance.
            migration["origin_description"] = description
    if attributes.get("category"):
        body["category"] = str(attributes["category"])
    languages = attributes.get("languages")
    if isinstance(languages, list) and languages:
        body["languages"] = [str(lang) for lang in languages]
    retrieval_keys = attributes.get("retrieval_keys")
    if isinstance(retrieval_keys, list) and retrieval_keys:
        body["retrieval_keys"] = [str(key) for key in retrieval_keys]
    tags = list(source.tags) or list(attributes.get("tags") or [])
    if tags:
        body["tags"] = [str(tag) for tag in tags]
    if related:
        body["related_to"] = related
    return body, missing


@dataclass
class GraphOutcome:
    created: int = 0
    resumed: int = 0
    relinked: int = 0
    adopted: int = 0
    statuses: int = 0
    failures: list[str] = field(default_factory=list)
    unlinked: list[str] = field(default_factory=list)


# Every metadata field a write from this pass sets. A row whose value for any
# of them changed on the team server was edited there; status is left out
# because linking does not write status.
_WRITTEN_METADATA = (
    "memory_scope",
    "project_id",
    "priority",
    "learnings",
    "epic_id",
    "parent_task_id",
    "depends_on",
    "assignees",
    "technologies",
    "branch_name",
    "feature",
    "complexity",
    "retrieval_keys",
)


def target_digest(row: Mapping[str, Any]) -> str:
    """What the migration compares to tell whether someone edited a row it wrote."""
    metadata = row.get("metadata") or {}
    snapshot = {
        "name": row.get("name"),
        "content": row.get("content"),
        "description": row.get("description"),
        "tags": sorted(str(tag) for tag in row.get("tags") or []),
        "retrieval_keys": row.get("retrieval_keys"),
        **{key: metadata.get(key) for key in _WRITTEN_METADATA},
    }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


async def _existing_container(
    client: Any, node: PlannedEntity, target_project_id: str
) -> str | None:
    """The team's own epic or milestone of this name in the target project."""
    response = await client._request(
        "POST",
        "/search/explore",
        json={
            "mode": "list",
            "types": [node.source.entity_type],
            "project": target_project_id,
            "limit": 200,
            "offset": 0,
            "depth": 1,
        },
        _buffer_pending=False,
    )
    for entity in response.get("entities") or []:
        if str(entity.get("name") or "") == node.name and entity.get("id"):
            return str(entity["id"])
    return None


def _task_status(node: PlannedEntity) -> str | None:
    if node.source.entity_type != "task":
        return None
    status = str(node.source.status or node.source.attributes.get("status") or "").lower()
    status = _STATUS_ALIASES.get(status, status)
    return status if status in _TASK_STATUSES and status != "todo" else None


async def execute_plan(
    client: Any,
    plan: GraphPlan,
    *,
    ids: dict[str, str],
    statuses: dict[str, str],
    partial: dict[str, dict[str, Any]],
    target_project_id: str,
    origin_org: str,
    save: Callable[[], None],
    concurrency: int = 8,
    log: Callable[[str], None] = lambda _message: None,
    operation_namespace: str = "",
    revisions: dict[str, int] | None = None,
    structure: dict[str, dict[str, Any]] | None = None,
    preexisting: set[str] | None = None,
) -> GraphOutcome:
    """Create the plan layer by layer.

    `ids`, `statuses`, and `partial` are the ledger: what landed, which task
    statuses were set, and which rows landed without some of their links
    (with a digest of how each looked on the target right after it landed).
    """
    outcome = GraphOutcome()
    gate = asyncio.Semaphore(concurrency)
    writes = 0
    # The revision each row was left at by this migration's own last write:
    # an undo removes a row only while it still sits there.
    last_written = revisions if revisions is not None else {}
    # What an undo needs without the source: each row's type, layer, and links.
    shape = structure if structure is not None else {}
    # Rows whose create landed on a row that already existed: the migration
    # updated them rather than creating them, so an undo never removes them.
    adopted = preexisting if preexisting is not None else set()
    intents_recorded = 0
    intents_saved = 0

    def remember(origin: str, revision: object) -> None:
        if origin not in adopted and type(revision) is int and revision >= 1:
            last_written[origin] = revision

    async def persist_intent() -> None:
        """Put the intent just recorded on disk before its request is sent.

        Rows that record intents in the same turn share one ledger write: the
        first to resume saves everything recorded so far and the rest find
        theirs already saved. One write per row would rewrite the whole ledger
        for every row a layer starts, blocking the event loop long enough for
        a pooled connection to go stale between assignment and send.
        """
        nonlocal intents_recorded, intents_saved
        intents_recorded += 1
        wanted = intents_recorded
        await asyncio.sleep(0)
        if intents_saved < wanted:
            covered = intents_recorded
            save()
            intents_saved = covered

    async def read(target_id: str) -> dict[str, Any] | None:
        """The row as the team server holds it; None when it is gone."""
        async with gate:
            try:
                return await client._request("GET", f"/entities/{target_id}", _buffer_pending=False)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    return None
                raise

    def operation_key(node: PlannedEntity, stage: str, links: object = None) -> str:
        identity = [
            operation_namespace,
            origin_org,
            target_project_id,
            node.source.uuid,
            stage,
            links,
        ]
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return f"migrate-graph-{digest}"

    def resolved_links(body: dict[str, Any]) -> dict[str, Any]:
        metadata = body["metadata"]
        return {
            "related_to": sorted(body.get("related_to") or []),
            "epic_id": metadata.get("epic_id"),
            "parent_task_id": metadata.get("parent_task_id"),
            "depends_on": sorted(metadata.get("depends_on") or []),
        }

    async def write(node: PlannedEntity, body: dict[str, Any]) -> tuple[str, int | None] | None:
        origin = node.source.uuid
        kind = node.source.entity_type
        async with gate:
            try:
                response = await client._request(
                    "POST",
                    "/entities",
                    json=body,
                    params={
                        "sync": "true",
                        "replay_interrupted": "false",
                        "protect_ownership": "true",
                    },
                    _buffer_pending=False,
                    _idempotency_key=operation_key(node, "create"),
                )
            except Exception as exc:
                if (
                    getattr(exc, "status_code", None) != 409
                    or kind not in {"epic", "milestone"}
                    or getattr(exc, "error_code", None) not in {None, "constraint_violation"}
                ):
                    outcome.failures.append(f"{kind} {origin}: {exc}")
                    return None
                response = None
        if response is None:
            # A teammate already holds this container in the project: link to it.
            existing = await _existing_container(client, node, target_project_id)
            if existing is None:
                outcome.failures.append(
                    f"{kind} {origin}: a {kind} named '{node.name}' exists in the project "
                    "but could not be found to link to"
                )
                return None
            outcome.adopted += 1
            return existing, None
        target_id = str(response.get("id") or "")
        if not target_id:
            outcome.failures.append(f"{kind} {origin}: no id returned")
            return None
        revision = response.get("revision")
        if type(revision) is not int or revision < 1:
            raise RuntimeError(
                "the create receipt omitted its exact entity revision; upgrade the team "
                "server and reconcile the saved create receipt before retrying"
            )
        return target_id, revision

    async def set_status(node: PlannedEntity, target_id: str, status: str) -> bool:
        origin = node.source.uuid
        statuses.pop(origin, None)
        pending = partial.setdefault(origin, {"missing": [], "digest": None})
        try:
            intent: dict[str, Any] | None = pending.get("status_intent")
            if intent is None:
                revision = pending.get("created_revision")
                if type(revision) is not int or revision < 1:
                    pending["status_revision_required"] = True
                    save()
                    raise RuntimeError(
                        "no trustworthy saved create revision is available; reconcile "
                        "the target status before retrying"
                    )
                intent = {
                    "target_id": target_id,
                    "body": {"status": status, "expected_revision": revision},
                    "key": operation_key(
                        node, "status", {"target_id": target_id, "status": status}
                    ),
                }
                pending["status_intent"] = intent
                await persist_intent()
            if intent["target_id"] != target_id or intent["body"]["status"] != status:
                raise RuntimeError(
                    "the source status or target changed after its intent was saved; "
                    "restore the original input or reconcile the saved status receipt"
                )
            async with gate:
                response = await client._request(
                    "PATCH",
                    f"/tasks/{target_id}",
                    json=intent["body"],
                    params={"sync": "true", "replay_interrupted": "false"},
                    _buffer_pending=False,
                    _idempotency_key=intent["key"],
                )
                receipt = response.get("mutation_receipt") or {}
                if receipt.get("applied") is False:
                    raise RuntimeError("the server queued the status instead of applying it")
                remember(origin, receipt.get("revision"))
        except Exception as exc:
            outcome.failures.append(f"task {origin}: status {status} not set ({exc})")
            return False
        statuses[origin] = status
        pending.pop("status_intent")
        pending.pop("created_revision", None)
        outcome.statuses += 1
        return True

    total = len(plan.entities)
    processed = 0
    started = time.monotonic()

    async def create(node: PlannedEntity) -> None:
        nonlocal processed
        # One row's trouble never stops the run: it is reported, and the
        # ledger keeps what is needed to finish it next time.
        try:
            await create_one(node)
        except Exception as exc:
            outcome.failures.append(f"{node.source.entity_type} {node.source.uuid}: {exc}")
        processed += 1
        if processed % _PROGRESS_EVERY == 0 and processed < total:
            rate = processed / max(time.monotonic() - started, 0.001)
            log(f"  {processed} of {total} rows ({rate:.0f} per second)")

    async def create_one(node: PlannedEntity) -> None:
        nonlocal writes
        origin = node.source.uuid
        relink_after_create = False
        pending = partial.get(origin)
        if origin in ids and pending and pending.get("restore_status"):
            raise RuntimeError(
                "the legacy status restoration has no trustworthy saved revision; "
                "reconcile the target status before retrying"
            )
        if origin in ids and pending and pending.get("status_revision_required"):
            raise RuntimeError(
                "no trustworthy saved create revision is available; reconcile "
                "the target status before retrying"
            )
        if (
            origin in ids
            and pending
            and (
                pending.get("status_intent")
                or (
                    "created_revision" in pending
                    and _task_status(node)
                    and statuses.get(origin) != _task_status(node)
                )
            )
        ):
            status = _task_status(node)
            if not status:
                raise RuntimeError(
                    "the source status changed after its intent was saved; restore "
                    "the original input or reconcile the saved status receipt"
                )
            if not await set_status(node, ids[origin], status):
                return
            if not pending.get("missing"):
                partial.pop(origin)
                outcome.resumed += 1
                return
        if origin in ids and not pending:
            outcome.resumed += 1
        elif origin in ids and pending:
            if not pending.get("missing") and "link_body" not in pending:
                partial.pop(origin)
                outcome.resumed += 1
            else:
                await relink(node, pending)
            return
        else:
            source_digest = hashlib.sha256(
                json.dumps(
                    {"source": asdict(node.source), "name": node.name, "scope": node.scope},
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
            if pending and "create_body" in pending:
                if pending.get("source_digest") != source_digest:
                    raise RuntimeError(
                        "the source changed after its create intent was saved; restore the "
                        "original source or reconcile the saved target receipt before retrying"
                    )
                body = pending["create_body"]
                missing = pending["missing"]
            else:
                body, missing = _payload(
                    node, ids=ids, target_project_id=target_project_id, origin_org=origin_org
                )
                # Retry the same body even when a dependency lands after a lost
                # acknowledgement; new links belong to the later relink operation.
                partial[origin] = {
                    "create_body": body,
                    "source_digest": source_digest,
                    "missing": missing,
                    "digest": None,
                }
                await persist_intent()
            created = await write(node, body)
            if created is None:
                return
            target_id, revision = created
            shape[origin] = {
                "type": node.source.entity_type,
                "layer": node.layer,
                "links": _link_targets(node),
            }
            # A fresh row starts at revision 1; a higher one means the id
            # already held a row (the author's own, or one with no author).
            if type(revision) is int and revision > 1:
                adopted.add(origin)
            remember(origin, revision)
            ids[origin] = target_id
            outcome.created += 1
            partial[origin] = {
                "missing": missing,
                "digest": None,
                "created_revision": revision,
            }
            if missing:
                # Recorded before the read, so a failed read cannot leave the
                # row looking finished.
                landed = await read(target_id)
                partial[origin]["digest"] = target_digest(landed) if landed else None
                relink_after_create = any(target in ids for target in missing)
                outcome.unlinked.append(
                    f"{origin}: landed without {len(missing)} link(s) to rows that failed; "
                    "a re-run adds them once those rows land"
                )
            writes += 1
            if writes % _SAVE_EVERY == 0:
                save()
        status = _task_status(node)
        if (
            status
            and statuses.get(origin) != status
            and not await set_status(node, ids[origin], status)
        ):
            return
        if relink_after_create:
            await relink(node, partial[origin])
        elif origin in partial and not partial[origin].get("missing"):
            partial.pop(origin)

    async def relink(node: PlannedEntity, pending: dict[str, Any]) -> None:
        origin = node.source.uuid
        target_id = ids[origin]
        if "link_body" not in pending:
            body, missing = _payload(
                node, ids=ids, target_project_id=target_project_id, origin_org=origin_org
            )
            if set(missing) >= set(pending.get("missing") or []):
                outcome.resumed += 1
                outcome.unlinked.append(
                    f"{origin}: still missing {len(missing)} link(s) to rows that failed"
                )
                return
            if not pending.get("digest"):
                partial.pop(origin, None)
                outcome.resumed += 1
                outcome.unlinked.append(
                    f"{origin}: could not confirm it was unchanged on the team server, so its "
                    "missing links were not added"
                )
                return
            current = await read(target_id)
            # The digest leaves out fields a teammate may change, such as task
            # status; any write since the migration's own last one moves the
            # revision, so a row the migration recorded must still sit there.
            own = last_written.get(origin)
            if (
                current is None
                or target_digest(current) != pending.get("digest")
                or (own is not None and current.get("revision") != own)
            ):
                partial.pop(origin, None)
                outcome.resumed += 1
                outcome.unlinked.append(
                    f"{origin}: changed on the team server since it was migrated, so its "
                    "missing links were not added"
                )
                return
            revision = current.get("revision")
            if type(revision) is not int or revision < 1:
                raise RuntimeError(
                    "the target did not return an entity revision; upgrade the team server "
                    "before retrying missing links"
                )
            topology = resolved_links(body)
            request = {"expected_revision": revision, **topology}
            pending["link_body"] = request
            pending["link_missing"] = missing
            pending["link_key"] = operation_key(node, "links", topology)
            # A lost response retries this exact request. The server either finds
            # every binding already applied or enforces the saved revision.
            await persist_intent()
        async with gate:
            try:
                response = await client._request(
                    "POST",
                    f"/entities/{target_id}/links",
                    json=pending["link_body"],
                    _buffer_pending=False,
                    _idempotency_key=pending["link_key"],
                )
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    raise RuntimeError(
                        "the target entity or its additive link endpoint is unavailable; "
                        "check the entity and upgrade the team server before retrying"
                    ) from exc
                raise
        revision = response.get("revision")
        if response.get("entity_id") != target_id or type(revision) is not int or revision < 1:
            raise RuntimeError(
                "the target returned an invalid link receipt; retry the saved intent"
            )
        missing = pending["link_missing"]
        outcome.relinked += 1
        if origin not in shape:
            # Created by an older run that kept no record of it: an undo keeps
            # it, and now knows what it links to so it keeps those rows too.
            adopted.add(origin)
            shape[origin] = {
                "type": node.source.entity_type,
                "layer": node.layer,
                "links": _link_targets(node),
            }
        remember(origin, revision)
        if missing:
            partial[origin] = {"missing": missing, "digest": None}
            landed = await read(target_id)
            partial[origin]["digest"] = target_digest(landed) if landed else None
        else:
            partial.pop(origin, None)

    for index, layer in enumerate(plan.layers):
        await asyncio.gather(*(create(node) for node in layer))
        save()
        log(f"  layer {index + 1}/{len(plan.layers)}: {len(ids)} entities on the target")
    return outcome


@dataclass
class UndoOutcome:
    removed: int = 0
    gone: int = 0
    kept_edited: list[str] = field(default_factory=list)
    kept_linked: list[str] = field(default_factory=list)
    kept_shared: list[str] = field(default_factory=list)
    kept_unrecorded: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


async def undo_plan(
    client: Any,
    *,
    structure: dict[str, dict[str, Any]],
    ids: dict[str, str],
    revisions: dict[str, int],
    statuses: dict[str, str],
    partial: dict[str, dict[str, Any]],
    save: Callable[[], None],
    dry_run: bool = False,
    concurrency: int = 8,
    log: Callable[[str], None] = lambda _message: None,
) -> UndoOutcome:
    """Remove what a migration created, as long as nobody has touched it since.

    Works from the ledger alone: `structure` records, for each row the
    migration created, its type, plan layer, and the rows it links to, so an
    undo follows what was migrated even after the source changed or is gone.
    A row goes only while it still carries this migration's provenance for its
    origin, sits at the revision the migration's own last write left it at,
    and nothing outside the migration depends on it; the server re-checks the
    last two as it deletes. Layers are taken newest first, and a row that
    something kept still links to is kept too, so an undo never leaves a kept
    row pointing at nothing. A row links only to rows in earlier layers, so the
    rows of one layer are undone concurrently.
    """
    outcome = UndoOutcome()
    still_linked: set[str] = set()
    gate = asyncio.Semaphore(concurrency)

    def label(origin: str) -> str:
        return f"{(structure.get(origin) or {}).get('type') or 'row'} {origin}"

    def keep(origin: str, bucket: list[str], reason: str) -> None:
        bucket.append(f"{label(origin)}: {reason}")
        still_linked.update((structure.get(origin) or {}).get("links") or [])

    async def undo_one(origin: str) -> None:
        target_id = ids.get(origin)
        if target_id is None:
            return
        if origin in still_linked:
            keep(origin, outcome.kept_linked, "a row that was kept still links to it")
            return
        revision = revisions.get(origin)
        if revision is None or origin not in structure:
            keep(
                origin,
                outcome.kept_unrecorded,
                "the migration did not create it, or an older run left no record of it",
            )
            return
        try:
            async with gate:
                current = await client._request(
                    "GET", f"/entities/{target_id}", _buffer_pending=False
                )
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                outcome.gone += 1
                if not dry_run:
                    _forget(origin, ids, revisions, statuses, partial)
                return
            keep(origin, outcome.failures, f"could not read it ({exc})")
            return
        provenance = ((current.get("metadata") or {}).get("migration") or {}).get(
            "origin_entity_id"
        )
        if provenance != origin:
            keep(origin, outcome.kept_unrecorded, "the row on the target is not this migration's")
            return
        if current.get("revision") != revision:
            keep(origin, outcome.kept_edited, "changed on the team server since it was migrated")
            return
        if dry_run:
            # The same checks the delete runs, so the dry run keeps what the undo would.
            async with gate:
                check = await client._request(
                    "GET",
                    f"/entities/{target_id}/deletable",
                    params={"expected_revision": str(revision)},
                    _buffer_pending=False,
                )
            if check.get("deletable") is True:
                outcome.removed += 1
            elif check.get("error") == "entity_shared":
                keep(origin, outcome.kept_shared, str(check.get("reason") or "shared"))
            else:
                keep(origin, outcome.kept_edited, "changed on the team server since it was migrated")
            return
        try:
            async with gate:
                await client._request(
                    "DELETE",
                    f"/entities/{target_id}",
                    params={"expected_revision": str(revision), "if_unshared": "true"},
                    _buffer_pending=False,
                )
        except Exception as exc:
            if getattr(exc, "error_code", None) == "entity_shared":
                keep(origin, outcome.kept_shared, str(exc))
            elif getattr(exc, "status_code", None) == 409:
                keep(origin, outcome.kept_edited, "changed on the team server during the undo")
            elif getattr(exc, "status_code", None) == 404:
                outcome.gone += 1
                _forget(origin, ids, revisions, statuses, partial)
            else:
                keep(origin, outcome.failures, f"delete failed ({exc})")
            return
        outcome.removed += 1
        _forget(origin, ids, revisions, statuses, partial)
        if outcome.removed % _SAVE_EVERY == 0:
            save()
            log(f"  {outcome.removed} removed...")

    layers: dict[int, list[str]] = {}
    for origin in ids:
        layers.setdefault(int((structure.get(origin) or {}).get("layer") or 0), []).append(origin)
    for layer in sorted(layers, reverse=True):
        await asyncio.gather(*(undo_one(origin) for origin in layers[layer]))
    if not dry_run:
        save()
    return outcome


def _forget(
    origin: str,
    ids: dict[str, str],
    revisions: dict[str, int],
    statuses: dict[str, str],
    partial: dict[str, dict[str, Any]],
) -> None:
    ids.pop(origin, None)
    revisions.pop(origin, None)
    statuses.pop(origin, None)
    partial.pop(origin, None)
