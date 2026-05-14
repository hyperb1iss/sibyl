from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import httpx

from tools.baselines.common import (
    BASELINE_TAGS,
    CASE_FILE_ORDER,
    DEFAULT_BASELINE_EMAIL,
    DEFAULT_BASELINE_PASSWORD,
    DEFAULT_BASELINES_DIR,
    MCP_ADD_CONTENT,
    MCP_ADD_TITLE,
    REST_SEED_TITLE,
    api_base_url,
    baseline_base_url,
    dump_json,
    emit,
    ensure_graph_fixture,
    ensure_rest_seed,
    graph_ref,
    login_or_signup,
    write_jsonl,
    write_manifest,
)
from tools.baselines.replay import replay_all


def build_auth_cases(email: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "auth-login",
            "kind": "auth_login",
            "description": "Local auth login returns tokens and org context.",
            "expect": {
                "equals": {
                    "/status_code": 200,
                    "/body/user/email": email,
                },
                "required": [
                    "/body/access_token",
                    "/body/refresh_token",
                    "/body/organization/id",
                    "/body/user/id",
                ],
            },
        },
        {
            "id": "auth-me",
            "kind": "rest",
            "auth": "bearer",
            "method": "GET",
            "path": "/auth/me",
            "expect": {
                "equals": {
                    "/status_code": 200,
                    "/body/user/email": email,
                },
                "required": [
                    "/body/organization/id",
                    "/body/org_role",
                    "/body/user/id",
                ],
            },
        },
    ]


def build_rest_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "rest-health",
            "kind": "rest",
            "auth": "none",
            "method": "GET",
            "path": "/health",
            "expect": {
                "equals": {
                    "/status_code": 200,
                    "/body/status": "healthy",
                },
                "required": ["/body/version"],
            },
        },
        {
            "id": "rest-graph-stats",
            "kind": "rest",
            "auth": "bearer",
            "method": "GET",
            "path": "/graph/stats",
            "expect": {
                "equals": {"/status_code": 200},
                "required": ["/body/by_type", "/body/total_nodes", "/body/total_edges"],
                "minimums": {"/body/total_nodes": 1},
            },
        },
    ]


def build_graph_cases(graph_fixture: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": "rest-entity-task-a",
            "kind": "rest",
            "auth": "bearer",
            "method": "GET",
            "path": f"/entities/{graph_ref('task_a')}",
            "expect": {
                "equals": {
                    "/status_code": 200,
                    "/body/id": graph_ref("task_a"),
                    "/body/name": graph_fixture["task_a"]["name"],
                    "/body/entity_type": "task",
                },
                "list_contains": [
                    {
                        "pointer": "/body/related",
                        "match": {
                            "id": graph_ref("task_b"),
                            "name": graph_fixture["task_b"]["name"],
                            "relationship": "DEPENDS_ON",
                            "direction": "incoming",
                        },
                    },
                    {
                        "pointer": "/body/related",
                        "match": {
                            "id": graph_ref("epic"),
                            "name": graph_fixture["epic"]["name"],
                            "relationship": "BELONGS_TO",
                            "direction": "outgoing",
                        },
                    },
                ],
            },
        },
        {
            "id": "rest-entity-task-b",
            "kind": "rest",
            "auth": "bearer",
            "method": "GET",
            "path": f"/entities/{graph_ref('task_b')}",
            "expect": {
                "equals": {
                    "/status_code": 200,
                    "/body/id": graph_ref("task_b"),
                    "/body/name": graph_fixture["task_b"]["name"],
                    "/body/entity_type": "task",
                },
                "list_contains": [
                    {
                        "pointer": "/body/related",
                        "match": {
                            "id": graph_ref("task_a"),
                            "name": graph_fixture["task_a"]["name"],
                            "relationship": "DEPENDS_ON",
                            "direction": "outgoing",
                        },
                    },
                    {
                        "pointer": "/body/related",
                        "match": {
                            "id": graph_ref("project"),
                            "name": graph_fixture["project"]["name"],
                            "relationship": "BELONGS_TO",
                            "direction": "outgoing",
                        },
                    },
                ],
            },
        },
        {
            "id": "rest-graph-full",
            "kind": "rest",
            "auth": "bearer",
            "method": "GET",
            "path": "/graph/full",
            "expect": {
                "equals": {"/status_code": 200},
                "minimums": {
                    "/body/node_count": 4,
                    "/body/edge_count": 4,
                },
                "list_contains": [
                    {
                        "pointer": "/body/nodes",
                        "match": {
                            "id": graph_ref("project"),
                            "label": graph_fixture["project"]["name"],
                        },
                    },
                    {
                        "pointer": "/body/nodes",
                        "match": {
                            "id": graph_ref("epic"),
                            "label": graph_fixture["epic"]["name"],
                        },
                    },
                    {
                        "pointer": "/body/nodes",
                        "match": {
                            "id": graph_ref("task_a"),
                            "label": graph_fixture["task_a"]["name"],
                        },
                    },
                    {
                        "pointer": "/body/nodes",
                        "match": {
                            "id": graph_ref("task_b"),
                            "label": graph_fixture["task_b"]["name"],
                        },
                    },
                    {
                        "pointer": "/body/edges",
                        "match": {
                            "source": graph_ref("task_b"),
                            "target": graph_ref("task_a"),
                            "type": "DEPENDS_ON",
                        },
                    },
                    {
                        "pointer": "/body/edges",
                        "match": {
                            "source": graph_ref("task_b"),
                            "target": graph_ref("project"),
                            "type": "BELONGS_TO",
                        },
                    },
                ],
            },
        },
    ]


