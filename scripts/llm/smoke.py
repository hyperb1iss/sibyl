#!/usr/bin/env python3
"""Live LLM extraction smoke harness for the native Sibyl substrate."""

# ruff: noqa: E402,T201

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
for source_root in (
    ROOT / "packages/python/sibyl-core/src",
    ROOT / "apps/api/src",
):
    sys.path.insert(0, str(source_root))

from sibyl_core.ai.clients import invalidate_agent_cache
from sibyl_core.ai.errors import LLMError
from sibyl_core.ai.llm import EnvConfigSource, Extractor, LLMSurface, set_config_source
from sibyl_core.ai.registry import ModelEntry, ModelKind, llm_entries, model_registry

EntityLabel = Literal[
    "api",
    "concept",
    "example",
    "language",
    "organization",
    "pattern",
    "person",
    "project",
    "tool",
    "warning",
]

EXTRACTION_PROMPT = """Extract entities from this documentation chunk.

Chunk Content:
{content}

Context (from document):
{context}

Entity types to extract:
- pattern: Coding pattern, best practice, or design pattern
- tool: Library, framework, package, or development tool
- language: Programming language
- concept: Abstract concept, principle, or technique
- api: API endpoint, method, or interface
- warning: Gotcha, pitfall, or common mistake
- example: Code example or usage pattern

Only extract entities that are clearly mentioned or demonstrated.
Do not infer entities that aren't explicitly present.
Return exactly three high-signal entities when at least three are present.
Do not return more than three entities."""

INVALID_NAMES = {"", "UNKNOWN", "unknown", "null", "None", "none"}
LLM_PROVIDERS = {"anthropic", "gemini", "openai"}
MIN_SCHEMA_SUCCESS_RATE = 0.95
MIN_FIELD_COVERAGE = 0.98
MAX_TYPE_DISTRIBUTION_DRIFT = 0.25
MAX_ENTITY_COUNT_DELTA = 0.15
MIN_NAME_QUALITY_RATE = 0.98
MIN_ENTITY_NAME_LENGTH = 2


class ExtractedEntityPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    entity_type: EntityLabel = Field(alias="type")
    description: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)

    model_config = ConfigDict(populate_by_name=True)


class ExtractedEntitiesPayload(BaseModel):
    entities: list[ExtractedEntityPayload] = Field(default_factory=list, max_length=50)


@dataclass(frozen=True)
class ExpectedEntity:
    name: str
    entity_type: EntityLabel


@dataclass(frozen=True)
class SmokeChunk:
    chunk_id: str
    content: str
    context: str
    expected: tuple[ExpectedEntity, ...]

    @property
    def prompt(self) -> str:
        return EXTRACTION_PROMPT.format(content=self.content, context=self.context)


@dataclass(frozen=True)
class ChunkResult:
    chunk_id: str
    success: bool
    latency_ms: float
    entity_count: int
    estimated_cost_usd: float
    error: str | None
    entities: tuple[ExtractedEntityPayload, ...]


@dataclass(frozen=True)
class SmokeSummary:
    model_alias: str
    provider: str
    provider_model_id: str
    chunks: int
    schema_success_rate: float
    field_coverage: float
    type_distribution_drift: float
    entity_count_delta: float
    name_quality_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    average_estimated_cost_usd: float
    total_estimated_cost_usd: float
    passed: bool


