from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from tools.baselines.common import (
    CASE_FILE_ORDER,
    DEFAULT_BASELINE_EMAIL,
    DEFAULT_BASELINE_PASSWORD,
    DEFAULT_BASELINES_DIR,
    api_base_url,
    auth_headers,
    baseline_base_url,
    dump_json,
    emit,
    load_manifest,
    login_or_signup,
    login_with_retry,
    mcp_base_url,
    parse_http_response,
    read_jsonl,
    resolve_placeholders,
    validate_expectations,
)


async def execute_mcp_case(
    case: dict[str, Any], mcp_session: ClientSession | None
) -> dict[str, Any]:
    if mcp_session is None:
        raise ValueError("MCP session required for MCP baseline case")

    kind = case["kind"]
    if kind == "mcp_list_tools":
        return (await mcp_session.list_tools()).model_dump(mode="json", by_alias=True)
    if kind == "mcp_list_resources":
        return (await mcp_session.list_resources()).model_dump(mode="json", by_alias=True)
    if kind == "mcp_tool":
        return (
            await mcp_session.call_tool(
                case["tool"],
                case.get("arguments"),
            )
        ).model_dump(mode="json", by_alias=True)
    if kind == "mcp_resource":
        return (await mcp_session.read_resource(case["uri"])).model_dump(mode="json", by_alias=True)

    raise ValueError(f"Unsupported MCP baseline case kind: {kind}")


async def execute_case(
    case: dict[str, Any],
    *,
    api_client: httpx.AsyncClient,
    token: str | None,
    mcp_session: ClientSession | None,
    email: str,
    password: str,
) -> dict[str, Any]:
    kind = case["kind"]

    if kind == "auth_login":
        response = await login_with_retry(
            api_client,
            email=email,
            password=password,
        )
        return parse_http_response(response)

    if kind == "rest":
        if case.get("auth", "bearer") == "bearer":
            if token is None:
                raise ValueError("Bearer token required for authenticated REST baseline case")
            headers = auth_headers(token)
        else:
            headers = {}
        response = await api_client.request(
            case["method"],
            case["path"],
            headers=headers,
            json=case.get("json"),
        )
        return parse_http_response(response)

    if kind.startswith("mcp_"):
        return await execute_mcp_case(case, mcp_session)

    raise ValueError(f"Unsupported baseline case kind: {kind}")


async def replay_case(
    case: dict[str, Any],
    *,
    api_client: httpx.AsyncClient,
    token: str | None,
    mcp_session: ClientSession | None,
    email: str,
    password: str,
) -> dict[str, Any]:
    actual = await execute_case(
        case,
        api_client=api_client,
        token=token,
        mcp_session=mcp_session,
        email=email,
        password=password,
    )
    errors = validate_expectations(actual, case["expect"])
    if errors:
        formatted = "\n".join(f"- {error}" for error in errors)
        raise AssertionError(
            f"Baseline case {case['id']} failed:\n{formatted}\n\nActual:\n{dump_json(actual)}"
        )
    return actual


def apply_auth_expectation_overrides(case: dict[str, Any], *, email: str) -> dict[str, Any]:
    """Allow replaying auth cases with non-default baseline users."""

    if case.get("id") not in {"auth-login", "auth-me"}:
        return case

    expect = case.get("expect")
    if not isinstance(expect, dict):
        return case

    equals = expect.get("equals")
    if not isinstance(equals, dict) or "/body/user/email" not in equals:
        return case

    updated_equals = dict(equals)
    updated_equals["/body/user/email"] = email
    updated_expect = dict(expect)
    updated_expect["equals"] = updated_equals
    updated_case = dict(case)
    updated_case["expect"] = updated_expect
    return updated_case


async def replay_all(
    *,
    base_url: str,
    baselines_dir: Path,
    email: str,
    password: str,
    manifest_path: Path | None = None,
) -> None:
    api_url = api_base_url(base_url)
    mcp_url = mcp_base_url(base_url)
    manifest = load_manifest(manifest_path or (baselines_dir / "manifest.json"))

    async with AsyncExitStack() as stack:
        api_client = await stack.enter_async_context(
            httpx.AsyncClient(base_url=api_url, timeout=30.0)
        )
        token: str | None = None
        mcp_session: ClientSession | None = None

        async def ensure_token() -> str:
            nonlocal token
            if token is None:
                auth_payload = await login_or_signup(
                    api_client,
                    email=email,
                    password=password,
                )
                token = str(auth_payload["access_token"])
            return token

        async def ensure_mcp_session() -> ClientSession:
            nonlocal mcp_session
            if mcp_session is not None:
                return mcp_session

            current_token = await ensure_token()
            transport_client = await stack.enter_async_context(
                create_mcp_http_client(headers=auth_headers(current_token))
            )
            read_stream, write_stream = await stack.enter_async_context(
                streamable_http_client(mcp_url, http_client=transport_client)
            )
            mcp_session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await mcp_session.initialize()
            return mcp_session

        for filename in CASE_FILE_ORDER:
            path = baselines_dir / filename
            emit(f"Replaying {path.as_posix()}")
            for case in read_jsonl(path):
                resolved_case = apply_auth_expectation_overrides(
                    resolve_placeholders(case, manifest),
                    email=email,
                )
                case_kind = str(resolved_case["kind"])
                current_token: str | None = None
                current_mcp_session: ClientSession | None = None

                if case_kind == "auth_login":
                    current_token = token
                elif case_kind == "rest" and resolved_case.get("auth", "bearer") == "bearer":
                    current_token = await ensure_token()
                elif case_kind.startswith("mcp_"):
                    current_mcp_session = await ensure_mcp_session()
                    current_token = token

                actual = await replay_case(
                    resolved_case,
                    api_client=api_client,
                    token=current_token,
                    mcp_session=current_mcp_session,
                    email=email,
                    password=password,
                )
                if case_kind == "auth_login":
                    body = actual.get("body")
                    if isinstance(body, dict) and "access_token" in body:
                        token = str(body["access_token"])
                emit(f"  PASS {resolved_case['id']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the captured Sibyl baseline corpus.")
    parser.add_argument(
        "--base-url",
        default=baseline_base_url(),
        help="Base URL for the running Sibyl server.",
    )
    parser.add_argument(
        "--baselines-dir",
        type=Path,
        default=DEFAULT_BASELINES_DIR,
        help="Directory containing the generated baseline corpus files.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=None,
        help="Manifest with runtime fixture IDs. Defaults to <baselines-dir>/manifest.json.",
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
    return parser.parse_args()


async def amain() -> int:
    args = parse_args()
    await replay_all(
        base_url=args.base_url,
        baselines_dir=args.baselines_dir,
        email=args.email,
        password=args.password,
        manifest_path=args.manifest_path,
    )
    emit("Baseline replay passed.")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