def build_search_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "search-rest-baseline-episode",
            "kind": "rest",
            "auth": "bearer",
            "method": "POST",
            "path": "/search",
            "json": {"query": REST_SEED_TITLE, "limit": 5},
            "expect": {
                "equals": {
                    "/status_code": 200,
                    "/body/query": REST_SEED_TITLE,
                },
                "minimums": {"/body/total": 1},
                "list_contains": [
                    {"pointer": "/body/results", "match": {"name": REST_SEED_TITLE}},
                ],
            },
        },
        {
            "id": "search-graph-fixture-task-b",
            "kind": "rest",
            "auth": "bearer",
            "method": "POST",
            "path": "/search",
            "json": {"query": "Silver Delta", "limit": 5},
            "expect": {
                "equals": {
                    "/status_code": 200,
                    "/body/query": "Silver Delta",
                },
                "minimums": {"/body/total": 1},
                "list_contains": [
                    {"pointer": "/body/results", "match": {"name": "Silver Delta"}},
                ],
            },
        },
    ]


def build_mcp_cases(graph_fixture: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": "mcp-list-tools",
            "kind": "mcp_list_tools",
            "expect": {
                "list_contains": [
                    {"pointer": "/tools", "match": {"name": "search"}},
                    {"pointer": "/tools", "match": {"name": "explore"}},
                    {"pointer": "/tools", "match": {"name": "add"}},
                    {"pointer": "/tools", "match": {"name": "manage"}},
                    {"pointer": "/tools", "match": {"name": "logs"}},
                ]
            },
        },
        {
            "id": "mcp-list-resources",
            "kind": "mcp_list_resources",
            "expect": {
                "list_contains": [
                    {"pointer": "/resources", "match": {"uri": "sibyl://health"}},
                    {"pointer": "/resources", "match": {"uri": "sibyl://stats"}},
                ]
            },
        },
        {
            "id": "mcp-search",
            "kind": "mcp_tool",
            "tool": "search",
            "arguments": {"query": REST_SEED_TITLE, "limit": 5},
            "expect": {
                "equals": {
                    "/isError": False,
                    "/structuredContent/query": REST_SEED_TITLE,
                },
                "list_contains": [
                    {"pointer": "/structuredContent/results", "match": {"name": REST_SEED_TITLE}},
                ],
            },
        },
        {
            "id": "mcp-explore",
            "kind": "mcp_tool",
            "tool": "explore",
            "arguments": {"mode": "list", "types": ["episode"], "limit": 5},
            "expect": {
                "equals": {"/isError": False, "/structuredContent/mode": "list"},
                "list_contains": [
                    {
                        "pointer": "/structuredContent/entities",
                        "match": {"name": REST_SEED_TITLE},
                    }
                ],
            },
        },
        {
            "id": "mcp-explore-related",
            "kind": "mcp_tool",
            "tool": "explore",
            "arguments": {
                "mode": "related",
                "entity_id": graph_ref("task_b"),
                "relationship_types": ["DEPENDS_ON", "BELONGS_TO"],
                "limit": 10,
            },
            "expect": {
                "equals": {"/isError": False, "/structuredContent/mode": "related"},
                "list_contains": [
                    {
                        "pointer": "/structuredContent/entities",
                        "match": {
                            "id": graph_ref("task_a"),
                            "name": graph_fixture["task_a"]["name"],
                            "relationship": "DEPENDS_ON",
                            "direction": "outgoing",
                        },
                    },
                    {
                        "pointer": "/structuredContent/entities",
                        "match": {
                            "id": graph_ref("project"),
                            "name": graph_fixture["project"]["name"],
                            "relationship": "BELONGS_TO",
                            "direction": "outgoing",
                        },
                    },
                ],
            },
        },
        {
            "id": "mcp-add",
            "kind": "mcp_tool",
            "tool": "add",
            "arguments": {
                "title": MCP_ADD_TITLE,
                "content": MCP_ADD_CONTENT,
                "entity_type": "pattern",
                "category": "baseline",
                "project": graph_ref("project"),
                "tags": [*BASELINE_TAGS, "mcp-smoke"],
            },
            "expect": {
                "equals": {"/isError": False, "/structuredContent/success": True},
                "required": ["/structuredContent/id"],
                "serialized_contains": ["Queued:"],
            },
        },
        {
            "id": "mcp-manage-link-graph-status",
            "kind": "mcp_tool",
            "tool": "manage",
            "arguments": {"action": "link_graph_status"},
            "expect": {
                "equals": {
                    "/isError": False,
                    "/structuredContent/success": True,
                },
                "required": ["/structuredContent/data/chunks_pending"],
            },
        },
        {
            "id": "mcp-logs",
            "kind": "mcp_tool",
            "tool": "logs",
            "arguments": {"limit": 2},
            "expect": {
                "equals": {"/isError": False},
                "minimums": {"/structuredContent/result": 1},
                "list_contains": [
                    {"pointer": "/structuredContent/result", "match": {"service": "api"}},
                ],
            },
        },
        {
            "id": "mcp-resource-health",
            "kind": "mcp_resource",
            "uri": "sibyl://health",
            "expect": {
                "serialized_contains": ["healthy", "graph_connected", "entity_counts"],
            },
        },
        {
            "id": "mcp-resource-stats",
            "kind": "mcp_resource",
            "uri": "sibyl://stats",
            "expect": {
                "serialized_contains": ["entity_counts", "total_entities"],
            },
        },
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture the Sibyl legacy baseline corpus.")
    parser.add_argument(
        "--base-url",
        default=baseline_base_url(),
        help="Base URL for the running Sibyl server.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_BASELINES_DIR,
        help="Directory where corpus files should be written.",
    )
    parser.add_argument(
        "--email",
        default=DEFAULT_BASELINE_EMAIL,
        help="Baseline user email.",
    )
    parser.add_argument(
        "--password",
        default=DEFAULT_BASELINE_PASSWORD,
        help="Baseline user password.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Write the corpus without replaying it immediately.",
    )
    return parser.parse_args()


