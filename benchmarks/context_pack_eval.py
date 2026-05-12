#!/usr/bin/env python3
# ruff: noqa: E402
"""Evaluate the live Sibyl context-pack endpoint against fixture cases."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "python" / "sibyl-core" / "src"))
sys.path.insert(0, str(ROOT / "apps" / "cli" / "src"))

from sibyl_core.evals import ContextPackEvalReport, EvalConfig, run_context_pack_evaluation_cli


async def _get_client_headers(api_url: str, timeout: float) -> dict[str, str]:
    try:
        from sibyl_cli.client import SibylClient  # noqa: PLC0415

        client = SibylClient(base_url=api_url, timeout=timeout)
        if client.auth_token:
            await client._refresh_token()
        return client._default_headers()
    except Exception:
        return {"Content-Type": "application/json"}


def _parse_metadata(values: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            msg = f"Invalid metadata entry: {item!r}. Expected key=value."
            raise argparse.ArgumentTypeError(msg)
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            msg = f"Invalid metadata entry: {item!r}. Key cannot be empty."
            raise argparse.ArgumentTypeError(msg)
        metadata[key] = value
    return metadata


async def _run(args: argparse.Namespace, cases_file: Path | None) -> ContextPackEvalReport:
    config = EvalConfig(
        api_base_url=args.api_url,
        headers=await _get_client_headers(args.api_url, args.timeout),
        output_dir=args.output_dir,
        save_results=not args.no_save,
        label=args.label,
        metadata=_parse_metadata(args.metadata),
        timeout_seconds=args.timeout,
    )
    return await run_context_pack_evaluation_cli(cases_file=cases_file, config=config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate live Sibyl context packs.")
    parser.add_argument(
        "cases",
        nargs="?",
        type=Path,
        help="Path to a JSON file with context-pack eval cases. Uses a smoke case by default.",
    )
    parser.add_argument(
        "--cases",
        dest="cases_option",
        type=Path,
        help="Path to a JSON file with context-pack eval cases.",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:3334/api",
        help="Base Sibyl API URL.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".moon" / "cache" / "evals",
        help="Directory for saved evaluation reports.",
    )
    parser.add_argument(
        "--label",
        help="Optional label to embed in saved reports and filenames.",
    )
    parser.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra metadata to store in the saved report. Repeat as needed.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Print the report summary without writing a JSON artifact.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )
    args = parser.parse_args()
    if args.cases and args.cases_option:
        parser.error("Pass cases either positionally or with --cases, not both.")
    cases_file = args.cases_option or args.cases

    report = asyncio.run(_run(args, cases_file))
    if not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
