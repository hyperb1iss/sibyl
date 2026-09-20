# Independent quality and speed raw audit

The raw audit passes for integrity and accounting. The experiment misses its registered diagnostic target: routing costs less and preserves the measured action outcomes, but fails both speed gates. No production qualification follows.

All 128 scheduled paths are present, with 64 paths per arm and 171 original provider receipts. Every fixture, code, manifest and pre-call freeze hash matches. The audit reconstructed actions, routing, costs, and timing bounds without importing the scorer or reading `summary.json`.

| Measure | Complete critic | Jev then critic |
| --- | ---: | ---: |
| Paths | 64 | 64 |
| Accept / flag / abstain / error | 24 / 40 / 0 / 0 | 24 / 40 / 0 / 0 |
| Predeclared rubric correct | 64 | 64 |
| Original strict action correct | 52 | 52 |
| Unsafe or insufficient accepted | 0 / 40 | 0 / 40 |
| Safe accepted | 24 / 24 | 24 / 24 |
| Useful assertion-evaluations available | 42 | 42 |
| Unsafe assertion-evaluations exposed | 0 | 0 |
| Original provider calls | 64 | 107 |
| Known observed cost | $1.773265 | $1.236339356 |
| Unknown cost calls | 0 | 0 |
| Service median | 4.463886 s | 4.456487 s |
| Service p95 | 6.931712 s | 7.822884 s |

Routing bypasses 21 critic calls and runs 43 fallbacks. Fallback reasons are 40 unsupported candidates, two numeric-text deferrals, and one confidence deferral. All 64 Jev calls cost $0.006124356; the 43 fallback critics cost $1.230215. The measured cost reduction is $0.536925644, or 30.28%. The run spends $3.009604356 across both arms.

There are no paired safe-candidate losses and no action changes between arms. All six originally abstain-labeled cases receive flag on both repeats in both arms. Flag was admitted for those cases in the frozen pre-call rubric; consequently rubric accuracy and strict original-action accuracy differ. Rubric-level agreement does not establish the semantic correctness of every critique.

The aggregate median improves by only 7.4 ms, far short of the required 20%. The p95 increases by 891.2 ms, or 12.86%. Quantiles use the frozen experiment's arithmetic median and nearest-rank p95. Queue time is reported separately in JSON and is not interactive product latency.

## What the raw timing establishes

Every fallback critic dispatch occurs after its Jev call completes. Every path's service time covers its raw sequential stage durations, and total time covers queue plus service. All original per-path timing receipts match the combined rows.

All 21 bypass pairs are faster than their corresponding baseline calls. Their median service falls from 1.813407 s to 0.148926 s; the median paired improvement is 1.605651 s.

Of 43 fallback pairs, 36 are slower. The median paired increase is 391.170 ms. Across fallback pairs, the mean added service time is exactly decomposed as follows:

| Component | Mean change |
| --- | ---: |
| Serial Jev call | +163.266 ms |
| Fallback critic duration relative to its paired baseline critic | +132.248 ms |
| Harness overhead difference | +2.511 ms |
| Total | +298.024 ms |

The serial Jev overhead is directly measured. The entire slowdown cannot be attributed to that overhead: the independently executed critic calls also vary. The case setting both aggregate p95 values (`qs-016`, repeat 0) gains 891.173 ms, comprising 222.676 ms of Jev time, 665.303 ms of critic-duration difference, and 3.194 ms of overhead.

| Evidence band | Pairs | Baseline median / p95 | Routed median / p95 | Bypasses |
| --- | ---: | ---: | ---: | ---: |
| Around 1 KB | 40 | 4.464 / 6.739 s | 4.696 / 7.132 s | 11 |
| Around 8 KB | 16 | 4.763 / 6.932 s | 4.318 / 7.823 s | 6 |
| Around 24 KB | 8 | 3.459 / 11.107 s | 2.563 / 11.938 s | 4 |

The JSON includes complete route-conditioned and length-conditioned timing, repeat metrics, and each paired decomposition. Small strata and two correlated repeats are descriptive evidence, not population tail estimates.

## Scope and reproduction

Timing begins with prepared evidence and ends at the validation action. Evidence acquisition, candidate generation, corrections, publication, retrieval, and answer generation are excluded. The availability calculation is deterministic accept/withhold accounting; no downstream retriever or answer generator ran. The corpus contains synthetic plain reflections, not procedures. The confidence threshold remains 0.99 and was not refit.

Reconstruct the audit locally:

```sh
python3 /tmp/jev-quality-speed-20260920/raw_audit.py > /tmp/jev-quality-speed-20260920/raw-audit-output.json
```

The standalone standard-library script verifies the exact schedule and complete grid, original dispatch/request/response byte bindings, product prompt/schema, source bytes/provenance/citation ranges, strict Jev outputs and confidence routing, critic findings and mechanical validation, cost sums including failures, and path timing bounds. It accepts `--live`, `--repo`, and `--out` for equivalent archived locations. The JSON records the manifest and script hashes. No experiment files or receipts were changed.
