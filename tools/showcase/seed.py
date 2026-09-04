"""Seed an isolated local organization for public Sibyl screenshots."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from sibyl_cli import config_store
from sibyl_cli.client import SibylClient, SibylClientError
from tools.baselines.common import api_base_url, auth_headers, emit
from tools.showcase.fixtures import (
    KNOWLEDGE,
    PROJECTS,
    SHOWCASE_TAG,
    SOURCES,
    TASKS,
    KnowledgeFixture,
    ProjectFixture,
    SourceFixture,
    TaskFixture,
)

DEFAULT_BASE_URL = "http://localhost:3334"
DEFAULT_ORG_NAME = "Sibyl Showcase"
DEFAULT_ORG_SLUG = "sibyl-showcase"
DEFAULT_MANIFEST = Path(".moon/cache/showcase-runtime-manifest.json")
DEFAULT_FILTER_CONFIG = Path(".moon/cache/showcase-private-terms.json")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ORG_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class ShowcaseSafetyError(RuntimeError):
    """Raised before a showcase seed could mix with non-showcase data."""


def require_loopback_url(raw_url: str) -> str:
    """Return a normalized local URL or refuse the seed target."""
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        raise ShowcaseSafetyError("Showcase data can only be seeded into a loopback Sibyl server.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ShowcaseSafetyError("Showcase server URL must not contain credentials or options.")
    return raw_url.rstrip("/")


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield from _strings(getattr(value, field.name))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, Iterable):
        for item in value:
            yield from _strings(item)


def load_forbidden_terms(path: Path) -> tuple[str, ...]:
    """Load the required local-only screenshot filter."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ShowcaseSafetyError(
            f"Create the ignored screenshot filter config at {path} before seeding."
        ) from error
    except json.JSONDecodeError as error:
        raise ShowcaseSafetyError(f"Screenshot filter config is not valid JSON: {path}") from error

    raw_terms = payload.get("forbidden_terms") if isinstance(payload, Mapping) else None
    if not isinstance(raw_terms, list) or not raw_terms:
        raise ShowcaseSafetyError(
            "Screenshot filter config needs a non-empty forbidden_terms list."
        )
    terms = tuple(
        dict.fromkeys(
            term.strip().casefold() for term in raw_terms if isinstance(term, str) and term.strip()
        )
    )
    if len(terms) != len(raw_terms):
        raise ShowcaseSafetyError("Screenshot filter terms must be unique, non-empty strings.")
    return terms


def forbidden_terms(value: Any, terms: Iterable[str]) -> set[str]:
    """Return private terms found anywhere in a payload."""
    text = "\n".join(_strings(value)).casefold()
    return {term for term in terms if term in text}


def expected_entity_keys() -> set[tuple[str, str]]:
    return {
        *(("project", fixture.name) for fixture in PROJECTS),
        *(("task", fixture.title) for fixture in TASKS),
        *((fixture.entity_type, fixture.name) for fixture in KNOWLEDGE),
    }


def expected_source_keys() -> set[tuple[str, str]]:
    return {(fixture.name, fixture.url) for fixture in SOURCES}


def validate_fixture(terms: Iterable[str]) -> None:
    """Fail fast if the committed fixture is unsafe or internally ambiguous."""
    matches = forbidden_terms(
        {
            "projects": PROJECTS,
            "tasks": TASKS,
            "knowledge": KNOWLEDGE,
            "sources": SOURCES,
        },
        terms,
    )
    if matches:
        raise ShowcaseSafetyError("Showcase fixture contains forbidden private content.")
    if len(expected_entity_keys()) != len(PROJECTS) + len(TASKS) + len(KNOWLEDGE):
        raise ShowcaseSafetyError("Showcase entity names must be unique within each type.")
    if len(expected_source_keys()) != len(SOURCES):
        raise ShowcaseSafetyError("Showcase source names and URLs must be unique.")


