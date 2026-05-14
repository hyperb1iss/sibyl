from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import httpx

DEFAULT_BASE_URL = "http://localhost:3334"
DEFAULT_BASELINES_DIR = Path("baselines")
DEFAULT_BASELINE_EMAIL = "baseline-corpus@sibyl.dev"
DEFAULT_BASELINE_PASSWORD = "baseline-corpus-password-secure-123!"  # noqa: S105
DEFAULT_BASELINE_NAME = "Baseline Corpus User"
REST_SEED_TITLE = "Baseline Corpus Episode"
REST_SEED_DESCRIPTION = "Baseline corpus seed for search and graph smoke tests."
REST_SEED_CONTENT = "Episode used as baseline corpus seed for search and graph smoke tests."
MCP_ADD_TITLE = "Baseline MCP Smoke Pattern"
MCP_ADD_CONTENT = "Baseline MCP smoke pattern for replay."
GRAPH_PROJECT_NAME = "Cinder Atlas"
GRAPH_PROJECT_DESCRIPTION = "Baseline project anchor for graph replay coverage."
GRAPH_PROJECT_CONTENT = "Project focused on resilience hardening for deterministic graph replay."
GRAPH_EPIC_NAME = "Velvet Quill"
GRAPH_EPIC_DESCRIPTION = "Baseline epic bound to the project anchor."
GRAPH_EPIC_CONTENT = "Epic that organizes the baseline dependency graph fixture."
GRAPH_TASK_A_NAME = "Obsidian Spire"
GRAPH_TASK_A_DESCRIPTION = "Baseline dependency predecessor task."
GRAPH_TASK_A_CONTENT = "Task covering storage adapter extraction and seam isolation."
GRAPH_TASK_B_NAME = "Silver Delta"
GRAPH_TASK_B_DESCRIPTION = "Baseline dependency successor task."
GRAPH_TASK_B_CONTENT = "Task covering replay verification and migration acceptance."
BASELINE_TAGS = ["baseline-corpus"]
HTTP_OK = 200
AUTH_RETRY_DELAYS = (0.25, 0.5, 1.0)
CASE_FILE_ORDER = (
    "auth_smoke.jsonl",
    "rest_smoke.jsonl",
    "graph_smoke.jsonl",
    "search_queries.jsonl",
    "mcp_smoke.jsonl",
)
PLACEHOLDER_PATTERN = re.compile(r"\{\{([a-zA-Z0-9_.-]+)\}\}")


def emit(message: str, stream: TextIO = sys.stdout) -> None:
    stream.write(f"{message}\n")


def baseline_base_url() -> str:
    return os.getenv("SIBYL_BASELINE_URL", DEFAULT_BASE_URL).rstrip("/")


def api_base_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api"


def mcp_base_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/mcp"


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def manifest_ref(*parts: str) -> str:
    return "{{" + ".".join(parts) + "}}"


def graph_ref(name: str, field: str = "id") -> str:
    return manifest_ref("graph_fixture", name, field)


async def login_or_signup(
    client: httpx.AsyncClient,
    *,
    email: str = DEFAULT_BASELINE_EMAIL,
    password: str = DEFAULT_BASELINE_PASSWORD,
    name: str = DEFAULT_BASELINE_NAME,
) -> dict[str, Any]:
    login_response = await login_with_retry(client, email=email, password=password)
    if login_response.status_code == HTTP_OK:
        return parse_http_response(login_response)["body"]

    signup_response = await client.post(
        "/auth/local/signup",
        json={"email": email, "password": password, "name": name},
    )
    if signup_response.status_code == httpx.codes.CONFLICT:
        retry_login = await login_with_retry(client, email=email, password=password)
        retry_login.raise_for_status()
        return parse_http_response(retry_login)["body"]
    signup_response.raise_for_status()
    return parse_http_response(signup_response)["body"]


async def login_with_retry(
    client: httpx.AsyncClient,
    *,
    email: str,
    password: str,
) -> httpx.Response:
    response: httpx.Response | None = None
    for delay in [0.0, *AUTH_RETRY_DELAYS]:
        if delay:
            await asyncio.sleep(delay)
        response = await client.post(
            "/auth/local/login",
            json={"email": email, "password": password},
        )
        if response.status_code != httpx.codes.TOO_MANY_REQUESTS:
            return response
    assert response is not None
    return response


def parse_http_response(response: httpx.Response) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except json.JSONDecodeError:
        body = response.text
    return {"status_code": response.status_code, "body": body}


