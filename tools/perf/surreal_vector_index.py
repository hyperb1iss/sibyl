"""Benchmark SurrealDB vector index choices for content chunk search."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from surrealdb import AsyncSurreal

JsonObject = dict[str, Any]

DEFAULT_ROWS = 2_000
DEFAULT_DIMENSIONS = 128
DEFAULT_QUERIES = 20
DEFAULT_LIMIT = 10
DEFAULT_EF = 40
DEFAULT_BATCH_SIZE = 250
DEFAULT_SEED = 20260530
DEFAULT_CONTENT_BYTES = 512
DEFAULT_HNSW_EFC = 150
DEFAULT_HNSW_M = 12
DEFAULT_DISKANN_DEGREE = 16
DEFAULT_DISKANN_L_BUILD = 64
DEFAULT_OUTPUT_DIR = Path("benchmarks/results/surreal-vector")
DEFAULT_CACHE_ROOT = Path(".moon/cache/surreal-vector-index-bench")
MILLISECONDS_PER_SECOND = 1000.0
PERCENTILE_50 = 0.50
PERCENTILE_95 = 0.95
NOISE_SCALE = 0.035
MIN_CENTROIDS = 8
MAX_CENTROIDS = 64
ROWS_PER_CENTROID = 50
TARGET_ORGANIZATION_ID = "org-primary"
TARGET_SOURCE_ID = "source-primary"
ORGANIZATION_IDS = (TARGET_ORGANIZATION_ID, "org-secondary", "org-tertiary")
SOURCE_IDS = (TARGET_SOURCE_ID, "source-docs", "source-code")
INDEX_KINDS = ("hnsw", "diskann")
TEXT_CHUNK = "Sibyl vector benchmark content with org/source scoped document chunks. "
MAX_ERROR_LENGTH = 1_000
DISKANN_WIN_P95_RATIO = 0.90
DISKANN_BUILD_TIME_RATIO = 1.25
RECALL_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class VectorBenchConfig:
    rows: int
    dimensions: int
    queries: int
    limit: int
    candidate_limit: int
    ef: int
    batch_size: int
    seed: int
    content_bytes: int
    output_path: Path
    cache_dir: Path
    run_id: str
    url: str | None
    namespace: str
    database: str
    include_diskann: bool
    hnsw_efc: int
    hnsw_m: int
    diskann_degree: int
    diskann_l_build: int


@dataclass(frozen=True, slots=True)
class ChunkRow:
    uuid: str
    organization_id: str
    source_id: str
    document_id: str
    chunk_index: int
    content: str
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class QuerySample:
    index: int
    embedding: list[float]
    expected_uuids: list[str]


@dataclass(frozen=True, slots=True)
class IndexPlan:
    name: str
    table: str
    index_name: str
    definition: str


@dataclass(frozen=True, slots=True)
class QueryMeasurement:
    query_index: int
    latency_ms: float
    recall: float
    result_count: int
    expected_count: int


def generate_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def default_output_path(run_id: str) -> Path:
    return DEFAULT_OUTPUT_DIR / f"surreal_vector_index_{run_id}.json"


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]

    lower = ordered[lower_index]
    upper = ordered[upper_index]
    return lower + (upper - lower) * (position - lower_index)


def exact_top_k(
    rows: Sequence[ChunkRow],
    query_embedding: Sequence[float],
    *,
    organization_id: str,
    source_id: str,
    limit: int,
) -> list[str]:
    candidates = (
        row for row in rows if row.organization_id == organization_id and row.source_id == source_id
    )
    scored = sorted(
        ((cosine_similarity(row.embedding, query_embedding), row.uuid) for row in candidates),
        key=lambda item: (-item[0], item[1]),
    )
    return [uuid for _, uuid in scored[:limit]]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def recall_at(expected: Sequence[str], actual: Sequence[str]) -> float:
    if not expected:
        return 1.0
    return len(set(expected).intersection(actual)) / len(expected)


def build_index_plans(config: VectorBenchConfig) -> list[IndexPlan]:
    suffix = surreal_identifier(config.run_id)
    plans = [
        IndexPlan(
            name="hnsw",
            table=f"document_chunks_vector_bench_{suffix}_hnsw",
            index_name=f"idx_vector_bench_{suffix}_hnsw_embedding",
            definition=(
                f"DEFINE INDEX idx_vector_bench_{suffix}_hnsw_embedding "
                f"ON document_chunks_vector_bench_{suffix}_hnsw FIELDS embedding "
                f"HNSW DIMENSION {config.dimensions} DIST COSINE TYPE F32 "
                f"EFC {config.hnsw_efc} M {config.hnsw_m};"
            ),
        )
    ]
    if config.include_diskann:
        plans.append(
            IndexPlan(
                name="diskann",
                table=f"document_chunks_vector_bench_{suffix}_diskann",
                index_name=f"idx_vector_bench_{suffix}_diskann_embedding",
                definition=(
                    f"DEFINE INDEX idx_vector_bench_{suffix}_diskann_embedding "
                    f"ON document_chunks_vector_bench_{suffix}_diskann FIELDS embedding "
                    f"DISKANN DIMENSION {config.dimensions} DIST COSINE TYPE F32 "
                    f"DEGREE {config.diskann_degree} L_BUILD {config.diskann_l_build};"
                ),
            )
        )
    return plans


def surreal_identifier(raw: str) -> str:
    identifier = re.sub(r"[^a-zA-Z0-9_]", "_", raw).strip("_").lower()
    if not identifier:
        return "run"
    if identifier[0].isdigit():
        return f"run_{identifier}"
    return identifier


def generate_rows(config: VectorBenchConfig) -> list[ChunkRow]:
    rng = random.Random(config.seed)  # noqa: S311
    centroid_count = min(MAX_CENTROIDS, max(MIN_CENTROIDS, config.rows // ROWS_PER_CENTROID))
    centroids = [_random_unit_vector(rng, config.dimensions) for _ in range(centroid_count)]
    content = _content(config.content_bytes)
    rows: list[ChunkRow] = []
    for index in range(config.rows):
        org_id = ORGANIZATION_IDS[index % len(ORGANIZATION_IDS)]
        source_id = SOURCE_IDS[(index // len(ORGANIZATION_IDS)) % len(SOURCE_IDS)]
        centroid = centroids[index % centroid_count]
        embedding = _perturb_vector(rng, centroid, config.dimensions)
        rows.append(
            ChunkRow(
                uuid=f"chunk-{index:08d}",
                organization_id=org_id,
                source_id=source_id,
                document_id=f"doc-{source_id}-{index // 8:06d}",
                chunk_index=index,
                content=content,
                embedding=embedding,
            )
        )
    return rows


def build_query_samples(
    rows: Sequence[ChunkRow],
    config: VectorBenchConfig,
) -> list[QuerySample]:
    target_rows = [
        row
        for row in rows
        if row.organization_id == TARGET_ORGANIZATION_ID and row.source_id == TARGET_SOURCE_ID
    ]
    if len(target_rows) < config.limit:
        raise ValueError(
            "workload does not contain enough target org/source rows for recall measurement"
        )
    rng = random.Random(config.seed + 1)  # noqa: S311
    samples: list[QuerySample] = []
    for index in range(config.queries):
        base = target_rows[(index * len(target_rows)) // config.queries]
        query_embedding = _perturb_vector(rng, base.embedding, config.dimensions)
        samples.append(
            QuerySample(
                index=index,
                embedding=query_embedding,
                expected_uuids=exact_top_k(
                    rows,
                    query_embedding,
                    organization_id=TARGET_ORGANIZATION_ID,
                    source_id=TARGET_SOURCE_ID,
                    limit=config.limit,
                ),
            )
        )
    return samples


def summarize_measurements(measurements: Sequence[QueryMeasurement]) -> JsonObject:
    latencies = [measurement.latency_ms for measurement in measurements]
    recalls = [measurement.recall for measurement in measurements]
    return {
        "query_count": len(measurements),
        "recall_at_k": sum(recalls) / len(recalls) if recalls else None,
        "latency_ms": {
            "min": min(latencies) if latencies else None,
            "p50": percentile(latencies, PERCENTILE_50),
            "p95": percentile(latencies, PERCENTILE_95),
            "max": max(latencies) if latencies else None,
            "avg": sum(latencies) / len(latencies) if latencies else None,
        },
        "samples": [asdict(measurement) for measurement in measurements],
    }


def choose_index(results: dict[str, JsonObject]) -> JsonObject:
    hnsw = results.get("hnsw", {})
    diskann = results.get("diskann", {})
    if not hnsw.get("supported"):
        return {
            "recommendation": "no_adoption",
            "reason": "HNSW benchmark did not complete, so the production baseline is invalid.",
        }
    if not diskann:
        return {
            "recommendation": "keep_hnsw",
            "reason": "DiskANN was not included in this run.",
        }
    if not diskann.get("supported"):
        diskann_error = str(diskann.get("error", "unknown error")).strip()
        hnsw_recall = _recall_at_k(hnsw)
        recall_note = (
            f"; HNSW baseline recall was {hnsw_recall:.3f}, so investigate "
            "the production scalar-prefilter KNN query shape separately."
            if hnsw_recall is not None and hnsw_recall < RECALL_TOLERANCE
            else ""
        )
        return {
            "recommendation": "keep_hnsw",
            "reason": f"DiskANN is unavailable in this runtime: {diskann_error}{recall_note}",
        }

    hnsw_summary = hnsw["query_summary"]
    diskann_summary = diskann["query_summary"]
    hnsw_recall = float(hnsw_summary["recall_at_k"])
    diskann_recall = float(diskann_summary["recall_at_k"])
    hnsw_p95 = float(hnsw_summary["latency_ms"]["p95"])
    diskann_p95 = float(diskann_summary["latency_ms"]["p95"])
    hnsw_build = float(hnsw["index_build_ms"])
    diskann_build = float(diskann["index_build_ms"])

    if (
        diskann_recall + RECALL_TOLERANCE >= hnsw_recall
        and diskann_p95 <= hnsw_p95 * DISKANN_WIN_P95_RATIO
        and diskann_build <= hnsw_build * DISKANN_BUILD_TIME_RATIO
    ):
        return {
            "recommendation": "adopt_diskann",
            "reason": (
                "DiskANN preserved recall while improving p95 latency without a large "
                "index-build penalty."
            ),
        }
    return {
        "recommendation": "keep_hnsw",
        "reason": "DiskANN did not beat the current HNSW target-workload baseline.",
    }


def _recall_at_k(result: JsonObject) -> float | None:
    summary = result.get("query_summary")
    if not isinstance(summary, dict):
        return None
    recall = summary.get("recall_at_k")
    if not isinstance(recall, int | float):
        return None
    return float(recall)


async def run_vector_index_benchmark(config: VectorBenchConfig) -> JsonObject:
    if config.url is None:
        config.cache_dir.mkdir(parents=True, exist_ok=True)
    rows = generate_rows(config)
    query_samples = build_query_samples(rows, config)
    plans = build_index_plans(config)
    results: dict[str, JsonObject] = {}
    for plan in plans:
        results[plan.name] = await _benchmark_index(config, plan, rows, query_samples)

    return {
        "schema_version": "sibyl-surreal-vector-index-bench-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": config.run_id,
        "surreal": {
            "python_package_version": _package_version("surrealdb"),
            "url_mode": "custom" if config.url else "embedded-surrealkv",
            "namespace": config.namespace,
            "database": config.database,
            "engine_version": _first_non_empty(
                *(result.get("engine_version") for result in results.values())
            ),
        },
        "workload": {
            "rows": config.rows,
            "dimensions": config.dimensions,
            "queries": config.queries,
            "limit": config.limit,
            "candidate_limit": config.candidate_limit,
            "ef": config.ef,
            "query_mode": "production_scalar_prefilter",
            "batch_size": config.batch_size,
            "seed": config.seed,
            "content_bytes": config.content_bytes,
            "organization_filter": TARGET_ORGANIZATION_ID,
            "source_filter": TARGET_SOURCE_ID,
            "organization_ids": list(ORGANIZATION_IDS),
            "source_ids": list(SOURCE_IDS),
        },
        "index_definitions": {plan.name: plan.definition for plan in plans},
        "results": results,
        "decision": choose_index(results),
        "source_notes": [
            "DiskANN was introduced in SurrealDB 3.1 under the same KNN operator as HNSW.",
            "The query shape mirrors Sibyl content retrieval org/source filters.",
        ],
    }


def write_report(report: JsonObject, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def format_summary(report: JsonObject, output_path: Path) -> str:
    lines = [
        "Sibyl SurrealDB vector index benchmark",
        f"  run_id: {report['run_id']}",
        f"  output: {output_path}",
        f"  decision: {report['decision']['recommendation']}",
        f"  reason: {report['decision']['reason']}",
        "  results:",
    ]
    for name, result in report["results"].items():
        if not result["supported"]:
            lines.append(f"    {name}: unsupported error={result['error']}")
            continue
        latency = result["query_summary"]["latency_ms"]
        lines.append(
            f"    {name}: recall={result['query_summary']['recall_at_k']:.3f} "
            f"p50_ms={_format_number(latency['p50'])} "
            f"p95_ms={_format_number(latency['p95'])} "
            f"index_build_ms={_format_number(result['index_build_ms'])} "
            f"disk_bytes={result['disk_bytes']}"
        )
    return "\n".join(lines) + "\n"


def config_from_args(argv: Sequence[str] | None = None) -> VectorBenchConfig:
    run_id = os.getenv("SIBYL_VECTOR_BENCH_RUN_ID") or generate_run_id()
    parser = argparse.ArgumentParser(
        description="Benchmark HNSW and DiskANN for Sibyl document chunk vectors."
    )
    parser.add_argument(
        "--rows", type=int, default=_env_int("SIBYL_VECTOR_BENCH_ROWS", DEFAULT_ROWS)
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_DIMENSIONS", DEFAULT_DIMENSIONS),
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_QUERIES", DEFAULT_QUERIES),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_LIMIT", DEFAULT_LIMIT),
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_CANDIDATE_LIMIT", DEFAULT_LIMIT * 5),
    )
    parser.add_argument("--ef", type=int, default=_env_int("SIBYL_VECTOR_BENCH_EF", DEFAULT_EF))
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_BATCH_SIZE", DEFAULT_BATCH_SIZE),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_SEED", DEFAULT_SEED),
    )
    parser.add_argument(
        "--content-bytes",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_CONTENT_BYTES", DEFAULT_CONTENT_BYTES),
    )
    output_env = os.getenv("SIBYL_VECTOR_BENCH_OUTPUT_PATH")
    cache_env = os.getenv("SIBYL_VECTOR_BENCH_CACHE_DIR")
    parser.add_argument(
        "--output-path",
        type=Path,
        default=Path(output_env) if output_env else None,
    )
    parser.add_argument("--cache-dir", type=Path, default=Path(cache_env) if cache_env else None)
    parser.add_argument("--run-id", default=run_id)
    parser.add_argument("--url", default=os.getenv("SIBYL_VECTOR_BENCH_URL"))
    parser.add_argument(
        "--namespace",
        default=os.getenv("SIBYL_VECTOR_BENCH_NAMESPACE", "sibyl_vector_bench"),
    )
    parser.add_argument(
        "--database",
        default=os.getenv("SIBYL_VECTOR_BENCH_DATABASE", "bench"),
    )
    parser.add_argument(
        "--skip-diskann",
        action="store_true",
        help="Benchmark only the current HNSW baseline.",
    )
    parser.add_argument(
        "--hnsw-efc",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_HNSW_EFC", DEFAULT_HNSW_EFC),
    )
    parser.add_argument(
        "--hnsw-m",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_HNSW_M", DEFAULT_HNSW_M),
    )
    parser.add_argument(
        "--diskann-degree",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_DISKANN_DEGREE", DEFAULT_DISKANN_DEGREE),
    )
    parser.add_argument(
        "--diskann-l-build",
        type=int,
        default=_env_int("SIBYL_VECTOR_BENCH_DISKANN_L_BUILD", DEFAULT_DISKANN_L_BUILD),
    )
    args = parser.parse_args(argv)
    rows = _positive_int(args.rows, "rows")
    dimensions = _positive_int(args.dimensions, "dimensions")
    queries = _positive_int(args.queries, "queries")
    limit = _positive_int(args.limit, "limit")
    candidate_limit = max(_positive_int(args.candidate_limit, "candidate_limit"), limit)
    return VectorBenchConfig(
        rows=rows,
        dimensions=dimensions,
        queries=queries,
        limit=limit,
        candidate_limit=candidate_limit,
        ef=_positive_int(args.ef, "ef"),
        batch_size=_positive_int(args.batch_size, "batch_size"),
        seed=args.seed,
        content_bytes=_positive_int(args.content_bytes, "content_bytes"),
        output_path=args.output_path or default_output_path(args.run_id),
        cache_dir=args.cache_dir or DEFAULT_CACHE_ROOT / args.run_id,
        run_id=args.run_id,
        url=args.url,
        namespace=args.namespace,
        database=args.database,
        include_diskann=not args.skip_diskann,
        hnsw_efc=_positive_int(args.hnsw_efc, "hnsw_efc"),
        hnsw_m=_positive_int(args.hnsw_m, "hnsw_m"),
        diskann_degree=_positive_int(args.diskann_degree, "diskann_degree"),
        diskann_l_build=_positive_int(args.diskann_l_build, "diskann_l_build"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = config_from_args(argv)
    report = asyncio.run(run_vector_index_benchmark(config))
    write_report(report, config.output_path)
    sys.stdout.write(format_summary(report, config.output_path))
    return 0


async def _benchmark_index(
    config: VectorBenchConfig,
    plan: IndexPlan,
    rows: Sequence[ChunkRow],
    query_samples: Sequence[QuerySample],
) -> JsonObject:
    url = _url_for_index(config, plan.name)
    store_path = _store_path_for_index(config, plan.name)
    started = time.perf_counter()
    try:
        db = await _connect(url=url, namespace=config.namespace, database=config.database)
        try:
            engine_version = await _surreal_engine_version(db)
            await _prepare_table(db, plan.table, config.dimensions)
            insert_ms = await _insert_rows(db, plan.table, rows, config.batch_size)
            index_started = time.perf_counter()
            await db.query(plan.definition)
            index_build_ms = _elapsed_ms(index_started)
            measurements = await _run_queries(db, plan.table, query_samples, config)
            table_info = await _table_info(db, plan.table)
        finally:
            await db.close()
    except Exception as exc:
        return {
            "supported": False,
            "index": plan.name,
            "table": plan.table,
            "url": _redact_url(url),
            "index_definition": plan.definition,
            "error": _error_text(exc),
            "elapsed_ms": _elapsed_ms(started),
            "disk_bytes": _path_size(store_path) if store_path is not None else None,
        }

    return {
        "supported": True,
        "index": plan.name,
        "table": plan.table,
        "url": _redact_url(url),
        "index_definition": plan.definition,
        "engine_version": engine_version,
        "insert_ms": insert_ms,
        "index_build_ms": index_build_ms,
        "elapsed_ms": _elapsed_ms(started),
        "disk_bytes": _path_size(store_path) if store_path is not None else None,
        "table_info": table_info,
        "query_summary": summarize_measurements(measurements),
    }


async def _connect(*, url: str, namespace: str, database: str) -> Any:
    db = AsyncSurreal(url)
    await db.use(namespace, database)
    return db


async def _prepare_table(db: Any, table: str, dimensions: int) -> None:
    await db.query(f"REMOVE TABLE IF EXISTS {table};")
    await db.query(
        f"""
        DEFINE TABLE {table} SCHEMAFULL;
        DEFINE FIELD uuid ON {table} TYPE string;
        DEFINE FIELD organization_id ON {table} TYPE string;
        DEFINE FIELD source_id ON {table} TYPE string;
        DEFINE FIELD document_id ON {table} TYPE string;
        DEFINE FIELD chunk_index ON {table} TYPE int;
        DEFINE FIELD content ON {table} TYPE string;
        DEFINE FIELD embedding ON {table} TYPE array<float, {dimensions}>;
        DEFINE INDEX idx_{table}_org_source ON {table} FIELDS organization_id, source_id;
        """
    )


async def _insert_rows(
    db: Any,
    table: str,
    rows: Sequence[ChunkRow],
    batch_size: int,
) -> float:
    started = time.perf_counter()
    for start in range(0, len(rows), batch_size):
        batch = [asdict(row) for row in rows[start : start + batch_size]]
        await db.query(f"INSERT INTO {table} $rows;", {"rows": batch})
    return _elapsed_ms(started)


async def _run_queries(
    db: Any,
    table: str,
    query_samples: Sequence[QuerySample],
    config: VectorBenchConfig,
) -> list[QueryMeasurement]:
    sql = (
        "SELECT uuid, (1 - vector::distance::knn()) AS score "  # noqa: S608
        f"FROM {table} WHERE organization_id = $organization_id "
        "AND source_id = $source_id "
        f"AND embedding <|{config.candidate_limit}, {config.ef}|> $query_embedding "
        "ORDER BY score DESC LIMIT $limit;"
    )
    measurements: list[QueryMeasurement] = []
    for sample in query_samples:
        started = time.perf_counter()
        rows = await db.query(
            sql,
            {
                "organization_id": TARGET_ORGANIZATION_ID,
                "source_id": TARGET_SOURCE_ID,
                "query_embedding": sample.embedding,
                "limit": config.limit,
            },
        )
        latency_ms = _elapsed_ms(started)
        result_uuids = [str(row["uuid"]) for row in _rows(rows) if "uuid" in row]
        measurements.append(
            QueryMeasurement(
                query_index=sample.index,
                latency_ms=latency_ms,
                recall=recall_at(sample.expected_uuids, result_uuids),
                result_count=len(result_uuids),
                expected_count=len(sample.expected_uuids),
            )
        )
    return measurements


async def _surreal_engine_version(db: Any) -> str | None:
    for statement in ("RETURN version();", "SELECT version();"):
        result: object | None = None
        try:
            result = await db.query(statement)
        except Exception as exc:
            _error_text(exc)
        if result is None:
            continue
        version_value = _extract_scalar(result)
        if version_value:
            return str(version_value)
    return None


async def _table_info(db: Any, table: str) -> JsonObject | None:
    try:
        result = await db.query(f"INFO FOR TABLE {table};")
    except Exception:
        return None
    if isinstance(result, dict):
        return cast(JsonObject, result)
    rows = _rows(result)
    if not rows:
        return None
    return rows[0]


def _rows(result: object) -> list[JsonObject]:
    if not isinstance(result, list):
        return []
    rows: list[JsonObject] = []
    for row in result:
        if isinstance(row, dict):
            rows.append(cast(JsonObject, row))
    return rows


def _extract_scalar(result: object) -> object | None:
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    if isinstance(first, dict):
        return next(iter(first.values()), None)
    return first


def _url_for_index(config: VectorBenchConfig, index_name: str) -> str:
    if config.url:
        return config.url
    return f"surrealkv://{config.cache_dir / f'{index_name}.skv'}"


def _store_path_for_index(config: VectorBenchConfig, index_name: str) -> Path | None:
    if config.url:
        return None
    return config.cache_dir / f"{index_name}.skv"


def _path_size(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    if path.is_file():
        return path.stat().st_size
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())


def _redact_url(url: str) -> str:
    if "@" not in url:
        return url
    scheme, remainder = url.split("://", 1)
    return f"{scheme}://<credentials>@{remainder.split('@', 1)[1]}"


def _random_unit_vector(rng: random.Random, dimensions: int) -> list[float]:
    return _normalize([rng.gauss(0.0, 1.0) for _ in range(dimensions)])


def _perturb_vector(
    rng: random.Random,
    base: Sequence[float],
    dimensions: int,
) -> list[float]:
    return _normalize([base[index] + rng.gauss(0.0, NOISE_SCALE) for index in range(dimensions)])


def _normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return [0.0 for _ in values]
    return [value / norm for value in values]


def _content(content_bytes: int) -> str:
    repetitions = math.ceil(content_bytes / len(TEXT_CHUNK))
    return (TEXT_CHUNK * repetitions)[:content_bytes]


def _first_non_empty(*values: object) -> object | None:
    return next((value for value in values if value), None)


def _package_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _format_number(value: object) -> str:
    if value is None:
        return "n/a"
    if not isinstance(value, str | int | float):
        return str(value)
    return f"{float(value):.2f}"


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * MILLISECONDS_PER_SECOND


def _error_text(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:MAX_ERROR_LENGTH]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _positive_int(value: int, name: str) -> int:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
