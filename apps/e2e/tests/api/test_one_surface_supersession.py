"""Live proof that every ranked read surface shares one lifecycle contract."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from tests.conftest import API_BASE_URL, CLIRunner


def _result_ids(payload: dict[str, Any]) -> set[str]:
    results = payload.get("results", [])
    assert isinstance(results, list), payload
    return {str(item["id"]) for item in results if isinstance(item, dict) and "id" in item}


def _section_ids(payload: dict[str, Any]) -> set[str]:
    sections = payload.get("sections", [])
    assert isinstance(sections, list), payload
    served: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            continue
        for item in section.get("items", []):
            if not isinstance(item, dict):
                continue
            if "id" in item:
                served.add(str(item["id"]))
            for related in item.get("related", []):
                if isinstance(related, dict) and "id" in related:
                    served.add(str(related["id"]))
    return served


def _assert_current_only(
    *, surface: str, ids: set[str], current_id: str, retired_id: str
) -> None:
    assert current_id in ids, f"{surface} omitted the current successor: {sorted(ids)}"
    assert retired_id not in ids, f"{surface} revived the retired target: {sorted(ids)}"


async def _create_decision(
    client: httpx.AsyncClient,
    *,
    name: str,
    content: str,
    retrieval_key: str,
    project_id: str,
    related_to: list[str] | None = None,
) -> str:
    response = await client.post(
        "/entities",
        params={"sync": "true"},
        json={
            "name": name,
            "content": content,
            "entity_type": "decision",
            "metadata": {"memory_scope": "project", "project_id": project_id},
            "retrieval_keys": [retrieval_key],
            "related_to": related_to,
            "skip_conflicts": True,
            "defer_embeddings": True,
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


async def _create_project(client: httpx.AsyncClient, *, marker: str) -> str:
    response = await client.post(
        "/entities",
        params={"sync": "true"},
        json={
            "name": f"One Surface Project {marker}",
            "content": f"Isolated project for {marker}",
            "entity_type": "project",
            "metadata": {"memory_scope": "private"},
            "skip_conflicts": True,
            "defer_embeddings": True,
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


async def _context_pack(
    client: httpx.AsyncClient,
    *,
    marker: str,
    project_id: str,
    retrieval_mode: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "goal": marker,
        "project": project_id,
        "limit": 50,
        "include_related": False,
        "record_exposure": False,
    }
    if retrieval_mode is not None:
        body["evidence"] = {
            "retrieval_mode": retrieval_mode,
            "types": ["decision"],
            "limit": 10,
            "reserve_distilled_notes": False,
        }

    response = await client.post("/context/pack", json=body)
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


async def _mcp_search(*, api_key: str, marker: str, project_id: str) -> dict[str, Any]:
    mcp_url = f"{API_BASE_URL.removesuffix('/api')}/mcp"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with (
        create_mcp_http_client(headers=headers) as transport_client,
        streamable_http_client(mcp_url, http_client=transport_client) as streams,
    ):
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                "search",
                {
                    "query": marker,
                    "types": ["decision"],
                    "project": project_id,
                    "limit": 10,
                    "include_documents": False,
                    "include_graph": True,
                    "boost_recent": False,
                },
            )

    payload = result.model_dump(mode="json", by_alias=True)
    assert payload.get("isError", payload.get("is_error", False)) is False, payload
    structured = payload.get("structuredContent") or payload.get("structured_content")
    assert isinstance(structured, dict), payload
    return structured


@pytest.mark.api
@pytest.mark.cli
@pytest.mark.asyncio
async def test_one_supersession_fixture_governs_all_six_public_observations(
    auth_api_client: httpx.AsyncClient,
    cli: CLIRunner,
    unique_id: str,
) -> None:
    """A retired target stays absent from every public ranked-read observation."""

    retrieval_key = f"one_surface_{unique_id.removeprefix('e2e-').replace('-', '')}"
    marker = f"quartz marmalade observatory {retrieval_key}"
    created_ids: list[str] = []
    api_key_id: str | None = None

    try:
        project_id = await _create_project(auth_api_client, marker=retrieval_key)
        created_ids.append(project_id)
        retired_id = await _create_decision(
            auth_api_client,
            name=f"Retired {marker}",
            content=f"{marker} retired deployment decision",
            retrieval_key=retrieval_key,
            project_id=project_id,
        )
        created_ids.append(retired_id)
        current_id = await _create_decision(
            auth_api_client,
            name=f"Current {marker}",
            content=f"{marker} current deployment decision",
            retrieval_key=retrieval_key,
            project_id=project_id,
            related_to=[f"supersedes:{retired_id}"],
        )
        created_ids.append(current_id)
        control_id = await _create_decision(
            auth_api_client,
            name=f"Control {marker}",
            content=f"{marker} independent current decision",
            retrieval_key=f"{retrieval_key}_control",
            project_id=project_id,
        )
        created_ids.append(control_id)

        sections_pack = await _context_pack(
            auth_api_client,
            marker=marker,
            project_id=project_id,
        )
        _assert_current_only(
            surface="context-pack sections",
            ids=_section_ids(sections_pack),
            current_id=current_id,
            retired_id=retired_id,
        )

        for retrieval_mode in ("fast", "naive"):
            evidence_pack = await _context_pack(
                auth_api_client,
                marker=marker,
                project_id=project_id,
                retrieval_mode=retrieval_mode,
            )
            evidence = evidence_pack.get("evidence")
            assert isinstance(evidence, dict), evidence_pack
            _assert_current_only(
                surface=f"context-pack {retrieval_mode} evidence",
                ids=_result_ids(evidence),
                current_id=current_id,
                retired_id=retired_id,
            )

        rest_response = await auth_api_client.post(
            "/search",
            json={
                "query": marker,
                "types": ["decision"],
                "project": project_id,
                "limit": 10,
                "include_documents": False,
                "include_raw_memory": False,
                "record_exposure": False,
                "boost_recent": False,
            },
        )
        rest_response.raise_for_status()
        rest_payload = rest_response.json()
        assert isinstance(rest_payload, dict)
        _assert_current_only(
            surface="REST search",
            ids=_result_ids(rest_payload),
            current_id=current_id,
            retired_id=retired_id,
        )

        cli_result = cli.run(
            "context",
            marker,
            "--project",
            project_id,
            "--limit",
            "50",
            "--json",
        )
        cli_output = cli_result.stdout or cli_result.stderr
        assert cli_result.success, cli_output
        assert cli_result.is_json, cli_output
        cli_payload = cli_result.json()
        assert isinstance(cli_payload, dict)
        cli_ids = _section_ids(cli_payload)
        assert control_id in cli_ids, "CLI context did not leave a control slot for comparison"
        assert int(cli_payload.get("total_items", 50)) < 50, "CLI context saturated its item budget"
        _assert_current_only(
            surface="CLI context",
            ids=cli_ids,
            current_id=current_id,
            retired_id=retired_id,
        )

        key_response = await auth_api_client.post(
            "/auth/api-keys",
            json={
                "name": marker,
                "live": False,
                "scopes": ["mcp"],
                "project_ids": [project_id],
            },
        )
        key_response.raise_for_status()
        key_payload = key_response.json()
        api_key_id = str(key_payload["id"])
        mcp_payload = await _mcp_search(
            api_key=str(key_payload["api_key"]),
            marker=marker,
            project_id=project_id,
        )
        _assert_current_only(
            surface="MCP search",
            ids=_result_ids(mcp_payload),
            current_id=current_id,
            retired_id=retired_id,
        )
    finally:
        if api_key_id is not None:
            with suppress(httpx.HTTPError):
                response = await auth_api_client.post(f"/auth/api-keys/{api_key_id}/revoke")
                response.raise_for_status()
        for entity_id in reversed(created_ids):
            with suppress(httpx.HTTPError):
                response = await auth_api_client.delete(f"/entities/{entity_id}")
                response.raise_for_status()