async def ensure_rest_seed(client: httpx.AsyncClient, token: str) -> dict[str, Any]:
    headers = auth_headers(token)
    search_response = await client.post(
        "/search",
        headers=headers,
        json={"query": REST_SEED_TITLE, "limit": 10},
    )
    search_response.raise_for_status()
    search_payload = parse_http_response(search_response)["body"]
    if isinstance(search_payload, dict):
        for result in search_payload.get("results", []):
            if result.get("name") == REST_SEED_TITLE:
                return result

    create_response = await client.post(
        "/entities",
        params={"sync": "true"},
        headers=headers,
        json={
            "name": REST_SEED_TITLE,
            "description": REST_SEED_DESCRIPTION,
            "content": REST_SEED_CONTENT,
            "entity_type": "episode",
            "category": "baseline",
            "tags": [*BASELINE_TAGS, "rest-smoke"],
            "metadata": {
                "capture_mode": "baseline",
                "capture_surface": "rest",
            },
        },
    )
    create_response.raise_for_status()
    return parse_http_response(create_response)["body"]


async def find_entity(
    client: httpx.AsyncClient,
    token: str,
    *,
    entity_type: str,
    name: str,
) -> dict[str, Any] | None:
    headers = auth_headers(token)
    response = await client.get(
        "/entities",
        headers=headers,
        params={
            "entity_type": entity_type,
            "search": name,
            "page_size": 25,
            "sort_by": "name",
            "sort_order": "asc",
        },
    )
    response.raise_for_status()
    payload = parse_http_response(response)["body"]
    if not isinstance(payload, dict):
        return None

    for entity in payload.get("entities", []):
        if entity.get("name") == name:
            return entity
    return None


