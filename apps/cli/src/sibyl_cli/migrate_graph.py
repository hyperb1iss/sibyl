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


def _shape(node: PlannedEntity) -> dict[str, Any]:
    """What an undo needs to know about a row without reading the source."""
    return {"type": node.source.entity_type, "layer": node.layer, "links": _link_targets(node)}


def _merged_shape(recorded: Mapping[str, Any] | None, current: Mapping[str, Any]) -> dict[str, Any]:
    """A shape covering both: every link either names, at the later of their layers.

    A row can carry links from its first body and from a later relink, so an
    undo that keeps it must protect all of them.
    """
    if not recorded:
        return dict(current)
    links = sorted({*(recorded.get("links") or []), *(current.get("links") or [])})
    layers = [
        layer for layer in (recorded.get("layer"), current.get("layer")) if type(layer) is int
    ]
    return {
        "type": current.get("type") or recorded.get("type"),
        "layer": max(layers) if layers else current.get("layer"),
        "links": links,
    }


def _key_used_by_another_body(exc: BaseException) -> bool:
    """The server already holds this idempotency key for a different request body."""
    return getattr(exc, "status_code", None) == 409 and "already used for a different request" in (
        str(exc)
    )


def _contested(exc: BaseException) -> bool:
    """A 409 the row itself can settle: a refused revision, or a key the server holds.

    The API reports a refused revision as a generic conflict, so the row is
    read to tell a teammate's edit from a write that landed. Lock contention is
    and a request still in flight are transient and stay failures.
    """
    return getattr(exc, "status_code", None) == 409 and getattr(exc, "error_code", None) not in {
        "entity_locked",
        "idempotency_in_progress",
    }


def _applied_without_receipt(exc: BaseException) -> bool:
    """The server applied the write but could not store its receipt."""
    return getattr(exc, "status_code", None) == 503 and "receipt is still pending" in str(exc)


# Who can see a row, narrowest first.
_VISIBILITY = ("private", "delegated", "project", "team", "organization", "shared", "public")


def _wider(landed: str | None, current: str | None) -> bool:
    """Whether a landed scope is more visible than the row's scope now."""
    if landed not in _VISIBILITY or current not in _VISIBILITY:
        return False
    return _VISIBILITY.index(landed) > _VISIBILITY.index(current)


def _body_link_origins(
    body: Mapping[str, Any], missing: Iterable[str] | None, ids: Mapping[str, str]
) -> list[str]:
    """The source rows a sent body links to, read back from its resolved targets."""
    by_target = {target: origin for origin, target in ids.items()}
    metadata = body.get("metadata") if isinstance(body.get("metadata"), Mapping) else {}
    predicates = {f"{predicate}:" for predicate in DECLARABLE.values()}
    related = [
        next((entry[len(p) :] for p in predicates if entry.startswith(p)), entry)
        for entry in body.get("related_to") or []
        if isinstance(entry, str)
    ]
    targets = [
        *related,
        metadata.get("epic_id"),
        metadata.get("parent_task_id"),
        *(metadata.get("depends_on") or []),
    ]
    found = {
        by_target[target] for target in targets if isinstance(target, str) and target in by_target
    }
    return sorted(found | set(missing or []))


def _read_back_shape(
    sent: Mapping[str, Any],
    ids: Mapping[str, str],
    structure: Mapping[str, Mapping[str, Any]],
    *,
    layer: int = 0,
) -> dict[str, Any]:
    """A shape for a body saved before shapes were kept.

    Its links come from the body itself, and its layer sits above every row it
    links to, so an undo removes it before them.
    """
    links = _body_link_origins(sent.get("create_body") or {}, sent.get("missing"), ids)
    above = [int((structure.get(link) or {}).get("layer") or 0) + 1 for link in links]
    return {"layer": max([layer, *above]), "links": links}


