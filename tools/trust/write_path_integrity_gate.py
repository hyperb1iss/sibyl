#!/usr/bin/env python3
"""Run the focused release gate for write-path integrity."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import which
from types import SimpleNamespace
from typing import Any

from sibyl.jobs import memory_extraction
from sibyl_core.models.memory_extraction import (
    ExtractedMemoryEntity,
    MemoryBatchEntityExtractionResult,
    SourceMemoryExtraction,
)
from sibyl_core.services import surreal_content as content_service

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_SCHEMA_VERSION = "sibyl-write-path-integrity-receipt-v1"
DEFAULT_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "write-path-integrity-receipt.json"
)

Runner = Callable[[tuple[str, ...]], int]
Echo = Callable[[str], None]

ZERO_BUDGETS = {
    "hallucinated_fact_count": 0,
    "self_referential_write_count": 0,
    "low_signal_write_count": 0,
}

SELF_FEEDING_SURFACES = frozenset(
    {
        "reflection",
        "reflection_candidate",
        "reflection_source",
        "synthesis_artifact",
    }
)


@dataclass(frozen=True)
class GateCheck:
    name: str
    description: str
    surfaces: tuple[str, ...]
    command: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    check: GateCheck
    exit_code: int
    elapsed_seconds: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class SeededEntity:
    name: str
    summary: str
    evidence: str
    entity_type: str = "tool"


@dataclass(frozen=True)
class SeededExtractionCase:
    source_id: str
    content: str
    forbidden_terms: tuple[str, ...]
    entities: tuple[SeededEntity, ...]


@dataclass(frozen=True)
class DreamSourceCase:
    source_id: str
    capture_surface: str
    selected_for_reflection: bool


@dataclass(frozen=True)
class LowSignalCase:
    source_id: str
    no_op_sources: int
    extracted_entities: int
    projected_entities: int
    content: str = "ok thanks"
    max_source_chars: int = 200


GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-write-path-integrity",
        description="raw source selector excludes self-feeding reflection and synthesis outputs",
        surfaces=("self-feeding guard", "dream-cycle source selection"),
        command=("moon", "run", "core:write-path-integrity-test"),
    ),
    GateCheck(
        name="api-write-path-integrity",
        description="extraction, reflection, and consolidation job receipts stay bounded",
        surfaces=("extraction", "low-signal no-op", "reflection", "consolidation"),
        command=("moon", "run", "api:write-path-integrity-test"),
    ),
    GateCheck(
        name="ai-memory-contracts",
        description="committed AI-memory manifest carries W4 zero-budget contracts",
        surfaces=("manifest", "release contract"),
        command=("moon", "run", "bench-gate"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "extraction",
    "low-signal no-op",
    "self-feeding guard",
    "dream-cycle source selection",
    "reflection",
    "consolidation",
    "manifest",
    "release contract",
)

DEFAULT_EXTRACTION_CASES: tuple[SeededExtractionCase, ...] = (
    SeededExtractionCase(
        source_id="seed-source-grounded",
        content=(
            "Sibyl stores raw memory before graph projection. SurrealDB native RRF powers "
            "graph retrieval for the Lumen route."
        ),
        forbidden_terms=("mercury route", "aurora db", "graphiti write path"),
        entities=(
            SeededEntity(
                name="SurrealDB native RRF",
                summary="SurrealDB native RRF powers graph retrieval for the Lumen route.",
                evidence="SurrealDB native RRF powers graph retrieval for the Lumen route.",
            ),
        ),
    ),
)

DEFAULT_DREAM_SOURCE_CASES: tuple[DreamSourceCase, ...] = (
    DreamSourceCase("cli-session", "cli", True),
    DreamSourceCase("reflection-result", "reflection", False),
    DreamSourceCase("reflection-review", "reflection_candidate", False),
    DreamSourceCase("reflection-source", "reflection_source", False),
    DreamSourceCase("synthesis-output", "synthesis_artifact", False),
)

DEFAULT_LOW_SIGNAL_CASES: tuple[LowSignalCase, ...] = (
    LowSignalCase(
        source_id="low-signal-ack",
        no_op_sources=1,
        extracted_entities=0,
        projected_entities=0,
    ),
)


def covered_surfaces(checks: Iterable[GateCheck] = GATE_CHECKS) -> set[str]:
    return {surface for check in checks for surface in check.surfaces}


def missing_required_surfaces(checks: Sequence[GateCheck] = GATE_CHECKS) -> list[str]:
    covered = covered_surfaces(checks)
    return [surface for surface in REQUIRED_SURFACES if surface not in covered]


def build_write_path_integrity_receipt(
    *,
    extraction_cases: Sequence[SeededExtractionCase] = DEFAULT_EXTRACTION_CASES,
    dream_source_cases: Sequence[DreamSourceCase] = DEFAULT_DREAM_SOURCE_CASES,
    low_signal_cases: Sequence[LowSignalCase] = DEFAULT_LOW_SIGNAL_CASES,
) -> dict[str, Any]:
    metrics = {
        "hallucinated_fact_count": _hallucinated_fact_count(extraction_cases),
        "self_referential_write_count": _self_referential_write_count(dream_source_cases),
        "low_signal_write_count": _low_signal_write_count(low_signal_cases),
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "fixture": "observed-write-path-integrity-v1",
        "budgets": dict(ZERO_BUDGETS),
        "metrics": metrics,
        "cases": {
            "extraction": len(extraction_cases),
            "dream_sources": len(dream_source_cases),
            "low_signal": len(low_signal_cases),
        },
        "observations": {
            "extraction": [
                {
                    "source_id": case.source_id,
                    "entities": [
                        {
                            "name": entity.name,
                            "summary": entity.summary,
                            "evidence": entity.evidence,
                            "entity_type": entity.entity_type,
                        }
                        for entity in case.entities
                    ],
                }
                for case in extraction_cases
            ],
            "dream_sources": [
                {
                    "source_id": case.source_id,
                    "capture_surface": case.capture_surface,
                    "selected_for_reflection": case.selected_for_reflection,
                }
                for case in dream_source_cases
            ],
            "low_signal": [
                {
                    "source_id": case.source_id,
                    "no_op_sources": case.no_op_sources,
                    "extracted_entities": case.extracted_entities,
                    "projected_entities": case.projected_entities,
                }
                for case in low_signal_cases
            ],
        },
    }


def validate_write_path_integrity_receipt(receipt: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        failures.append(f"receipt schema_version must be {RECEIPT_SCHEMA_VERSION}")
    metrics = receipt.get("metrics")
    if not isinstance(metrics, dict):
        return [*failures, "receipt metrics must be an object"]
    for metric, budget in ZERO_BUDGETS.items():
        value = metrics.get(metric)
        if not isinstance(value, int) or isinstance(value, bool):
            failures.append(f"metric {metric!r} must be an integer")
        elif value > budget:
            failures.append(f"metric {metric!r} exceeds budget {budget}: {value}")
    checks = receipt.get("checks")
    if checks is not None:
        if not isinstance(checks, list):
            failures.append("receipt checks must be a list")
        else:
            for index, check in enumerate(checks):
                if not isinstance(check, dict):
                    failures.append(f"receipt checks[{index}] must be an object")
                    continue
                if check.get("status") != "PASS":
                    failures.append(f"receipt checks[{index}] did not pass")
    return failures


async def collect_write_path_observations() -> dict[str, Any]:
    extraction_cases = await _observe_extraction_cases(DEFAULT_EXTRACTION_CASES)
    dream_source_cases = await _observe_dream_source_cases(DEFAULT_DREAM_SOURCE_CASES)
    low_signal_cases = await _observe_low_signal_cases(DEFAULT_LOW_SIGNAL_CASES)
    return build_write_path_integrity_receipt(
        extraction_cases=extraction_cases,
        dream_source_cases=dream_source_cases,
        low_signal_cases=low_signal_cases,
    )


def build_observed_write_path_receipt() -> dict[str, Any]:
    return asyncio.run(collect_write_path_observations())


async def _observe_extraction_cases(
    cases: Sequence[SeededExtractionCase],
) -> tuple[SeededExtractionCase, ...]:
    if not cases:
        return ()

    class ProbeExtractor:
        async def extract_many(
            self,
            prompts: list[str],
            *,
            max_concurrent: int,
        ) -> list[MemoryBatchEntityExtractionResult]:
            del prompts, max_concurrent
            return [
                MemoryBatchEntityExtractionResult(
                    sources=[
                        SourceMemoryExtraction(
                            source_id=case.source_id,
                            entities=[
                                ExtractedMemoryEntity(
                                    name=entity.name,
                                    entity_type=entity.entity_type,
                                    summary=entity.summary,
                                    evidence=entity.evidence,
                                    confidence=0.99,
                                )
                                for entity in case.entities
                            ],
                        )
                        for case in cases
                    ]
                )
            ]

    class ProbeEntityManager:
        async def create_direct_bulk(self, entities: Sequence[Any], **_: Any) -> list[str]:
            return [str(entity.id) for entity in entities]

    class ProbeRelationshipManager:
        async def create_bulk(self, relationships: Sequence[Any]) -> tuple[int, int]:
            return (len(relationships), 0)

    async def fake_runtime(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(
            entity_manager=ProbeEntityManager(),
            relationship_manager=ProbeRelationshipManager(),
        )

    @asynccontextmanager
    async def fake_content_session():
        yield None

    old_extractor = memory_extraction.memory_batch_entity_extractor
    old_runtime = memory_extraction.get_surreal_graph_runtime
    old_provider = memory_extraction.configured_embedding_provider
    old_content_session = memory_extraction.get_content_read_session
    try:
        memory_extraction.memory_batch_entity_extractor = lambda **_: ProbeExtractor()
        memory_extraction.get_surreal_graph_runtime = fake_runtime
        memory_extraction.configured_embedding_provider = lambda: None
        memory_extraction.get_content_read_session = fake_content_session
        result = await memory_extraction.extract_memory_entities(
            {},
            [
                {
                    "id": case.source_id,
                    "entity_type": "session",
                    "name": case.source_id,
                    "content": case.content,
                }
                for case in cases
            ],
            "org-write-path-integrity",
            created_source_ids=[case.source_id for case in cases],
            max_entities_per_source=8,
            max_source_chars=12_000,
            max_concurrent=2,
            max_tokens=1024,
        )
    finally:
        memory_extraction.memory_batch_entity_extractor = old_extractor
        memory_extraction.get_surreal_graph_runtime = old_runtime
        memory_extraction.configured_embedding_provider = old_provider
        memory_extraction.get_content_read_session = old_content_session

    extractions_by_source_id = {
        str(extraction.get("source_id")): extraction
        for extraction in result.get("extractions", [])
        if isinstance(extraction, dict)
    }
    observed: list[SeededExtractionCase] = []
    for case in cases:
        extraction = extractions_by_source_id.get(case.source_id, {})
        raw_entities = extraction.get("entities", []) if isinstance(extraction, dict) else []
        entities: list[SeededEntity] = []
        if isinstance(raw_entities, list):
            for raw_entity in raw_entities:
                if not isinstance(raw_entity, dict):
                    continue
                entities.append(
                    SeededEntity(
                        name=str(raw_entity.get("name") or ""),
                        entity_type=str(raw_entity.get("entity_type") or "topic"),
                        summary=str(raw_entity.get("summary") or ""),
                        evidence=str(raw_entity.get("evidence") or ""),
                    )
                )
        observed.append(
            SeededExtractionCase(
                source_id=case.source_id,
                content=case.content,
                forbidden_terms=case.forbidden_terms,
                entities=tuple(entities),
            )
        )
    return tuple(observed)


async def _observe_dream_source_cases(
    cases: Sequence[DreamSourceCase],
) -> tuple[DreamSourceCase, ...]:
    if not cases:
        return ()

    captured_at = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
    rows = [
        {
            "uuid": case.source_id,
            "organization_id": "org-write-path-integrity",
            "source_id": f"{case.source_id}:source",
            "principal_id": "user-bliss",
            "memory_scope": "private",
            "review_state": "pending",
            "entity_type": "raw_memory",
            "title": case.source_id,
            "raw_content": "A durable source memory with enough signal.",
            "metadata": {},
            "capture_surface": case.capture_surface,
            "captured_at": captured_at,
            "created_at": captured_at,
        }
        for case in cases
    ]

    class ProbeClient:
        async def execute_query(
            self,
            query: str,
            params: dict[str, object] | None = None,
            **kwargs: object,
        ) -> object:
            del query, params, kwargs
            return [{"status": "OK", "result": rows}]

    @asynccontextmanager
    async def fake_content_client():
        yield ProbeClient()

    old_content_client = content_service.surreal_content_client
    try:
        content_service.surreal_content_client = fake_content_client
        selected = await content_service.list_reflection_dream_source_memories(
            organization_id="org-write-path-integrity",
            limit=len(cases),
        )
    finally:
        content_service.surreal_content_client = old_content_client

    selected_ids = {memory.id for memory in selected}
    return tuple(
        DreamSourceCase(
            source_id=case.source_id,
            capture_surface=case.capture_surface,
            selected_for_reflection=case.source_id in selected_ids,
        )
        for case in cases
    )


async def _observe_low_signal_cases(
    cases: Sequence[LowSignalCase],
) -> tuple[LowSignalCase, ...]:
    if not cases:
        return ()

    async def fail_runtime(*_: object, **__: object) -> None:
        msg = "low-signal probe must not project"
        raise AssertionError(msg)

    old_extractor = memory_extraction.memory_batch_entity_extractor
    old_runtime = memory_extraction.get_surreal_graph_runtime
    try:
        memory_extraction.memory_batch_entity_extractor = lambda **_: (
            _raise_low_signal_probe_error()
        )
        memory_extraction.get_surreal_graph_runtime = fail_runtime
        observed: list[LowSignalCase] = []
        for case in cases:
            result = await memory_extraction.extract_memory_entities(
                {},
                [
                    {
                        "id": case.source_id,
                        "entity_type": "session",
                        "name": case.source_id,
                        "content": case.content,
                    }
                ],
                "org-write-path-integrity",
                created_source_ids=[case.source_id],
                max_entities_per_source=4,
                max_source_chars=case.max_source_chars,
                max_concurrent=1,
                max_tokens=256,
            )
            observed.append(
                LowSignalCase(
                    source_id=case.source_id,
                    no_op_sources=int(result.get("no_op_sources", 0)),
                    extracted_entities=int(result.get("extracted_entities", 0)),
                    projected_entities=int(result.get("projected_entities", 0)),
                    content=case.content,
                    max_source_chars=case.max_source_chars,
                )
            )
    finally:
        memory_extraction.memory_batch_entity_extractor = old_extractor
        memory_extraction.get_surreal_graph_runtime = old_runtime
    return tuple(observed)


def _raise_low_signal_probe_error() -> None:
    msg = "low-signal probe must not call the extractor"
    raise AssertionError(msg)


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def with_check_results(receipt: dict[str, Any], results: Sequence[GateResult]) -> dict[str, Any]:
    return {
        **receipt,
        "checks": [
            {
                "name": result.check.name,
                "status": "PASS" if result.passed else "FAIL",
                "exit_code": result.exit_code,
                "command": format_command(result.check.command),
                "surfaces": list(result.check.surfaces),
            }
            for result in results
        ],
    }


def write_receipt(receipt: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(receipt, indent=2, sort_keys=True)}\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _hallucinated_fact_count(cases: Sequence[SeededExtractionCase]) -> int:
    return sum(
        1 for case in cases for entity in case.entities if _entity_hallucinates(case, entity)
    )


def _entity_hallucinates(case: SeededExtractionCase, entity: SeededEntity) -> bool:
    combined = " ".join((entity.name, entity.summary, entity.evidence)).lower()
    if any(term.lower() in combined for term in case.forbidden_terms):
        return True
    evidence = " ".join(entity.evidence.lower().split())
    source = " ".join(case.content.lower().split())
    return bool(evidence) and evidence not in source


def _self_referential_write_count(cases: Sequence[DreamSourceCase]) -> int:
    return sum(
        1
        for case in cases
        if case.selected_for_reflection and case.capture_surface.lower() in SELF_FEEDING_SURFACES
    )


def _low_signal_write_count(cases: Sequence[LowSignalCase]) -> int:
    return sum(
        1
        for case in cases
        if case.no_op_sources != 1 or case.extracted_entities != 0 or case.projected_entities != 0
    )


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def _real_runner(command: tuple[str, ...]) -> int:
    executable = which(command[0])
    if executable is None:
        msg = f"Required executable not found on PATH: {command[0]}"
        raise RuntimeError(msg)
    env = dict(os.environ)
    env.setdefault("MOON_COLOR", "false")
    completed = subprocess.run(  # noqa: S603
        (executable, *command[1:]),
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    return completed.returncode


def _run_check(check: GateCheck, *, runner: Runner, echo: Echo) -> GateResult:
    echo("")
    echo(f"[{check.name}] {check.description}")
    echo(f"surfaces: {', '.join(check.surfaces)}")
    echo(f"command: {format_command(check.command)}")

    started = time.perf_counter()
    error: str | None = None
    try:
        exit_code = runner(check.command)
    except Exception as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    status = "PASS" if exit_code == 0 else f"FAIL exit={exit_code}"
    if error is not None:
        status = f"{status} error={error}"
    echo(f"result: {status} in {elapsed:.2f}s")
    return GateResult(
        check=check,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        error=error,
    )


def _print_receipt(receipt: dict[str, Any], results: Sequence[GateResult], *, echo: Echo) -> None:
    passed = [result for result in results if result.passed]
    failed = [result for result in results if not result.passed]
    status = "PASS" if not failed else "FAIL"
    surfaces = sorted(covered_surfaces(result.check for result in results))

    echo("")
    echo("Write-Path Integrity Gate Receipt")
    echo(f"status: {status}")
    echo(f"checks: {len(passed)} passed, {len(failed)} failed")
    echo(
        "metrics: " + ", ".join(f"{metric}={value}" for metric, value in receipt["metrics"].items())
    )
    echo(f"surfaces: {', '.join(surfaces)}")
    for result in results:
        check_status = "PASS" if result.passed else f"FAIL exit={result.exit_code}"
        error = f"; error={result.error}" if result.error is not None else ""
        echo(f"- {check_status} {result.check.name} ({result.elapsed_seconds:.2f}s){error}")


def run_gate(
    checks: Sequence[GateCheck] = GATE_CHECKS,
    *,
    runner: Runner | None = None,
    echo: Echo = _echo,
    receipt_path: Path | None = DEFAULT_RECEIPT_PATH,
) -> int:
    missing = missing_required_surfaces(checks)
    if missing:
        echo("Write-path integrity gate is missing required surfaces:")
        for surface in missing:
            echo(f"- {surface}")
        return 2

    receipt = build_observed_write_path_receipt()
    receipt_failures = validate_write_path_integrity_receipt(receipt)
    if receipt_failures:
        echo("Write-path integrity observation receipt failed:")
        for failure in receipt_failures:
            echo(f"- {failure}")
        return 1
    if receipt_path is not None:
        write_receipt(receipt, receipt_path)

    active_runner = runner or _real_runner
    echo("Write-Path Integrity Gate")
    echo(f"checks: {len(checks)}")
    echo(f"receipt_schema: {receipt['schema_version']}")
    if receipt_path is not None:
        echo(f"receipt: {display_path(receipt_path)}")

    results = [_run_check(check, runner=active_runner, echo=echo) for check in checks]
    final_receipt = with_check_results(receipt, results)
    if receipt_path is not None:
        write_receipt(final_receipt, receipt_path)
    _print_receipt(final_receipt, results, echo=echo)
    return 0 if all(result.passed for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run focused write-path integrity checks.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in GATE_CHECKS:
            _echo(f"{check.name}: {format_command(check.command)}")
        return 0

    return run_gate()


if __name__ == "__main__":
    raise SystemExit(main())