async def ensure_entity(
    client: httpx.AsyncClient,
    token: str,
    *,
    entity_type: str,
    name: str,
    description: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    existing = await find_entity(client, token, entity_type=entity_type, name=name)
    if existing is not None:
        return existing

    create_response = await client.post(
        "/entities",
        params={"sync": "true"},
        headers=auth_headers(token),
        json={
            "name": name,
            "description": description,
            "content": content,
            "entity_type": entity_type,
            "category": "baseline",
            "tags": [*(tags or BASELINE_TAGS)],
            "metadata": metadata or {},
        },
    )
    create_response.raise_for_status()
    return parse_http_response(create_response)["body"]


async def ensure_graph_fixture(client: httpx.AsyncClient, token: str) -> dict[str, dict[str, Any]]:
    fixture_metadata = {
        "capture_mode": "baseline",
        "capture_surface": "graph",
    }

    project = await ensure_entity(
        client,
        token,
        entity_type="project",
        name=GRAPH_PROJECT_NAME,
        description=GRAPH_PROJECT_DESCRIPTION,
        content=GRAPH_PROJECT_CONTENT,
        metadata=fixture_metadata,
        tags=[*BASELINE_TAGS, "graph-fixture"],
    )
    epic = await ensure_entity(
        client,
        token,
        entity_type="epic",
        name=GRAPH_EPIC_NAME,
        description=GRAPH_EPIC_DESCRIPTION,
        content=GRAPH_EPIC_CONTENT,
        metadata={
            **fixture_metadata,
            "project_id": project["id"],
        },
        tags=[*BASELINE_TAGS, "graph-fixture"],
    )
    task_a = await ensure_entity(
        client,
        token,
        entity_type="task",
        name=GRAPH_TASK_A_NAME,
        description=GRAPH_TASK_A_DESCRIPTION,
        content=GRAPH_TASK_A_CONTENT,
        metadata={
            **fixture_metadata,
            "project_id": project["id"],
            "epic_id": epic["id"],
            "priority": "high",
        },
        tags=[*BASELINE_TAGS, "graph-fixture"],
    )
    task_b = await ensure_entity(
        client,
        token,
        entity_type="task",
        name=GRAPH_TASK_B_NAME,
        description=GRAPH_TASK_B_DESCRIPTION,
        content=GRAPH_TASK_B_CONTENT,
        metadata={
            **fixture_metadata,
            "project_id": project["id"],
            "epic_id": epic["id"],
            "priority": "medium",
            "depends_on": [task_a["id"]],
        },
        tags=[*BASELINE_TAGS, "graph-fixture"],
    )

    return {
        "project": project,
        "epic": epic,
        "task_a": task_a,
        "task_b": task_b,
    }


async def ensure_raw_memory(
    client: httpx.AsyncClient,
    token: str,
    *,
    title: str,
    raw_content: str,
    source_id: str,
    diary: bool = False,
    agent_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = auth_headers(token)
    recall_payload: dict[str, Any] = {
        "query": title,
        "memory_scope": "private",
        "limit": 10,
    }
    if diary:
        recall_payload.update({"diary": True, "agent_id": agent_id})

    recall_response = await client.post(
        "/memory/raw/recall",
        headers=headers,
        json=recall_payload,
    )
    recall_response.raise_for_status()
    recall_payload = parse_http_response(recall_response)["body"]
    if isinstance(recall_payload, dict):
        for memory in recall_payload.get("memories", []):
            if memory.get("source_id") == source_id or memory.get("title") == title:
                return memory

    create_response = await client.post(
        "/memory/raw",
        headers=headers,
        json={
            "title": title,
            "raw_content": raw_content,
            "source_id": source_id,
            "memory_scope": "private",
            "diary": diary,
            "agent_id": agent_id,
            "tags": [*(tags or BASELINE_TAGS), "context-pack"],
            "metadata": {"capture_mode": "baseline", **(metadata or {})},
            "provenance": {"source": "baseline-seed"},
            "capture_surface": "baseline",
        },
    )
    create_response.raise_for_status()
    return parse_http_response(create_response)["body"]


async def ensure_raw_memory_fixture(
    client: httpx.AsyncClient,
    token: str,
) -> dict[str, dict[str, Any]]:
    personal = await ensure_raw_memory(
        client,
        token,
        title="Personal Baseline Memory",
        raw_content=("Personal baseline memory says remember Amethyst Loom for private recall."),
        source_id="baseline:personal-memory",
        tags=[*BASELINE_TAGS, "personal-memory"],
    )
    coding_handoff = await ensure_raw_memory(
        client,
        token,
        title="Coding Handoff Baseline",
        raw_content=(
            "Coding handoff baseline says Silver Delta covers replay verification "
            "and migration acceptance."
        ),
        source_id="baseline:coding-handoff",
        tags=[*BASELINE_TAGS, "coding-handoff"],
    )
    project_recall = await ensure_raw_memory(
        client,
        token,
        title="Project Recall Baseline",
        raw_content=(
            "Project recall baseline says Cinder Atlas focuses on resilience "
            "hardening for deterministic graph replay."
        ),
        source_id="baseline:project-recall",
        tags=[*BASELINE_TAGS, "project-recall"],
    )
    delegated_recall = await ensure_raw_memory(
        client,
        token,
        title="Delegated Recall Baseline",
        raw_content=(
            "Delegated recall baseline says Obsidian Spire covers storage adapter "
            "extraction and isolation."
        ),
        source_id="baseline:delegated-recall",
        tags=[*BASELINE_TAGS, "delegated-recall"],
    )
    agent_diary = await ensure_raw_memory(
        client,
        token,
        title="Nova Baseline Diary",
        raw_content="Nova diary says checkpoint Neon Thread for delegated handoff.",
        source_id="baseline:agent-diary",
        diary=True,
        agent_id="nova",
        tags=[*BASELINE_TAGS, "agent-diary"],
        metadata={"agent_id": "nova", "memory_kind": "agent_diary"},
    )
    source_grounding = await ensure_raw_memory(
        client,
        token,
        title="Source Grounding Baseline",
        raw_content=("Source grounding baseline says source links survive promotion."),
        source_id="baseline:source-grounding",
        tags=[*BASELINE_TAGS, "source-grounding"],
    )
    stale_decision = await ensure_raw_memory(
        client,
        token,
        title="Stale Decision Replacement Baseline",
        raw_content=(
            "Stale decision replacement baseline says Silver Delta supersedes "
            "old shortcuts for migration acceptance."
        ),
        source_id="baseline:stale-decision-replacement",
        tags=[*BASELINE_TAGS, "stale-decision-replacement"],
    )
    return {
        "personal": personal,
        "coding_handoff": coding_handoff,
        "project_recall": project_recall,
        "delegated_recall": delegated_recall,
        "agent_diary": agent_diary,
        "source_grounding": source_grounding,
        "stale_decision": stale_decision,
    }


def write_manifest(
    path: Path,
    *,
    base_url: str,
    email: str,
    rest_seed: dict[str, Any],
    graph_fixture: dict[str, dict[str, Any]],
    raw_memory_fixture: dict[str, dict[str, Any]] | None = None,
    access_token: str | None = None,
) -> None:
    manifest = {
        "captured_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "email": email,
        "files": list(CASE_FILE_ORDER),
        "rest_seed": {
            "title": REST_SEED_TITLE,
            "id": rest_seed.get("id"),
            "entity_type": rest_seed.get("entity_type") or rest_seed.get("type"),
        },
        "mcp_add": {
            "title": MCP_ADD_TITLE,
            "note": "The replay corpus asserts current MCP add and manage org-context behavior.",
        },
        "graph_fixture": {
            name: {
                "id": entity["id"],
                "name": entity["name"],
                "entity_type": entity["entity_type"],
            }
            for name, entity in graph_fixture.items()
        },
    }
    if raw_memory_fixture:
        manifest["raw_memory_fixture"] = {
            name: {
                "id": memory["id"],
                "title": memory["title"],
                "source_id": memory["source_id"],
            }
            for name, memory in raw_memory_fixture.items()
        }
    if access_token:
        manifest["auth"] = {"access_token": access_token}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(manifest, indent=2, sort_keys=True)}\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{json.dumps(row, sort_keys=True)}\n" for row in rows),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise TypeError(f"Expected object rows in {path}, got {type(payload).__name__}")
        rows.append(payload)
    return rows


def dump_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2)


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Expected object manifest in {path}, got {type(payload).__name__}")
    return payload


