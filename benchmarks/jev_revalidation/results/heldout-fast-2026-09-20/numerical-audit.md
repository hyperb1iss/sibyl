# Held-out numerical audit: PASS

Independent standard-library numerical/structural audit; no semantic review, provider calls or product changes. PASS is receipt correctness, not quality/speed qualification.

| Arm | Paths | Actions | Safe accepted | False accepts | p50 ms | p95 ms | Total reported cost USD |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| direct | 48 | {'accept': 22, 'flag': 23, 'error': 3} | 16/24 | 6 | 2465.652 | 4954.370 | 0.170856 |
| live_jev | 48 | {'accept': 17, 'flag': 30, 'error': 1} | 17/24 | 0 | 2500.727 | 4963.481 | 0.171059092 |
| misleading | 48 | {'accept': 10, 'flag': 37, 'error': 1} | 8/24 | 2 | 3034.467 | 5146.462 | 0.193404 |

Primary live-Jev/direct ratios: {"service_ms_p50": 1.014225566896985, "service_ms_p95": 1.0018390500647592, "cost": 1.0011886735028328}.

All-arms provider-reported acquisition: $0.535319092 known; 0 unknown-cost calls. All failures remain in scheduled denominators.

6770 checks; 0 failures. Detailed checks, artifact hashes, action harms and unknown usage are retained in /tmp/jev-heldout-numerical-audit.json.

- Stage latency includes fresh preparation, serial Jev where applicable, critic and local validation; excludes retrieval, authorization and durable publication.
- Stress labels are injected diagnostics, not Jev observations; stress arm is separate from primary cost/timing comparison.
- 12 minimal-pair clusters and dependent repeats, not 144 independent examples.
- Semantic finding quality requires a separate blinded review.
- Provider-reported usage is retained; no invoice independently queried.

Natural Jev labels match fixture gold on 80/84 assertions. All labels match in 44/48 live paths.

all_hints_match_fixture: 44 paths across 22 unique cases; direct/live action correctness 29/37; paired improvements/regressions 8/0.

any_wrong_hint: 4 paths across 2 unique cases; direct/live action correctness 3/3; paired improvements/regressions 0/0.

These post-call correctness strata are descriptive. They do not establish a causal effect of correct versus wrong hints, or semantic finding validity.

The primary median, p95 and cost gates all failed in this run. No primary new false accept or lost safe accept occurred. The misleading-label arm lost eight safe accepts relative to direct across five unique cases; its two false accepts already occurred in the direct arm. Action-level stress harm is a robustness veto under the frozen protocol.