async def amain() -> int:
    args = parse_args()
    api_url = api_base_url(args.base_url)
    output_dir = args.output_dir

    async with httpx.AsyncClient(base_url=api_url, timeout=30.0) as api_client:
        auth_payload = await login_or_signup(
            api_client,
            email=args.email,
            password=args.password,
        )
        token = str(auth_payload["access_token"])
        rest_seed = await ensure_rest_seed(api_client, token)
        graph_fixture = await ensure_graph_fixture(api_client, token)

    write_jsonl(output_dir / "auth_smoke.jsonl", build_auth_cases(args.email))
    write_jsonl(output_dir / "rest_smoke.jsonl", build_rest_cases())
    write_jsonl(output_dir / "graph_smoke.jsonl", build_graph_cases(graph_fixture))
    write_jsonl(output_dir / "search_queries.jsonl", build_search_cases())
    write_jsonl(output_dir / "mcp_smoke.jsonl", build_mcp_cases(graph_fixture))
    write_manifest(
        output_dir / "manifest.json",
        base_url=args.base_url,
        email=args.email,
        rest_seed=rest_seed,
        graph_fixture=graph_fixture,
    )

    emit(f"Wrote baseline corpus to {output_dir.as_posix()}")
    emit(
        dump_json(
            {
                "files": list(CASE_FILE_ORDER),
                "graph_fixture": {
                    name: {"id": entity["id"], "name": entity["name"]}
                    for name, entity in graph_fixture.items()
                },
                "rest_seed": rest_seed,
            }
        )
    )

    if not args.skip_verify:
        await replay_all(
            base_url=args.base_url,
            baselines_dir=output_dir,
            email=args.email,
            password=args.password,
            manifest_path=output_dir / "manifest.json",
        )

    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
