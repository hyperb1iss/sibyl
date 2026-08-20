#!/usr/bin/env python3
"""Run the focused release gate for source adapter ingest surfaces."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tools.trust.gate_runner import (
    Echo,
    GateCheck,
    Runner,
    stdout_echo,
)
from tools.trust.gate_runner import main as gate_main
from tools.trust.gate_runner import (
    missing_required_surfaces as find_missing_required_surfaces,
)
from tools.trust.gate_runner import run_gate as run_release_gate

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_NAME = "Adapter Ingest Gate"
MISSING_SURFACES_HEADING = "Adapter ingest gate is missing required surfaces:"
CLI_DESCRIPTION = "Run focused adapter ingest release-gate checks."

GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-source-adapter-contract",
        description="adapter contract, stable source identity, dedupe, and policy checks",
        surfaces=(
            "source adapter contract",
            "source-preserving ingest",
            "dedupe correctness",
            "private scope enforcement",
        ),
        command=("moon", "run", "core:adapter-ingest-contract-test"),
    ),
    GateCheck(
        name="core-mailbox-ingest",
        description="mailbox import checkpoints, resumability, private records, and dedupe",
        surfaces=(
            "import resumability",
            "source-preserving ingest",
            "dedupe correctness",
            "private scope enforcement",
        ),
        command=("moon", "run", "core:adapter-ingest-mailbox-test"),
    ),
    GateCheck(
        name="large-corpus-rehearsal",
        description="dogfood corpus volume, resumability, search, dedupe, and policy receipt",
        surfaces=(
            "large-corpus rehearsal",
            "source search before extraction",
            "import resumability",
            "dedupe correctness",
            "private scope enforcement",
            "source import receipts",
        ),
        command=("moon", "run", "large-corpus-rehearsal"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "source adapter contract",
    "source-preserving ingest",
    "import resumability",
    "dedupe correctness",
    "private scope enforcement",
    "large-corpus rehearsal",
    "source search before extraction",
    "source import receipts",
)


def missing_required_surfaces(
    checks: Sequence[GateCheck] = GATE_CHECKS,
) -> list[str]:
    return find_missing_required_surfaces(checks, REQUIRED_SURFACES)


def run_gate(
    checks: Sequence[GateCheck] = GATE_CHECKS,
    *,
    runner: Runner | None = None,
    echo: Echo = stdout_echo,
) -> int:
    return run_release_gate(
        gate_name=GATE_NAME,
        missing_surfaces_heading=MISSING_SURFACES_HEADING,
        checks=checks,
        required_surfaces=REQUIRED_SURFACES,
        repo_root=REPO_ROOT,
        runner=runner,
        echo=echo,
    )


def main(argv: list[str] | None = None) -> int:
    return gate_main(
        argv,
        parser_description=CLI_DESCRIPTION,
        gate_name=GATE_NAME,
        missing_surfaces_heading=MISSING_SURFACES_HEADING,
        checks=GATE_CHECKS,
        required_surfaces=REQUIRED_SURFACES,
        repo_root=REPO_ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
