# usage_rerank: P5 phase 1 feasibility harness

Measures how much labeled ranking signal Sibyl's v1.1 usage loop has actually produced,
and whether reordering by a usage prior would have helped. Read
[`FEASIBILITY.md`](FEASIBILITY.md) for the numbers and the verdict.

This harness only measures. It makes no production scoring change, and a change would only
follow a measured gate.

## Layout

| File | Role |
| --- | --- |
| `paths.py` | Connection and output settings, all env-overridable |
| `store.py` | Read-only SurrealDB HTTP client with a mutation denylist |
| `events.py` | `memory_usage_events` row model and normalization |
| `join.py` | Session grouping, rank recovery, feedback attribution, contamination bounds |
| `prior.py` | Usage-prior multiplier, point-in-time counts, rerank what-if, bootstrap and permutation tests |
| `age.py` | True `created_at` lookup for both item kinds, used to control for item age |
| `extract.py` | CLI: store to JSONL plus a summary receipt |
| `whatif.py` | CLI: JSONL to a what-if report |
| `out/` | Receipts from the 2026-08-03 dev-store run |

`out/` commits the summary and what-if reports. The row-level dumps
(`usage_events.jsonl`, `exposure_sessions.jsonl`) are gitignored, because they are
megabytes of one dev store's rows and `extract.py` rebuilds them in seconds.

## Running

```bash
uv run python benchmarks/usage_rerank/extract.py
uv run python benchmarks/usage_rerank/whatif.py
uv run python benchmarks/usage_rerank/whatif.py --interactive-only
```

`extract.py` reads a live content store. It is read-only twice over: every statement is
checked against a mutation denylist before it leaves the process, and the only statements
the harness issues are `SELECT`. Pointing it at a shared dev store is safe.

Re-summarize without touching the store:

```bash
uv run python benchmarks/usage_rerank/extract.py --from-jsonl benchmarks/usage_rerank/out/usage_events.jsonl
```

Environment overrides: `SIBYL_P5_SURREAL_HTTP_URL`, `SIBYL_P5_SURREAL_USERNAME`,
`SIBYL_P5_SURREAL_PASSWORD`, `SIBYL_P5_CONTENT_NAMESPACE`, `SIBYL_P5_CONTENT_DATABASE`,
`SIBYL_P5_OUT`. Note that `memory_usage_events` lives in the shared content namespace
(`sibyl_content`), not in a per-org graph namespace.

## Tests

```bash
uv run pytest tools/tests/test_usage_rerank_harness.py -q
```

98 tests, all on fixture events, no live store required. The fixtures reproduce the real
emitter's key shapes and microsecond timestamp behaviour on purpose, because the harness's
conclusions depend on those details.

## Three things worth knowing before extending this

**The `(session_key, message_key)` join does not work.** Exposure and citation session
keys cannot collide, because the citation digest folds `cited_ids` into its payload and the
exposure digest does not. Attribution goes through item identity plus time instead. The
extractor measures the overlap on every run so a future schema change that fixes this is
visible immediately.

**Recovered rank is within-kind only, and three sessions break even that.** The emitter
records raw_capture and graph_entity targets in two separate calls, so a session's raw
events normally all precede its graph events regardless of served order. Never compare a
recovered rank across kinds. Three sessions in the dev store show the kinds interleaving
instead of blocking, which means their writes overlapped and their timestamp order is not a
served order at all, so `run_whatif` drops any session without contiguous kind blocks.

**Two statistics answer two different questions.** `bootstrap_ci_vs_zero` asks whether an
arm beat the served baseline, which is the question a gate cares about.
`permutation_null` asks whether it beat a random prior of the same strength, and its
distribution is centred well below zero because any reordering degrades a good baseline, so
it is not a two-sided floor around zero. An arm can beat the null and still not beat zero.

**Use true `created_at`, not the first-event age proxy.** Item age confounds every
exposure-count comparison, and `first_seen_at` is asymmetrically censored: it truncates
long histories, and older items are the ones more likely to be truncated. `age.py` reads
real creation timestamps so `describe_candidate_priors` can standardize by age instead.

**Point-in-time counts are not optional.** Every usage count is computed from events
strictly before the session being scored. A version that skips this scores an item using
the citation it is trying to predict and reports a large, meaningless win.
