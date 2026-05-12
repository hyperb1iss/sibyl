from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx

from tools.baselines.common import (
    DEFAULT_BASELINE_EMAIL,
    DEFAULT_BASELINE_PASSWORD,
    api_base_url,
    baseline_base_url,
    dump_json,
    emit,
    ensure_graph_fixture,
    ensure_raw_memory_fixture,
    ensure_rest_seed,
    login_or_signup,
    write_manifest,
)

DEFAULT_RUNTIME_MANIFEST = Path(".moon/cache/baseline-runtime-manifest.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the baseline corpus fixture on a running Sibyl runtime."
    )
    parser.add_argument(
        "--base-url",
        default=baseline_base_url(),
        help="Base URL for the running Sibyl server.",
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
        "--manifest-path",
        type=Path,
        default=DEFAULT_RUNTIME_MANIFEST,
        help="Where to write the runtime-specific manifest.",
    )
    return parser.parse_args()


async def amain() -> int:
    args = parse_args()

    async with httpx.AsyncClient(base_url=api_base_url(args.base_url), timeout=30.0) as api_client:
        auth_payload = await login_or_signup(
            api_client,
            email=args.email,
            password=args.password,
        )
        token = str(auth_payload["access_token"])
        rest_seed = await ensure_rest_seed(api_client, token)
        graph_fixture = await ensure_graph_fixture(api_client, token)
        raw_memory_fixture = await ensure_raw_memory_fixture(api_client, token)

    write_manifest(
        args.manifest_path,
        base_url=args.base_url,
        email=args.email,
        rest_seed=rest_seed,
        graph_fixture=graph_fixture,
        raw_memory_fixture=raw_memory_fixture,
        access_token=token,
    )

    emit(f"Wrote runtime baseline manifest to {args.manifest_path.as_posix()}")
    emit(
        dump_json(
            {
                "manifest_path": args.manifest_path.as_posix(),
                "graph_fixture": {
                    name: {"id": entity["id"], "name": entity["name"]}
                    for name, entity in graph_fixture.items()
                },
                "rest_seed": rest_seed,
                "raw_memory_fixture": {
                    name: {"id": memory["id"], "title": memory["title"]}
                    for name, memory in raw_memory_fixture.items()
                },
            }
        )
    )
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
