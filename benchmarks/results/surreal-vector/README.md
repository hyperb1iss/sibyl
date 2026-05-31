# Surreal Vector Index Benchmarks

This directory stores citable artifacts for SurrealDB vector-index experiments
against Sibyl's `document_chunks.embedding` workload.

Run a local embedded smoke benchmark:

```bash
moon run bench-vector-index -- --rows 1000 --dimensions 128 --queries 20
```

For an adoption-grade run, increase `--rows`, `--dimensions`, and
`--content-bytes` until the generated corpus exceeds the memory profile of the
target deployment. The artifact records the Surreal runtime, dataset shape,
index definitions, org/source filters, recall, p50/p95 latency, build time, and
disk footprint.

The benchmark mirrors the production scalar-prefilter query shape:
`organization_id`, `source_id`, then the KNN operator. If SurrealDB returns an
empty HNSW baseline for that shape, treat the run as a query-planner finding
rather than as evidence for adopting DiskANN.

The production schema stays on HNSW unless a saved benchmark shows DiskANN
preserves recall while improving the target-workload p95 without an excessive
build-time penalty.