def _resolve_manifest_value(manifest: dict[str, Any], dotted_path: str) -> Any:
    current: Any = manifest
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            raise KeyError(dotted_path)
        current = current[part]
    return current


def resolve_placeholders(value: Any, manifest: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [resolve_placeholders(item, manifest) for item in value]
    if isinstance(value, dict):
        return {key: resolve_placeholders(item, manifest) for key, item in value.items()}
    if not isinstance(value, str):
        return value

    matches = list(PLACEHOLDER_PATTERN.finditer(value))
    if not matches:
        return value

    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return _resolve_manifest_value(manifest, matches[0].group(1))

    resolved = value
    for match in matches:
        replacement = _resolve_manifest_value(manifest, match.group(1))
        resolved = resolved.replace(match.group(0), str(replacement))
    return resolved


def resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer must start with '/': {pointer}")

    current = document
    for raw_part in pointer.strip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
            continue
        if isinstance(current, dict):
            current = current[part]
            continue
        raise KeyError(pointer)
    return current


def matches_partial(candidate: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict):
        return False
    return all(candidate.get(key) == value for key, value in expected.items())


def _validate_required(actual: Any, pointers: list[str]) -> list[str]:
    errors: list[str] = []
    for pointer in pointers:
        try:
            resolve_pointer(actual, pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            errors.append(f"missing required pointer: {pointer}")
    return errors


def _validate_equals(actual: Any, expected_values: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for pointer, expected_value in expected_values.items():
        try:
            actual_value = resolve_pointer(actual, pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            errors.append(f"missing pointer for equals check: {pointer}")
            continue
        if actual_value != expected_value:
            errors.append(f"pointer {pointer} expected {expected_value!r} but got {actual_value!r}")
    return errors


def _validate_minimums(actual: Any, minimums: dict[str, int | float]) -> list[str]:
    errors: list[str] = []
    for pointer, minimum in minimums.items():
        try:
            actual_value = resolve_pointer(actual, pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            errors.append(f"missing pointer for minimum check: {pointer}")
            continue
        if isinstance(actual_value, list | dict | str):
            comparable: Any = len(actual_value)
        else:
            comparable = actual_value
        if not isinstance(comparable, int | float) or comparable < minimum:
            errors.append(
                f"pointer {pointer} expected minimum {minimum!r} but got {actual_value!r}"
            )
    return errors


def _validate_list_contains(actual: Any, rules: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for rule in rules:
        pointer = str(rule["pointer"])
        match = rule["match"]
        try:
            items = resolve_pointer(actual, pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            errors.append(f"missing pointer for list_contains check: {pointer}")
            continue
        if not isinstance(items, list) or not any(matches_partial(item, match) for item in items):
            errors.append(f"pointer {pointer} did not contain match {match!r}")
    return errors


def _validate_serialized_contains(actual: Any, needles: list[str]) -> list[str]:
    errors: list[str] = []
    serialized = dump_json(actual)
    for needle in needles:
        if needle not in serialized:
            errors.append(f"serialized payload missing substring: {needle!r}")
    return errors


def validate_expectations(actual: Any, expect: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(_validate_required(actual, expect.get("required", [])))
    errors.extend(_validate_equals(actual, expect.get("equals", {})))
    errors.extend(_validate_minimums(actual, expect.get("minimums", {})))
    errors.extend(_validate_list_contains(actual, expect.get("list_contains", [])))
    errors.extend(_validate_serialized_contains(actual, expect.get("serialized_contains", [])))
    return errors