async def active_cli_token(base_url: str) -> str:
    """Load the active local CLI token without printing or persisting it elsewhere."""
    api_url = api_base_url(base_url)
    env_token = os.getenv("SIBYL_AUTH_TOKEN", "").strip()
    if env_token:
        return env_token

    context = config_store.resolve_effective_context()
    if context is None:
        raise ShowcaseSafetyError("No active Sibyl CLI context. Run 'sibyl init' first.")
    context_api_url = api_base_url(context.server_url)
    if context_api_url != api_url:
        raise ShowcaseSafetyError(
            "The active Sibyl CLI context does not point at the showcase server."
        )
    try:
        async with SibylClient(context_name=context.name) as cli:
            await cli.list_orgs()
            token = cli.auth_token
    except SibylClientError as exc:
        raise ShowcaseSafetyError(
            "The active Sibyl login could not be renewed. Run 'sibyl auth login' first."
        ) from exc
    if not token:
        raise ShowcaseSafetyError("No local Sibyl login. Run 'sibyl auth login' first.")
    return token


async def resolve_showcase_org(
    client: httpx.AsyncClient,
    *,
    active_token: str,
    org_name: str,
    org_slug: str,
) -> tuple[str, dict[str, str]]:
    """Create or enter the dedicated organization and return its scoped token."""
    if not ORG_SLUG_PATTERN.fullmatch(org_slug):
        raise ShowcaseSafetyError("Showcase organization slug must be lowercase and URL-safe.")

    headers = auth_headers(active_token)
    list_response = await client.get("/orgs", headers=headers)
    list_response.raise_for_status()
    orgs = list_response.json().get("orgs", [])
    existing = next((org for org in orgs if org.get("slug") == org_slug), None)

    if existing is not None:
        if existing.get("name") != org_name or existing.get("is_personal"):
            raise ShowcaseSafetyError(
                "The showcase organization slug belongs to a different organization."
            )
        response = await client.post(
            f"/orgs/{quote(org_slug, safe='')}/switch",
            headers=headers,
        )
    else:
        response = await client.post(
            "/orgs",
            headers=headers,
            json={"name": org_name, "slug": org_slug},
        )
        if response.status_code == httpx.codes.CONFLICT:
            list_response = await client.get("/orgs", headers=headers)
            list_response.raise_for_status()
            orgs = list_response.json().get("orgs", [])
            existing = next((org for org in orgs if org.get("slug") == org_slug), None)
            if existing is None or existing.get("name") != org_name or existing.get("is_personal"):
                raise ShowcaseSafetyError(
                    "The showcase organization slug belongs to a different organization."
                )
            response = await client.post(
                f"/orgs/{quote(org_slug, safe='')}/switch",
                headers=headers,
            )
    response.raise_for_status()
    payload = response.json()
    token = str(payload.get("access_token") or "")
    organization = payload.get("organization") or {}
    if not token or organization.get("slug") != org_slug:
        raise ShowcaseSafetyError("Sibyl returned an invalid showcase organization session.")
    return token, {
        "id": str(organization["id"]),
        "name": str(organization["name"]),
        "slug": str(organization["slug"]),
    }