FIXTURE: tuple[SmokeChunk, ...] = (
    SmokeChunk(
        "chunk-01",
        "FastAPI dependencies inject the current organization before a route opens a SurrealDB "
        "EntityManager. Python async handlers should clone the driver per org scope.",
        "API routing and tenancy notes",
        (
            ExpectedEntity("FastAPI", "tool"),
            ExpectedEntity("Python", "language"),
            ExpectedEntity("SurrealDB", "tool"),
        ),
    ),
    SmokeChunk(
        "chunk-02",
        "React Query caches the AI settings response, while the WebSocket client keeps tasks and "
        "entities fresh without polling the whole dashboard.",
        "Web UI runtime",
        (
            ExpectedEntity("React Query", "tool"),
            ExpectedEntity("WebSocket", "api"),
            ExpectedEntity("AI settings response", "api"),
        ),
    ),
    SmokeChunk(
        "chunk-03",
        "SurrealDB namespaces isolate each organization. Missing group_id context can query the "
        "wrong namespace and break memory isolation.",
        "Graph data plane",
        (
            ExpectedEntity("SurrealDB namespaces", "concept"),
            ExpectedEntity("group_id", "api"),
            ExpectedEntity("memory isolation", "concept"),
        ),
    ),
    SmokeChunk(
        "chunk-04",
        "Redis-backed arq workers remain opt-in. The local coordination backend runs jobs "
        "in-process for single-machine development.",
        "Background jobs",
        (
            ExpectedEntity("Redis", "tool"),
            ExpectedEntity("arq", "tool"),
            ExpectedEntity("local coordination backend", "concept"),
        ),
    ),
    SmokeChunk(
        "chunk-05",
        "PydanticAI provides structured output across Anthropic, Gemini, and OpenAI so crawler "
        "extraction can validate entities before graph writes.",
        "LLM substrate",
        (
            ExpectedEntity("PydanticAI", "tool"),
            ExpectedEntity("Anthropic", "organization"),
            ExpectedEntity("OpenAI", "organization"),
        ),
    ),
    SmokeChunk(
        "chunk-06",
        "Use uv for Python package management, Ruff for linting, and ty for type checking. Avoid "
        "uv pip because it bypasses the workspace dependency graph.",
        "Python tooling",
        (
            ExpectedEntity("uv", "tool"),
            ExpectedEntity("Ruff", "tool"),
            ExpectedEntity("ty", "tool"),
        ),
    ),
    SmokeChunk(
        "chunk-07",
        "Next.js 16 uses proxy.ts instead of middleware.ts. Server components are default, so add "
        "'use client' only when state or browser APIs are required.",
        "Frontend conventions",
        (
            ExpectedEntity("Next.js 16", "tool"),
            ExpectedEntity("proxy.ts", "api"),
            ExpectedEntity("server components", "concept"),
        ),
    ),
    SmokeChunk(
        "chunk-08",
        "JWT sessions, OAuth login, and GitHub identity all flow through role-based access control "
        "before settings routes accept writes.",
        "Auth control plane",
        (
            ExpectedEntity("JWT sessions", "concept"),
            ExpectedEntity("OAuth", "api"),
            ExpectedEntity("role-based access control", "concept"),
        ),
    ),
    SmokeChunk(
        "chunk-09",
        "Backup archives bundle graph, content, and auth payloads behind one archive manifest. "
        "Imports warn about unrecognized payloads and skip them instead of guessing their shape.",
        "Archive portability",
        (
            ExpectedEntity("unrecognized payloads", "warning"),
            ExpectedEntity("backup archives", "concept"),
            ExpectedEntity("archive manifest", "pattern"),
        ),
    ),
    SmokeChunk(
        "chunk-10",
        "Vector embeddings use HNSW indexes and cosine distance for semantic search. Re-embedding "
        "is required when dimensions or providers change.",
        "Retrieval indexes",
        (
            ExpectedEntity("Vector embeddings", "concept"),
            ExpectedEntity("HNSW indexes", "concept"),
            ExpectedEntity("cosine distance", "concept"),
        ),
    ),
    SmokeChunk(
        "chunk-11",
        "Docker, Helm, and Homebrew packaging should tell the same install story as the local moon "
        "workspace.",
        "Distribution",
        (
            ExpectedEntity("Docker", "tool"),
            ExpectedEntity("Helm", "tool"),
            ExpectedEntity("Homebrew", "tool"),
        ),
    ),
    SmokeChunk(
        "chunk-12",
        "The MCP streamable-http endpoint exposes search, explore, add, and manage tools for "
        "agents that need bounded context.",
        "MCP server",
        (
            ExpectedEntity("MCP streamable-http endpoint", "api"),
            ExpectedEntity("search tool", "api"),
            ExpectedEntity("bounded context", "concept"),
        ),
    ),
    SmokeChunk(
        "chunk-13",
        "Tailwind CSS v4 renders the SilkCircuit palette with OKLCH tokens so dark-mode settings "
        "screens stay readable.",
        "Design system",
        (
            ExpectedEntity("Tailwind CSS v4", "tool"),
            ExpectedEntity("SilkCircuit palette", "concept"),
            ExpectedEntity("OKLCH tokens", "concept"),
        ),
    ),
    SmokeChunk(
        "chunk-14",
        "Vitest covers the settings card, Biome enforces formatting, and Playwright handles browser "
        "smoke checks when visual behavior matters.",
        "Web verification",
        (
            ExpectedEntity("Vitest", "tool"),
            ExpectedEntity("Biome", "tool"),
            ExpectedEntity("Playwright", "tool"),
        ),
    ),
    SmokeChunk(
        "chunk-15",
        "The SurrealDB driver serializes websocket writes through an asyncio.Lock. Do not share one "
        "driver instance across organizations.",
        "Concurrency warning",
        (
            ExpectedEntity("SurrealDB driver", "tool"),
            ExpectedEntity("asyncio.Lock", "api"),
            ExpectedEntity("driver sharing across organizations", "warning"),
        ),
    ),
    SmokeChunk(
        "chunk-16",
        "Memory spaces, organization roles, and delegated agent authority define who can read or "
        "write context packs.",
        "Trust model",
        (
            ExpectedEntity("Memory spaces", "concept"),
            ExpectedEntity("organization roles", "concept"),
            ExpectedEntity("delegated agent authority", "concept"),
        ),
    ),
    SmokeChunk(
        "chunk-17",
        "Synthesis produces Markdown and JSON artifacts from graph slices, with unsupported-claim "
        "reports when source evidence is missing.",
        "Synthesis artifacts",
        (
            ExpectedEntity("Synthesis", "concept"),
            ExpectedEntity("Markdown artifacts", "example"),
            ExpectedEntity("unsupported-claim reports", "pattern"),
        ),
    ),
    SmokeChunk(
        "chunk-18",
        "The crawler stores document chunks, runs EntityExtractor, and links extracted entities back "
        "to source URLs.",
        "Crawler integration",
        (
            ExpectedEntity("crawler", "tool"),
            ExpectedEntity("document chunks", "concept"),
            ExpectedEntity("EntityExtractor", "api"),
        ),
    ),
    SmokeChunk(
        "chunk-19",
        "SurrealDB backs the graph, content, and auth stores. The surrealdb Python SDK reaches it "
        "over WebSocket RPC, and SurrealKV persists the embedded dev database.",
        "Storage runtime",
        (
            ExpectedEntity("SurrealDB", "tool"),
            ExpectedEntity("surrealdb Python SDK", "tool"),
            ExpectedEntity("SurrealKV", "tool"),
        ),
    ),
    SmokeChunk(
        "chunk-20",
        "moonrepo orchestrates lint, test, and typecheck tasks after proto installs pnpm, uv, and "
        "the Python toolchain.",
        "Monorepo tooling",
        (
            ExpectedEntity("moonrepo", "tool"),
            ExpectedEntity("proto", "tool"),
            ExpectedEntity("pnpm", "tool"),
        ),
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", help="Registry alias or snapshot to probe.")
    parser.add_argument("--provider", action="append", choices=sorted(LLM_PROVIDERS))
    parser.add_argument("--json", action="store_true", help="Emit machine-readable summary JSON.")
    parser.add_argument(
        "--require-keys", action="store_true", help="Fail if any selected key is missing."
    )
    parser.add_argument("--max-concurrent", type=int, default=2)
    args = parser.parse_args()

    return asyncio.run(_main(args))


async def _main(args: argparse.Namespace) -> int:
    selected_entries = _selected_entries(args.model, args.provider)
    if not selected_entries:
        print("No LLM registry entries matched the requested filters.", file=sys.stderr)
        return 2

    summaries: list[SmokeSummary] = []
    skipped: list[dict[str, str]] = []
    failed = False

    for entry in selected_entries:
        key = _api_key_for(entry.provider)
        if key is None:
            skipped.append(
                {"provider": entry.provider, "model": entry.alias, "reason": "missing key"}
            )
            continue

        summary, results = await _run_entry(entry, key, max_concurrent=args.max_concurrent)
        summaries.append(summary)
        failed = failed or not summary.passed
        _print_summary(summary, results)

    if skipped:
        for item in skipped:
            print(f"skipped {item['provider']}:{item['model']} ({item['reason']})")
        if args.require_keys:
            failed = True

    if args.json:
        print(
            json.dumps(
                {
                    "summaries": [_dataclass_dict(summary) for summary in summaries],
                    "skipped": skipped,
                    "fixture_chunks": len(FIXTURE),
                },
                indent=2,
                sort_keys=True,
            )
        )

    if not summaries and args.require_keys:
        return 2
    return 1 if failed else 0


async def _run_entry(
    entry: ModelEntry,
    api_key: str,
    *,
    max_concurrent: int,
) -> tuple[SmokeSummary, list[ChunkResult]]:
    provider = cast(Literal["anthropic", "gemini", "openai"], entry.provider)
    env = {
        **os.environ,
        "SIBYL_LLM_CRAWLER_PROVIDER": provider,
        "SIBYL_LLM_CRAWLER_MODEL": entry.alias,
        "SIBYL_LLM_CRAWLER_TEMPERATURE": str(entry.default_temperature or 0.0),
        "SIBYL_LLM_CRAWLER_MAX_TOKENS": str(min(entry.max_output_tokens or 2048, 2048)),
        "SIBYL_LLM_CRAWLER_TIMEOUT_SECONDS": "45",
        _primary_key_env(provider): api_key,
    }
    set_config_source(EnvConfigSource(env))
    invalidate_agent_cache(LLMSurface.CRAWLER)

    extractor = Extractor(
        ExtractedEntitiesPayload,
        surface=LLMSurface.CRAWLER,
        output_retries=2,
    )
    semaphore = asyncio.Semaphore(max_concurrent)

    async def run_chunk(chunk: SmokeChunk) -> ChunkResult:
        async with semaphore:
            return await _run_chunk(extractor, chunk, entry)

    results = await asyncio.gather(*(run_chunk(chunk) for chunk in FIXTURE))
    summary = _summarize(entry, results)
    return summary, results


async def _run_chunk(
    extractor: Extractor[ExtractedEntitiesPayload],
    chunk: SmokeChunk,
    entry: ModelEntry,
) -> ChunkResult:
    started_at = time.perf_counter()
    try:
        payload = await extractor.extract(chunk.prompt)
        latency_ms = _elapsed_ms(started_at)
        entities = tuple(payload.entities)
        return ChunkResult(
            chunk_id=chunk.chunk_id,
            success=True,
            latency_ms=latency_ms,
            entity_count=len(entities),
            estimated_cost_usd=_estimate_cost(chunk.prompt, payload.model_dump_json(), entry),
            error=None,
            entities=entities,
        )
    except LLMError as exc:
        return ChunkResult(
            chunk_id=chunk.chunk_id,
            success=False,
            latency_ms=_elapsed_ms(started_at),
            entity_count=0,
            estimated_cost_usd=0.0,
            error=str(exc),
            entities=(),
        )


def _summarize(entry: ModelEntry, results: Sequence[ChunkResult]) -> SmokeSummary:
    expected_count = sum(len(chunk.expected) for chunk in FIXTURE)
    actual_entities = [entity for result in results for entity in result.entities]
    actual_count = len(actual_entities)
    latencies = [result.latency_ms for result in results if result.success]
    costs = [result.estimated_cost_usd for result in results if result.success]

    schema_success_rate = sum(result.success for result in results) / len(results)
    field_coverage = _field_coverage(actual_entities)
    type_distribution_drift = _type_distribution_drift(actual_entities)
    entity_count_delta = abs(actual_count - expected_count) / expected_count
    name_quality_rate = _name_quality_rate(actual_entities)

    return SmokeSummary(
        model_alias=entry.alias,
        provider=entry.provider,
        provider_model_id=entry.provider_model_id,
        chunks=len(results),
        schema_success_rate=schema_success_rate,
        field_coverage=field_coverage,
        type_distribution_drift=type_distribution_drift,
        entity_count_delta=entity_count_delta,
        name_quality_rate=name_quality_rate,
        p50_latency_ms=statistics.median(latencies) if latencies else 0.0,
        p95_latency_ms=_percentile(latencies, 0.95),
        average_estimated_cost_usd=sum(costs) / len(costs) if costs else 0.0,
        total_estimated_cost_usd=sum(costs),
        passed=(
            schema_success_rate >= MIN_SCHEMA_SUCCESS_RATE
            and field_coverage >= MIN_FIELD_COVERAGE
            and type_distribution_drift <= MAX_TYPE_DISTRIBUTION_DRIFT
            and entity_count_delta <= MAX_ENTITY_COUNT_DELTA
            and name_quality_rate >= MIN_NAME_QUALITY_RATE
        ),
    )


def _field_coverage(entities: Sequence[ExtractedEntityPayload]) -> float:
    if not entities:
        return 0.0
    covered = 0
    for entity in entities:
        covered += int(bool(entity.name.strip()))
        covered += int(bool(entity.entity_type))
        covered += int(bool(entity.description.strip()))
        covered += int(0.0 <= entity.confidence <= 1.0)
    return covered / (len(entities) * 4)


def _type_distribution_drift(entities: Sequence[ExtractedEntityPayload]) -> float:
    expected = Counter(expected.entity_type for chunk in FIXTURE for expected in chunk.expected)
    actual = Counter(entity.entity_type for entity in entities)
    expected_total = sum(expected.values())
    actual_total = sum(actual.values()) or 1
    labels = set(expected) | set(actual)
    return max(
        abs((actual[label] / actual_total) - (expected[label] / expected_total)) for label in labels
    )


def _name_quality_rate(entities: Sequence[ExtractedEntityPayload]) -> float:
    if not entities:
        return 0.0
    good = 0
    for entity in entities:
        name = entity.name.strip()
        good += int(len(name) >= MIN_ENTITY_NAME_LENGTH and name not in INVALID_NAMES)
    return good / len(entities)


def _selected_entries(
    aliases: Sequence[str] | None,
    providers: Sequence[str] | None,
) -> list[ModelEntry]:
    selected: list[ModelEntry] = []
    if aliases:
        for alias in aliases:
            entry = model_registry.get(alias, kind=ModelKind.LLM)
            if entry is None:
                raise SystemExit(f"Unknown LLM registry entry: {alias}")
            selected.append(entry)
    else:
        selected = llm_entries()

    if providers:
        provider_set = set(providers)
        selected = [entry for entry in selected if entry.provider in provider_set]

    return [entry for entry in selected if entry.provider in LLM_PROVIDERS]


def _api_key_for(provider: str) -> str | None:
    for name in _key_env_names(provider):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _key_env_names(provider: str) -> tuple[str, ...]:
    return {
        "anthropic": ("SIBYL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        "gemini": ("SIBYL_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "openai": ("SIBYL_OPENAI_API_KEY", "OPENAI_API_KEY"),
    }[provider]


def _primary_key_env(provider: str) -> str:
    return _key_env_names(provider)[0]


def _estimate_cost(prompt: str, output: str, entry: ModelEntry) -> float:
    input_tokens = max(1, len(prompt) // 4)
    output_tokens = max(1, len(output) // 4)
    input_cost = (input_tokens / 1_000_000) * entry.input_cost_per_mtok_usd
    output_cost = (output_tokens / 1_000_000) * (entry.output_cost_per_mtok_usd or 0.0)
    return round(input_cost + output_cost, 8)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 2)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _dataclass_dict(value: Any) -> dict[str, Any]:
    return value.__dict__.copy()


def _print_summary(summary: SmokeSummary, results: Sequence[ChunkResult]) -> None:
    status = "PASS" if summary.passed else "FAIL"
    print(f"{status} {summary.provider}:{summary.model_alias}")
    print(
        f"  schema={summary.schema_success_rate:.1%} fields={summary.field_coverage:.1%} "
        f"drift={summary.type_distribution_drift:.1%} "
        f"count_delta={summary.entity_count_delta:.1%} names={summary.name_quality_rate:.1%}"
    )
    print(
        f"  latency p50={summary.p50_latency_ms:.0f}ms "
        f"p95={summary.p95_latency_ms:.0f}ms "
        f"estimated_cost=${summary.total_estimated_cost_usd:.6f}"
    )
    failures = [result for result in results if not result.success]
    for failure in failures[:3]:
        print(f"  {failure.chunk_id}: {failure.error}")


if __name__ == "__main__":
    raise SystemExit(main())