async def _replay_saved_create(
    client: Any, intent: Mapping[str, Any]
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Send a create intent's saved bodies under its key, newest first, until one is taken.

    The server answers a body that is not the one holding the key with "used
    for a different request", so the next earlier body goes; an earlier body
    only ever recovers a row the server already holds.
    """
    candidates = [intent, *(intent.get("earlier") or [])]
    for index, candidate in enumerate(candidates):
        try:
            receipt = await client._request(
                "POST",
                "/entities",
                json=candidate["create_body"],
                params={
                    "sync": "true",
                    "replay_interrupted": "false",
                    "protect_ownership": "true",
                },
                _buffer_pending=False,
                _idempotency_key=intent["create_key"],
            )
        except Exception as exc:
            if index < len(candidates) - 1 and _key_used_by_another_body(exc):
                continue
            raise
        return receipt, candidate
    raise RuntimeError("the create intent holds no body")


def _sent_body(intent: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of a create intent that describe one body sent under its key."""
    return {
        key: intent[key]
        for key in ("create_body", "shape", "missing", "source_digest")
        if key in intent
    }


def _body_scope(body: Mapping[str, Any]) -> str | None:
    metadata = body.get("metadata")
    scope = metadata.get("memory_scope") if isinstance(metadata, Mapping) else None
    return scope if isinstance(scope, str) else None


def _payload(
    node: PlannedEntity,
    *,
    ids: Mapping[str, str],
    target_project_id: str,
    origin_org: str,
    origin_project: str = "",
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
        # With the org, names the migration: the server counts rows of the same
        # source project as one migration, and no others.
        **({"origin_project": origin_project} if origin_project else {}),
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
    # Rows that changed locally after an earlier body of theirs reached the team
    # server; the team copy holds that earlier body.
    landed_earlier: list[str] = field(default_factory=list)
    # The subset whose earlier body was more visible than the local row is now.
    landed_wider: list[str] = field(default_factory=list)
    # Tasks whose later local status was not written because the task changed on
    # the team server since the migration's own last write.
    team_statuses: list[str] = field(default_factory=list)
    # Tasks whose revisions the migration cannot claim (it linked to the row, or
    # could not confirm its first write); a later local status stays local.
    adopted_statuses: list[str] = field(default_factory=list)


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


def _task_status(node: PlannedEntity, *, include_todo: bool = False) -> str | None:
    """The task status to write. A new row already starts at todo, so todo is
    written only once the migration has sent the task another status."""
    if node.source.entity_type != "task":
        return None
    status = str(node.source.status or node.source.attributes.get("status") or "").lower()
    status = _STATUS_ALIASES.get(status, status)
    if status not in _TASK_STATUSES or (status == "todo" and not include_todo):
        return None
    return status


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
    undoing: set[str] | None = None,
    origin_project: str = "",
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

    def note_epic_start(response: dict[str, Any]) -> None:
        """Record an epic revision this migration's own status write moved.

        A task moving forward auto-starts its epic on the server. When the
        epic sat at the revision this migration last left it at, the start is
        the migration's own write; otherwise someone else changed it and it
        stays theirs.
        """
        started = (response.get("data") or {}).get("epic_started") or {}
        revision = started.get("revision")
        epic_origin = next((o for o, t in ids.items() if t == started.get("epic_id")), None)
        if (
            epic_origin is not None
            and type(revision) is int
            and last_written.get(epic_origin) == revision - 1
        ):
            remember(epic_origin, revision)

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

    async def write(
        node: PlannedEntity, body: dict[str, Any], key: str, *, raise_key_reuse: bool = False
    ) -> tuple[str, int | None] | None:
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
                    _idempotency_key=key,
                )
            except Exception as exc:
                if raise_key_reuse and _key_used_by_another_body(exc):
                    raise
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

    async def land(
        node: PlannedEntity, intent: dict[str, Any], key: str
    ) -> tuple[tuple[str, int | None], dict[str, Any]] | None:
        """Send an intent's bodies under its key, newest first, until one is accepted.

        The server answers a body that is not the one holding the key with "used
        for a different request", so the next earlier body goes. An earlier body
        therefore only ever recovers a row the server already holds.
        """
        origin = node.source.uuid
        candidates = [intent, *(intent.get("earlier") or [])]
        for index, candidate in enumerate(candidates):
            try:
                created = await write(
                    node,
                    candidate["create_body"],
                    key,
                    raise_key_reuse=index < len(candidates) - 1,
                )
            except Exception as exc:
                if not _key_used_by_another_body(exc):
                    raise
                continue
            if created is None:
                return None
            if index:
                outcome.landed_earlier.append(origin)
                if _wider(_body_scope(candidate["create_body"]), node.scope):
                    outcome.landed_wider.append(origin)
            return created, candidate
        return None

    def _status_guard_revision(origin: str, pending: dict[str, Any]) -> int | None:
        """The revision a status write may expect: the create's, else the migration's last.

        A row the migration wrote and nobody touched since still sits at the
        revision its own last write left, so a later source status change can
        land there; any other write moves the revision and the server refuses.
        """
        for revision in (pending.get("created_revision"), last_written.get(origin)):
            if type(revision) is int and revision >= 1:
                return revision
        return None

    async def _read_quietly(target_id: str) -> dict[str, Any] | None:
        try:
            return await read(target_id)
        except Exception:
            return None

    async def send_status(origin: str, intent: dict[str, Any]) -> tuple[str, str, int | None]:
        """Send a saved status intent under its key: the outcome, the task's status, and,
        for the migration's own write, the revision it left.

        "set" means the write is the migration's own. When the server answers
        that it applied the write but could not store the receipt, the intent
        is marked applied, and the task still at exactly the next revision with
        this status is claimed. Any other contested answer is settled by reading
        the task: the server never releases a refused key, so that request did
        not write. A task holding this status got it some other way ("there");
        one holding another was changed on the team server ("team"). Neither is
        claimed, so an undo keeps the task.
        """
        target_id = intent["target_id"]
        try:
            async with gate:
                response = await client._request(
                    "PATCH",
                    f"/tasks/{target_id}",
                    json=intent["body"],
                    params={"sync": "true", "replay_interrupted": "false"},
                    _buffer_pending=False,
                    _idempotency_key=intent["key"],
                )
        except Exception as exc:
            if _applied_without_receipt(exc) and not intent.get("applied"):
                intent["applied"] = True
                await persist_intent()
            applied = bool(intent.get("applied"))
            row = await _read_quietly(target_id) if applied or _contested(exc) else None
            expected = intent["body"]["expected_revision"]
            row_revision = (row or {}).get("revision")
            if not (
                type(row_revision) is int and type(expected) is int and row_revision > expected
            ):
                raise
            row_status = str(((row or {}).get("metadata") or {}).get("status") or "").lower()
            if row_status != intent["body"]["status"]:
                return "team", row_status, None
            if applied and row_revision == expected + 1:
                remember(origin, row_revision)
                return "set", row_status, row_revision
            return "there", row_status, None
        receipt = response.get("mutation_receipt") or {}
        if receipt.get("applied") is False:
            raise RuntimeError("the server queued the status instead of applying it")
        revision = receipt.get("revision")
        remember(origin, revision)
        note_epic_start(response)
        return "set", intent["body"]["status"], revision if type(revision) is int else None

    async def set_status(node: PlannedEntity, target_id: str, status: str) -> bool:
        origin = node.source.uuid
        follow_revision: int | None = None
        statuses.pop(origin, None)
        pending = partial.setdefault(origin, {"missing": [], "digest": None})
        try:
            intent: dict[str, Any] | None = pending.get("status_intent")
            if intent is not None and intent["target_id"] != target_id:
                raise RuntimeError(
                    "the target changed after its status intent was saved; reconcile the "
                    "saved status receipt before retrying"
                )
            if intent is not None and intent["body"]["status"] != status:
                # The local status moved while an earlier one was unconfirmed. The
                # earlier request is settled under its own key first, so a delayed
                # copy of it can never land after the current one; the current
                # status then follows from the revision that left.
                settled, held, settled_revision = await send_status(origin, intent)
                pending.pop("status_intent")
                pending.pop("created_revision", None)
                if settled == "set":
                    outcome.statuses += 1
                    # The migration's own write just left the task at this revision,
                    # so the current status follows from it, even on a row the
                    # migration otherwise does not keep in step.
                    follow_revision = settled_revision
                else:
                    # Not the migration's write, so no revision to build on: the
                    # task stays as the team server has it.
                    if held != status:
                        outcome.team_statuses.append(origin)
                    statuses[origin] = status
                    return True
                intent = None
            if intent is None:
                revision = follow_revision or _status_guard_revision(origin, pending)
                if revision is None and origin in adopted:
                    # The migration cannot claim this row's revisions (it linked to
                    # the row, or could not confirm its first write), so it does not
                    # keep the row's status in step. The task is listed only when the
                    # team copy holds a different status.
                    row = await _read_quietly(target_id)
                    held = str(((row or {}).get("metadata") or {}).get("status") or "").lower()
                    if held != status:
                        outcome.adopted_statuses.append(origin)
                    statuses[origin] = status
                    pending.pop("status_revision_required", None)
                    return True
                if revision is None:
                    pending["status_revision_required"] = True
                    save()
                    raise RuntimeError(
                        "no trustworthy saved create revision is available; reconcile "
                        "the target status before retrying"
                    )
                # The revision is part of the key: a task can come back to a status
                # it was sent before, and that later write is a different request.
                intent = {
                    "target_id": target_id,
                    # mirrors_source_status: this write copies the source's
                    # status, so the server records no completion for it. An
                    # intent saved before the flag existed is resent as saved.
                    "body": {
                        "status": status,
                        "expected_revision": revision,
                        "mirrors_source_status": True,
                    },
                    "key": operation_key(
                        node,
                        "status",
                        {"target_id": target_id, "status": status, "expected_revision": revision},
                    ),
                }
                pending["status_intent"] = intent
                await persist_intent()
            settled, _held, _revision = await send_status(origin, intent)
        except Exception as exc:
            outcome.failures.append(f"task {origin}: status {status} not set ({exc})")
            return False
        # A status changed on the team server stands, and the status counts as
        # handled either way, so a re-run does not resend a refused request (the
        # server keeps its key).
        if settled == "team":
            outcome.team_statuses.append(origin)
        elif settled == "set":
            outcome.statuses += 1
        statuses[origin] = status
        pending.pop("status_intent", None)
        pending.pop("created_revision", None)
        return True

    def finished(origin: str) -> bool:
        return origin in ids and origin not in partial

    total = len(plan.entities)
    processed = 0
    started = time.monotonic()

    async def create(node: PlannedEntity) -> None:
        nonlocal processed
        # Rows an earlier run finished stay out of the count, so the rate is
        # the rate of rows this run writes.
        counted = not finished(node.source.uuid)
        # One row's trouble never stops the run: it is reported, and the
        # ledger keeps what is needed to finish it next time.
        try:
            await create_one(node)
        except Exception as exc:
            outcome.failures.append(f"{node.source.entity_type} {node.source.uuid}: {exc}")
        if not counted:
            return
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
            if _status_guard_revision(origin, pending) is None and origin not in adopted:
                raise RuntimeError(
                    "no trustworthy saved create revision is available; reconcile "
                    "the target status before retrying"
                )
            # The status this flag held back is retried below, against that revision.
            pending.pop("status_revision_required")
            if not pending.get("missing") and set(pending) <= {"missing", "digest"}:
                partial.pop(origin)
                pending = None
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
            status = _task_status(node, include_todo=True)
            if not status:
                raise RuntimeError(
                    "the task's local status is not one the team server accepts; set a "
                    "valid status locally before retrying"
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
                # Older intents did not keep their key; theirs is the plain one.
                key = pending.get("create_key") or operation_key(node, "create")
                if pending.get("source_digest") != source_digest:
                    # The row changed locally while its create was unconfirmed. Its
                    # current body goes first under the same key; every body sent
                    # before stays on file, since one of them may hold the key.
                    body, missing = _payload(
                        node,
                        ids=ids,
                        target_project_id=target_project_id,
                        origin_org=origin_org,
                        origin_project=origin_project,
                    )
                    earlier = [_sent_body(pending), *(pending.get("earlier") or [])]
                    partial[origin] = {
                        "create_body": body,
                        "create_key": key,
                        "shape": _shape(node),
                        "source_digest": source_digest,
                        "missing": missing,
                        "digest": None,
                        "earlier": earlier,
                    }
                    await persist_intent()
            else:
                body, missing = _payload(
                    node,
                    ids=ids,
                    target_project_id=target_project_id,
                    origin_org=origin_org,
                    origin_project=origin_project,
                )
                # Retry the same body under the same key even when a dependency
                # lands, or an undo moves the route's keys, after a lost
                # acknowledgement; new links belong to the later relink operation.
                key = operation_key(node, "create")
                partial[origin] = {
                    "create_body": body,
                    "create_key": key,
                    "shape": _shape(node),
                    "source_digest": source_digest,
                    "missing": missing,
                    "digest": None,
                }
                await persist_intent()
            landed = await land(node, partial[origin], key)
            if landed is None:
                return
            created, sent = landed
            missing = sent["missing"]
            target_id, revision = created
            # The links the body that landed carries; a later relink adds its own.
            # A body saved before shapes were kept is read back from its targets.
            shape[origin] = dict(sent.get("shape") or {}) or _merged_shape(
                _read_back_shape(sent, ids, shape, layer=node.layer), _shape(node)
            )
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
        # Once the migration has sent a task a status, a move back to todo is a
        # status change like any other.
        status = _task_status(node, include_todo=origin in statuses)
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
                node,
                ids=ids,
                target_project_id=target_project_id,
                origin_org=origin_org,
                origin_project=origin_project,
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
            shape[origin] = _shape(node)
        else:
            # The relink added the current links to whatever the row landed with.
            shape[origin] = _merged_shape(shape[origin], _shape(node))
        remember(origin, revision)
        if missing:
            partial[origin] = {"missing": missing, "digest": None}
            landed = await read(target_id)
            partial[origin]["digest"] = target_digest(landed) if landed else None
        else:
            partial.pop(origin, None)

    # An undo marks rows before deleting them; one that stopped part way may
    # have removed rows the ledger still lists. Check those before trusting it.
    marked = undoing if undoing is not None else set()
    for origin in sorted(marked & set(ids)):
        if await read(ids[origin]) is None:
            _forget(origin, ids, last_written, statuses, partial)
            shape.pop(origin, None)
            adopted.discard(origin)
    if marked:
        marked.clear()
        save()

    total = sum(1 for node in plan.entities if not finished(node.source.uuid))
    if total < len(plan.entities):
        log(f"  {len(plan.entities) - total} rows already finished; {total} to write")
    started = time.monotonic()
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
    # Creates whose outcome was never confirmed; a real undo resolves them first.
    unresolved: int = 0
    # Unconfirmed creates from a run that kept no key; they cannot be resolved.
    unresolved_unkeyed: int = 0
    # The server refused the caller's deletes outright (project maintainer access).
    refused: bool = False


# A revision no row reaches. The access check runs before the revision check,
# so a caller without maintainer access still gets its 403, and anyone else is
# turned away at the revision instead of running the full sharing scan.
_UNREACHABLE_REVISION = "2147483647"


async def access_refused(client: Any, project_id: str) -> bool:
    """Whether the team server refuses this caller the deletes an undo needs."""
    try:
        await client._request(
            "GET",
            f"/entities/{project_id}/deletable",
            params={"expected_revision": _UNREACHABLE_REVISION},
            _buffer_pending=False,
        )
    except Exception as exc:
        return getattr(exc, "status_code", None) == 403
    return False


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
    undoing: set[str] | None = None,
    project_id: str | None = None,
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
    # Rows marked before their delete is sent and unmarked once their fate is
    # known, so a migration after an interrupted undo checks them first.
    marks = undoing if undoing is not None else set()
    uncertain: set[str] = set()

    def label(origin: str) -> str:
        return f"{(structure.get(origin) or {}).get('type') or 'row'} {origin}"

    def keep(origin: str, bucket: list[str], reason: str) -> None:
        bucket.append(f"{label(origin)}: {reason}")
        still_linked.update((structure.get(origin) or {}).get("links") or [])

    async def classify(origin: str) -> None:
        """Phase one, read only: is this row still the migration's, as it left it?"""
        target_id = ids[origin]
        revision = revisions.get(origin)
        if revision is None or origin not in structure:
            keep(
                origin,
                outcome.kept_unrecorded,
                "the migration did not create it, or an older run left no record of it",
            )
            kept_rows[origin] = None
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
            kept_rows[origin] = None
            return
        provenance = ((current.get("metadata") or {}).get("migration") or {}).get(
            "origin_entity_id"
        )
        if provenance != origin:
            keep(origin, outcome.kept_unrecorded, "the row on the target is not this migration's")
            kept_rows[origin] = current
        elif current.get("revision") != revision:
            keep(origin, outcome.kept_edited, "changed on the team server since it was migrated")
            kept_rows[origin] = current
        else:
            candidates.add(origin)

    async def protect_current_links(origin: str, current: dict[str, Any] | None) -> None:
        """A kept row keeps every migrated row it points at now.

        Links added on the team server after the migration (a teammate moving
        a migrated task under another migrated epic, or adding a dependency)
        are not in the plan, and the server does not count links between rows
        of the same migration as sharing, so the undo reads them itself.
        """
        target_ids = {t: o for o, t in ids.items()}
        metadata = (current or {}).get("metadata") or {}
        pointed = [metadata.get("epic_id"), metadata.get("parent_task_id")]
        pointed += list(metadata.get("depends_on") or [])
        async with gate:
            related = await client._request(
                "POST",
                "/search/explore",
                json={"mode": "related", "entity_id": ids[origin], "limit": 1000},
                _buffer_pending=False,
            )
        pointed += [
            row.get("id")
            for row in related.get("entities") or []
            if row.get("direction") == "outgoing"
        ]
        still_linked.update(target_ids[t] for t in pointed if t in target_ids)

    async def undo_one(origin: str) -> None:
        """Phase two: remove one row the first phase found unchanged."""
        target_id = ids.get(origin)
        if target_id is None or outcome.refused or origin not in candidates:
            return
        if origin in still_linked:
            keep(origin, outcome.kept_linked, "a row that was kept still links to it")
            return
        revision = revisions[origin]
        if dry_run:
            # The same checks the delete runs, so the dry run keeps what the undo would.
            try:
                async with gate:
                    check = await client._request(
                        "GET",
                        f"/entities/{target_id}/deletable",
                        params={"expected_revision": str(revision)},
                        _buffer_pending=False,
                    )
            except Exception as exc:
                if getattr(exc, "status_code", None) == 403:
                    outcome.refused = True
                else:
                    keep(origin, outcome.failures, f"could not check it ({exc})")
                return
            if check.get("deletable") is True:
                outcome.removed += 1
            elif check.get("error") == "entity_shared":
                keep(origin, outcome.kept_shared, str(check.get("reason") or "shared"))
            else:
                keep(
                    origin, outcome.kept_edited, "changed on the team server since it was migrated"
                )
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
            if getattr(exc, "status_code", None) == 403:
                outcome.refused = True
            elif getattr(exc, "error_code", None) == "entity_shared":
                keep(origin, outcome.kept_shared, str(exc))
            elif getattr(exc, "status_code", None) == 409:
                keep(origin, outcome.kept_edited, "changed on the team server during the undo")
            elif getattr(exc, "status_code", None) == 404:
                outcome.gone += 1
                _forget(origin, ids, revisions, statuses, partial)
            else:
                # The delete may have landed with its answer lost.
                uncertain.add(origin)
                keep(origin, outcome.failures, f"delete failed ({exc})")
            return
        outcome.removed += 1
        _forget(origin, ids, revisions, statuses, partial)
        if outcome.removed % _SAVE_EVERY == 0:
            save()
            log(f"  {outcome.removed} removed...")

    # Undoing deletes, which takes a project maintainer. Ask before anything
    # else, so a refused undo writes nothing (not even a replayed create).
    if project_id and await access_refused(client, project_id):
        outcome.refused = True
        return outcome

    # A create whose answer was lost may have landed. Replay each under its
    # own key (the server answers with the original receipt, or creates the
    # row now), so the undo sees every row the migration wrote.
    unconfirmed = {
        origin: pending
        for origin, pending in partial.items()
        if origin not in ids and pending.get("create_key") and pending.get("create_body")
    }
    # Intents from before keys were kept cannot be replayed safely; say so.
    outcome.unresolved_unkeyed = sum(
        1
        for origin, pending in partial.items()
        if origin not in ids and pending.get("create_body") and not pending.get("create_key")
    )
    if dry_run:
        outcome.unresolved = len(unconfirmed)

    def saved_shape(body: Mapping[str, Any]) -> dict[str, Any]:
        return dict(body.get("shape") or {}) or _read_back_shape(body, ids, structure)

    def saved_links(body: Mapping[str, Any]) -> list[str]:
        return list(saved_shape(body).get("links") or [])

    for origin, pending in unconfirmed.items() if not dry_run else ():
        try:
            receipt, sent = await _replay_saved_create(client, pending)
        except Exception as exc:
            # Unknown: keep what any saved body would link to, so nothing kept
            # is stranded.
            for body in (pending, *(pending.get("earlier") or [])):
                still_linked.update(saved_links(body))
            outcome.failures.append(f"{origin}: could not confirm an unfinished create ({exc})")
            continue
        target_id = str(receipt.get("id") or "")
        if not target_id:
            for body in (pending, *(pending.get("earlier") or [])):
                still_linked.update(saved_links(body))
            outcome.failures.append(f"{origin}: an unfinished create returned no id")
            continue
        ids[origin] = target_id
        structure[origin] = saved_shape(sent)
        if receipt.get("revision") == 1:
            revisions[origin] = 1
        partial[origin] = {"missing": sent.get("missing") or [], "digest": None}
    if unconfirmed and not dry_run:
        save()

    # Phase one: classify every row before deleting any, and protect what the
    # kept ones point at now, so no layer is undone before the rows that keep
    # it are known.
    candidates: set[str] = set()
    kept_rows: dict[str, dict[str, Any] | None] = {}
    await asyncio.gather(*(classify(origin) for origin in list(ids)))
    try:
        await asyncio.gather(
            *(protect_current_links(origin, current) for origin, current in kept_rows.items())
        )
    except Exception as exc:
        outcome.failures.append(
            f"could not read what a kept row links to ({exc}); nothing was removed"
        )
        return outcome

    layers: dict[int, list[str]] = {}
    for origin in ids:
        layers.setdefault(int((structure.get(origin) or {}).get("layer") or 0), []).append(origin)
    for layer in sorted(layers, reverse=True):
        if outcome.refused:
            break
        marking = [origin for origin in layers[layer] if origin in candidates]
        if not dry_run:
            marks.update(marking)
            save()
        await asyncio.gather(*(undo_one(origin) for origin in layers[layer]))
        if not dry_run:
            # Removed and kept rows are settled; only a delete whose answer was
            # lost leaves its row marked for the next migration to check.
            marks.difference_update(o for o in marking if o not in uncertain)
            save()
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
