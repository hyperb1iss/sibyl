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

The API only lets an entity declare links at creation, with itself as the
subject and every target already present. So the plan orders creation:
epics and milestones first, then tasks, then everything else, with typed
links and task dependencies as hard ordering constraints. An untyped link
carries no direction worth keeping, so it is declared from whichever end is
created later.

Entity ids are deterministic, and the server upserts a caller's own row at
its id, so writing a row again is safe: a ledger maps source ids to target
ids to skip finished work, and a row written while one of its link targets
had failed is written again once that target exists, which adds the link.
"""

from __future__ import annotations

import asyncio
import heapq
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

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

    def limited(self, count: int | None) -> GraphPlan:
        """The first `count` entities; a prefix of the order keeps every link target."""
        if count is None:
            return self
        return GraphPlan(
            entities=self.entities[: max(count, 0)],
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
    if entity.attributes.get("excluded_from_recall") or (
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


def _id_key(entity: SourceEntity, name: str) -> tuple[str, str, str]:
    """What the server hashes into an entity id: type, title, and category."""
    category = entity.category or "general"
    return (entity.entity_type, name[:_ID_PART_CHARS], category[:_ID_PART_CHARS])


def _unique_names(entities: Iterable[SourceEntity]) -> dict[str, str]:
    """Titles that mint distinct ids on the target.

    Two rows that would hash to the same id would land on one entity, the
    second replacing the first, so a repeat gets a counter placed where the
    hash still sees it.
    """
    taken: set[tuple[str, str, str]] = set()
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
    description = source.description or attributes.get("description")
    if description and description != content:
        body["description"] = str(description)
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
    statuses: int = 0
    failures: list[str] = field(default_factory=list)
    unlinked: list[str] = field(default_factory=list)


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
    partial: dict[str, list[str]],
    target_project_id: str,
    origin_org: str,
    save: Callable[[], None],
    concurrency: int = 8,
    log: Callable[[str], None] = lambda _message: None,
) -> GraphOutcome:
    """Create the plan layer by layer.

    `ids`, `statuses`, and `partial` are the ledger: what landed, which task
    statuses were set, and which rows landed without some of their links.
    """
    outcome = GraphOutcome()
    gate = asyncio.Semaphore(concurrency)
    writes = 0

    async def create(node: PlannedEntity) -> None:
        nonlocal writes
        origin = node.source.uuid
        relink = origin in ids and bool(partial.get(origin))
        if origin in ids and not relink:
            outcome.resumed += 1
        else:
            body, missing = _payload(
                node, ids=ids, target_project_id=target_project_id, origin_org=origin_org
            )
            if relink and set(missing) >= set(partial[origin]):
                # None of the links it was missing can be added yet.
                outcome.resumed += 1
                outcome.unlinked.append(
                    f"{origin}: still missing {len(missing)} link(s) to rows that failed"
                )
            else:
                async with gate:
                    try:
                        response = await client._request(
                            "POST",
                            "/entities",
                            json=body,
                            params={"sync": "true"},
                            _buffer_pending=False,
                        )
                    except Exception as exc:
                        outcome.failures.append(f"{node.source.entity_type} {origin}: {exc}")
                        return
                target_id = str(response.get("id") or "")
                if not target_id:
                    outcome.failures.append(f"{node.source.entity_type} {origin}: no id returned")
                    return
                ids[origin] = target_id
                if missing:
                    partial[origin] = missing
                    outcome.unlinked.append(
                        f"{origin}: landed without {len(missing)} link(s) to rows that failed; "
                        "a re-run adds them once those rows land"
                    )
                else:
                    partial.pop(origin, None)
                if relink:
                    outcome.relinked += 1
                else:
                    outcome.created += 1
                writes += 1
                if writes % _SAVE_EVERY == 0:
                    save()
        status = _task_status(node)
        if status and statuses.get(origin) != status:
            async with gate:
                try:
                    await client._request(
                        "PATCH",
                        f"/tasks/{ids[origin]}",
                        json={"status": status},
                        _buffer_pending=False,
                    )
                except Exception as exc:
                    outcome.failures.append(f"task {origin}: status {status} not set ({exc})")
                    return
            statuses[origin] = status
            outcome.statuses += 1

    for index, layer in enumerate(plan.layers):
        await asyncio.gather(*(create(node) for node in layer))
        save()
        log(f"  layer {index + 1}/{len(plan.layers)}: {len(ids)} entities on the target")
    return outcome