async def _list_entities(client: httpx.AsyncClient, token: str) -> list[dict[str, Any]]:
    response = await client.get(
        "/entities",
        headers=auth_headers(token),
        params={"page_size": 200, "sort_by": "name", "sort_order": "asc"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("has_more"):
        raise ShowcaseSafetyError("Showcase verifier refuses organizations over 200 entities.")
    summaries = list(payload.get("entities", []))
    details = await asyncio.gather(
        *(
            client.get(
                f"/entities/{quote(str(summary['id']), safe='')}",
                headers=auth_headers(token),
                params={"include_summary": "false", "related_limit": 0},
            )
            for summary in summaries
        )
    )
    for response in details:
        response.raise_for_status()
    return [dict(response.json()) for response in details]


async def _list_sources(client: httpx.AsyncClient, token: str) -> list[dict[str, Any]]:
    response = await client.get("/sources", headers=auth_headers(token), params={"limit": 200})
    response.raise_for_status()
    return _complete_source_page(response.json())


def _complete_source_page(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = list(payload.get("sources", []))
    if payload.get("total") != len(sources):
        raise ShowcaseSafetyError("Showcase verifier refuses organizations over 200 sources.")
    return [dict(source) for source in sources]


def _entity_is_marked(entity: Mapping[str, Any]) -> bool:
    metadata = entity.get("metadata") or {}
    tags = entity.get("tags") or []
    return metadata.get("capture_mode") == "showcase" or SHOWCASE_TAG in tags


def _duplicate_keys(keys: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    return {key for key, count in Counter(keys).items() if count > 1}


def _project_ids(entities: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    projects_by_name = {
        str(row.get("name")): str(row.get("id"))
        for row in entities
        if str(row.get("entity_type")) == "project"
    }
    return {
        fixture.key: projects_by_name[fixture.name]
        for fixture in PROJECTS
        if fixture.name in projects_by_name
    }


def _expected_entity_projections(project_ids: Mapping[str, str]) -> dict[tuple[str, str], Any]:
    expected: dict[tuple[str, str], Any] = {}
    for fixture in PROJECTS:
        expected[("project", fixture.name)] = {
            "entity_type": "project",
            "name": fixture.name,
            "description": fixture.description,
            "content": fixture.description,
            "category": "showcase",
            "languages": list(fixture.languages),
            "tags": [SHOWCASE_TAG, "project"],
            "source_file": None,
            "metadata": {
                "capture_mode": "showcase",
                "technologies": list(fixture.technologies),
                "category": "showcase",
                "languages": list(fixture.languages),
                "tags": [SHOWCASE_TAG, "project"],
                "description": fixture.description,
                "source_file": "",
                "_direct_insert": True,
                "title": fixture.name,
                "status": "active",
                "tech_stack": list(fixture.technologies),
                "total_tasks": 0,
                "completed_tasks": 0,
                "in_progress_tasks": 0,
            },
        }
    for fixture in TASKS:
        project_id = project_ids.get(fixture.project)
        if project_id is None:
            continue
        expected[("task", fixture.title)] = {
            "entity_type": "task",
            "name": fixture.title,
            "description": fixture.description,
            "content": fixture.description,
            "category": None,
            "languages": [],
            "tags": [SHOWCASE_TAG],
            "source_file": None,
            "metadata": {
                "title": fixture.title,
                "status": fixture.status,
                "priority": fixture.priority,
                "task_order": 0,
                "complexity": fixture.complexity,
                "project_id": project_id,
                "feature": fixture.feature,
                "technologies": list(fixture.technologies),
                "tags": [SHOWCASE_TAG],
                "description": fixture.description,
                "source_file": "",
                "_direct_insert": True,
            },
        }
    for fixture in KNOWLEDGE:
        project_id = project_ids.get(fixture.project)
        if project_id is None:
            continue
        expected[(fixture.entity_type, fixture.name)] = {
            "entity_type": fixture.entity_type,
            "name": fixture.name,
            # The graph writer derives persisted descriptions from content for
            # non-project knowledge entities.
            "description": fixture.content,
            "content": fixture.content,
            "category": fixture.category,
            "languages": [],
            "tags": [SHOWCASE_TAG, *fixture.tags],
            "source_file": None,
            "metadata": {
                "capture_mode": "showcase",
                "project_id": project_id,
                "category": fixture.category,
                "languages": [],
                "tags": [SHOWCASE_TAG, *fixture.tags],
                "description": fixture.content,
                "source_file": "",
                "_direct_insert": True,
                **({"confidence": 1.0} if fixture.entity_type == "pattern" else {}),
                **({"automation_level": "manual"} if fixture.entity_type == "procedure" else {}),
            },
        }
    return expected


_DYNAMIC_METADATA_FIELDS = frozenset(
    {
        "added_at",
        "created_by",
        "modified_by",
        "organization_id",
        "record_id",
        "updated_at",
    }
)


def _stable_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in metadata.items()
        if str(key) not in _DYNAMIC_METADATA_FIELDS
    }


def _entity_projection(entity: Mapping[str, Any]) -> dict[str, Any]:
    entity_type = str(entity.get("entity_type"))
    metadata = entity.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}

    projection: dict[str, Any] = {
        "entity_type": entity_type,
        "name": str(entity.get("name") or ""),
        "description": str(entity.get("description") or ""),
        "content": str(entity.get("content") or ""),
        "category": entity.get("category"),
        "languages": list(entity.get("languages") or []),
        "tags": list(entity.get("tags") or []),
        "source_file": entity.get("source_file"),
        "metadata": _stable_metadata(metadata),
    }
    return projection


def _expected_source_projections() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (fixture.name, fixture.url): {
            "name": fixture.name,
            "url": fixture.url,
            "source_type": fixture.source_type,
            "description": fixture.description,
            "crawl_depth": 2,
            "include_patterns": [],
            "exclude_patterns": [],
            "crawl_status": "pending",
            "document_count": 0,
            "chunk_count": 0,
            "last_crawled_at": None,
            "last_error": None,
        }
        for fixture in SOURCES
    }


def _source_projection(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(source.get("name") or ""),
        "url": str(source.get("url") or ""),
        "source_type": str(source.get("source_type") or ""),
        "description": str(source.get("description") or ""),
        "crawl_depth": source.get("crawl_depth"),
        "include_patterns": list(source.get("include_patterns") or []),
        "exclude_patterns": list(source.get("exclude_patterns") or []),
        "crawl_status": str(source.get("crawl_status") or ""),
        "document_count": source.get("document_count"),
        "chunk_count": source.get("chunk_count"),
        "last_crawled_at": source.get("last_crawled_at"),
        "last_error": source.get("last_error"),
    }


def build_corpus_snapshot(
    entities: Iterable[Mapping[str, Any]],
    sources: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Seal the exact persisted showcase fields for the browser capture."""
    entity_rows = [
        {
            "id": str(entity.get("id") or ""),
            **_entity_projection(entity),
            "metadata": dict(entity.get("metadata") or {}),
            "created_at": entity.get("created_at"),
            "updated_at": entity.get("updated_at"),
            "related": entity.get("related"),
            "background_jobs": dict(entity.get("background_jobs") or {}),
            "probe_rehearsal": entity.get("probe_rehearsal"),
        }
        for entity in entities
    ]
    source_rows = [
        {
            "id": str(source.get("id") or ""),
            **_source_projection(source),
            "created_at": source.get("created_at"),
        }
        for source in sources
    ]
    return {
        "entities": sorted(
            entity_rows,
            key=lambda row: (row["entity_type"], row["name"], row["id"]),
        ),
        "sources": sorted(
            source_rows,
            key=lambda row: (row["name"], row["url"], row["id"]),
        ),
    }


def _differing_paths(actual: Any, expected: Any, prefix: str = "") -> list[str]:
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        paths: list[str] = []
        for key in sorted(set(actual) | set(expected), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in actual or key not in expected:
                paths.append(path)
            else:
                paths.extend(_differing_paths(actual[key], expected[key], path))
        return paths
    return [] if actual == expected else [prefix]


def _isolation_failure(label: str, values: Any) -> str | None:
    if not values:
        return None
    items = values.items() if isinstance(values, Mapping) else values
    return f"{label}: {sorted(items)!r}"


def validate_existing_data(
    entities: Iterable[Mapping[str, Any]],
    sources: Iterable[Mapping[str, Any]],
    *,
    forbidden_public_terms: Iterable[str],
    require_complete: bool,
) -> None:
    """Prove that an organization contains only the committed showcase corpus."""
    entity_rows = list(entities)
    source_rows = list(sources)
    if forbidden_terms({"entities": entity_rows, "sources": source_rows}, forbidden_public_terms):
        raise ShowcaseSafetyError("Showcase organization contains forbidden private content.")

    entity_key_list = [(str(row.get("entity_type")), str(row.get("name"))) for row in entity_rows]
    source_key_list = [(str(row.get("name")), str(row.get("url"))) for row in source_rows]
    entity_keys = set(entity_key_list)
    source_keys = set(source_key_list)

    unknown_entities = entity_keys - expected_entity_keys()
    unknown_sources = source_keys - expected_source_keys()
    unmarked_entities = {
        (str(row.get("entity_type")), str(row.get("name")))
        for row in entity_rows
        if not _entity_is_marked(row)
    }
    duplicate_entities = _duplicate_keys(entity_key_list)
    duplicate_sources = _duplicate_keys(source_key_list)

    project_ids = _project_ids(entity_rows)
    expected_entities = _expected_entity_projections(project_ids)
    expected_sources = _expected_source_projections()
    unresolved_dependencies = {
        key
        for key in entity_keys & expected_entity_keys()
        if key[0] != "project" and key not in expected_entities
    }
    drifted_entities = {
        key: _differing_paths(_entity_projection(row), expected_entities[key])
        for key, row in zip(entity_key_list, entity_rows, strict=True)
        if key in expected_entities and _entity_projection(row) != expected_entities[key]
    }
    drifted_sources = {
        key: _differing_paths(_source_projection(row), expected_sources[key])
        for key, row in zip(source_key_list, source_rows, strict=True)
        if key in expected_sources and _source_projection(row) != expected_sources[key]
    }

    checks = (
        ("unexpected entities", unknown_entities),
        ("unexpected sources", unknown_sources),
        ("unmarked entities", unmarked_entities),
        ("duplicate entities", duplicate_entities),
        ("duplicate sources", duplicate_sources),
        ("entities without fixture projects", unresolved_dependencies),
        ("drifted entities", drifted_entities),
        ("drifted sources", drifted_sources),
    )
    failures = [
        failure for label, values in checks if (failure := _isolation_failure(label, values))
    ]
    if require_complete:
        missing_entities = expected_entity_keys() - entity_keys
        missing_sources = expected_source_keys() - source_keys
        for label, values in (
            ("missing entities", missing_entities),
            ("missing sources", missing_sources),
        ):
            failure = _isolation_failure(label, values)
            if failure:
                failures.append(failure)

    if failures:
        joined = "; ".join(failures)
        raise ShowcaseSafetyError(f"Showcase organization failed isolation checks: {joined}")


def _project_payload(fixture: ProjectFixture) -> dict[str, Any]:
    return {
        "name": fixture.name,
        "description": fixture.description,
        "content": fixture.description,
        "entity_type": "project",
        "category": "showcase",
        "languages": list(fixture.languages),
        "tags": [SHOWCASE_TAG, "project"],
        "metadata": {
            "capture_mode": "showcase",
            "technologies": list(fixture.technologies),
        },
        "defer_embeddings": True,
    }


def _task_payload(fixture: TaskFixture, project_id: str) -> dict[str, Any]:
    return {
        "title": fixture.title,
        "description": fixture.description,
        "project_id": project_id,
        "priority": fixture.priority,
        "complexity": fixture.complexity,
        "status": fixture.status,
        "feature": fixture.feature,
        "tags": [SHOWCASE_TAG],
        "technologies": list(fixture.technologies),
    }


def _knowledge_payload(fixture: KnowledgeFixture, project_id: str) -> dict[str, Any]:
    return {
        "name": fixture.name,
        "description": fixture.description,
        "content": fixture.content,
        "entity_type": fixture.entity_type,
        "category": fixture.category,
        "tags": [SHOWCASE_TAG, *fixture.tags],
        "metadata": {
            "capture_mode": "showcase",
            "project_id": project_id,
        },
        "related_to": [project_id],
        "defer_embeddings": True,
        "skip_conflicts": True,
    }


def _source_payload(fixture: SourceFixture) -> dict[str, Any]:
    return {
        "name": fixture.name,
        "url": fixture.url,
        "source_type": fixture.source_type,
        "description": fixture.description,
        "crawl_depth": 2,
        "include_patterns": [],
        "exclude_patterns": [],
    }


async def _create_entity(
    client: httpx.AsyncClient, token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    response = await _post_idempotent(
        client,
        token,
        "/entities",
        payload,
        params={"sync": "true"},
    )
    response.raise_for_status()
    return dict(response.json())


def _idempotency_key(kind: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"sibyl-showcase-v1:{kind}:{digest}"


async def _post_idempotent(
    client: httpx.AsyncClient,
    token: str,
    path: str,
    payload: dict[str, Any],
    *,
    params: Mapping[str, str] | None = None,
) -> httpx.Response:
    headers = {
        **auth_headers(token),
        "Idempotency-Key": _idempotency_key(path.strip("/").replace("/", "-"), payload),
    }
    response: httpx.Response | None = None
    for delay in (0.0, 0.05, 0.2, 0.8, 2.0):
        if delay:
            await asyncio.sleep(delay)
        response = await client.post(path, headers=headers, params=params, json=payload)
        if response.status_code != httpx.codes.CONFLICT:
            return response
        try:
            detail = response.json().get("detail")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        if detail != "An identical idempotent request is still in progress. Please retry.":
            return response
    assert response is not None
    return response


async def _create_source(client: httpx.AsyncClient, token: str, payload: dict[str, Any]) -> bool:
    response = await _post_idempotent(client, token, "/sources", payload)
    if response.status_code == httpx.codes.CONFLICT:
        return False
    response.raise_for_status()
    return True


async def _seed_showcase(
    client: httpx.AsyncClient,
    *,
    active_token: str,
    forbidden_public_terms: tuple[str, ...],
    org_name: str,
    org_slug: str,
) -> dict[str, Any]:
    token, organization = await resolve_showcase_org(
        client,
        active_token=active_token,
        org_name=org_name,
        org_slug=org_slug,
    )

    existing_entities, existing_sources = await asyncio.gather(
        _list_entities(client, token),
        _list_sources(client, token),
    )
    validate_existing_data(
        existing_entities,
        existing_sources,
        forbidden_public_terms=forbidden_public_terms,
        require_complete=False,
    )

    entity_index = {
        (str(entity["entity_type"]), str(entity["name"])): entity for entity in existing_entities
    }
    source_index = {
        (str(source["name"]), str(source["url"])): source for source in existing_sources
    }

    missing_projects = [
        fixture for fixture in PROJECTS if ("project", fixture.name) not in entity_index
    ]
    created_projects = await asyncio.gather(
        *(_create_entity(client, token, _project_payload(fixture)) for fixture in missing_projects)
    )
    entity_index.update((("project", str(entity["name"])), entity) for entity in created_projects)
    project_ids = {
        fixture.key: str(entity_index[("project", fixture.name)]["id"]) for fixture in PROJECTS
    }

    missing_tasks = [fixture for fixture in TASKS if ("task", fixture.title) not in entity_index]
    missing_knowledge = [
        fixture for fixture in KNOWLEDGE if (fixture.entity_type, fixture.name) not in entity_index
    ]
    missing_sources = [
        fixture for fixture in SOURCES if (fixture.name, fixture.url) not in source_index
    ]

    task_responses, knowledge_responses, source_responses = await asyncio.gather(
        asyncio.gather(
            *(
                _post_idempotent(
                    client,
                    token,
                    "/tasks",
                    _task_payload(fixture, project_ids[fixture.project]),
                )
                for fixture in missing_tasks
            )
        ),
        asyncio.gather(
            *(
                _create_entity(
                    client,
                    token,
                    _knowledge_payload(fixture, project_ids[fixture.project]),
                )
                for fixture in missing_knowledge
            )
        ),
        asyncio.gather(
            *(
                _create_source(
                    client,
                    token,
                    _source_payload(fixture),
                )
                for fixture in missing_sources
            )
        ),
    )
    for response in task_responses:
        response.raise_for_status()

    final_entities, final_sources = await asyncio.gather(
        _list_entities(client, token),
        _list_sources(client, token),
    )
    validate_existing_data(
        final_entities,
        final_sources,
        forbidden_public_terms=forbidden_public_terms,
        require_complete=True,
    )

    return {
        "base_url": str(client.base_url).rstrip("/"),
        "organization": organization,
        "entity_count": len(final_entities),
        "source_count": len(final_sources),
        "project_ids": project_ids,
        "corpus": build_corpus_snapshot(final_entities, final_sources),
        "created": {
            "projects": len(created_projects),
            "tasks": len(task_responses),
            "knowledge": len(knowledge_responses),
            "sources": sum(source_responses),
        },
    }


async def seed_showcase(
    *,
    base_url: str,
    filter_config: Path,
    org_name: str,
    org_slug: str,
) -> dict[str, Any]:
    """Seed and verify the showcase corpus without exposing its auth token."""
    forbidden_public_terms = load_forbidden_terms(filter_config)
    validate_fixture(forbidden_public_terms)
    safe_base_url = require_loopback_url(base_url)
    token = await active_cli_token(safe_base_url)
    async with httpx.AsyncClient(base_url=api_base_url(safe_base_url), timeout=60.0) as client:
        return await _seed_showcase(
            client,
            active_token=token,
            forbidden_public_terms=forbidden_public_terms,
            org_name=org_name,
            org_slug=org_slug,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed a dedicated local organization for public screenshots."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("SIBYL_SHOWCASE_URL", DEFAULT_BASE_URL),
        help="Loopback Sibyl server URL.",
    )
    parser.add_argument(
        "--org-name",
        default=os.getenv("SIBYL_SHOWCASE_ORG_NAME", DEFAULT_ORG_NAME),
        help="Display name for the isolated showcase organization.",
    )
    parser.add_argument(
        "--org-slug",
        default=os.getenv("SIBYL_SHOWCASE_ORG_SLUG", DEFAULT_ORG_SLUG),
        help="Slug for the isolated showcase organization.",
    )
    parser.add_argument(
        "--filter-config",
        type=Path,
        default=DEFAULT_FILTER_CONFIG,
        help="Path to the ignored local screenshot filter config.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path for the value-free runtime manifest.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    manifest = asyncio.run(
        seed_showcase(
            base_url=args.base_url,
            filter_config=args.filter_config,
            org_name=args.org_name,
            org_slug=args.org_slug,
        )
    )
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    emit(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
