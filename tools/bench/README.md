# Benchmark evidence retention

Use `moon run bench-evidence` to preserve explicitly selected inputs and results before CI
artifacts expire. The command reuses Sibyl's deterministic package archive format and writes a
local object named by its SHA-256. It does not upload files, change a benchmark verdict, or certify
that an experiment is complete.

## Preserve and move an experiment

Choose the files needed to reconstruct the result: the exact dataset, per-question report,
configuration, lockfile, source revision, and any prompts or provider-accounting receipts stored
separately. Include frozen memory for experiments that depend on derived state. Missing inputs
remain missing; the archive does not infer them from an aggregate report.

```bash
moon run bench-evidence -- preserve \
  --store /path/to/private/evidence \
  --file inputs/corpus.json=/path/to/corpus.json \
  --file results/report.json=/path/to/report.json \
  --file source/uv.lock=/path/to/pinned/uv.lock \
  --file source/identity.json=/path/to/source-identity.json
```

The JSON result contains the bundle path and digest. Copy the bundle through the normal private
file-transfer channel, retaining that digest separately. Verify it on the receiving machine:

```bash
moon run bench-evidence -- verify \
  --bundle /path/to/bundle.tar.gz --sha256 <recorded-digest>

moon run bench-evidence -- restore \
  --bundle /path/to/bundle.tar.gz --sha256 <recorded-digest> \
  --destination /path/to/new/experiment-inputs
```

Restore requires a destination that does not exist, inside an existing trusted parent directory.
Verification checks the transport digest and each archive member before creating the destination.
A failed write removes only the directory created by that restore call, permitting a retry.
Concurrent readers should consume the restored directory only after the command succeeds.

The existing archive codec holds the selected files in memory and restricts archive member names
to 100 UTF-8 bytes. Use short logical names. It is suitable for retained experiment bundles; it is
not a streaming database backup mechanism. Files are retained exactly as supplied. Exclude
credentials and redact private dogfood material before bundling; keep unredacted evidence in an
appropriately restricted store. Local persistence does not replace an independent backup.

## Remote eval handoff

A restored bundle is input to an existing runner. The live LongMemEval runner expects a disposable
Sibyl API with its backing SurrealDB available; it does not provision a host. Match the pinned
experiment's API, embedding, worker, and extraction settings. The harness creates an isolated tenant
through local signup, so use an eval instance configured for that workflow. Keep provider credentials
in the remote environment, never in the archive.

Inspect the runner's current flags before launching:

```bash
moon run bench-longmemeval-live -- --help
```

First verify the restored dataset digest and run deterministic fixture checks. Then prove ingestion
and retrieval capacity before authorizing a reader/judge campaign. A conversational result and an
official V2 result have different reader, judge, dataset, latency, and accounting contracts; preserve
their receipts separately.
