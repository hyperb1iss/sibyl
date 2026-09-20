# Independent numerical audit: PASS

The retained archive supports the reported action counts, timings and provider-reported costs. This verdict covers numerical and structural integrity, not semantic finding quality or production qualification. The audit used a separate standard-library script, with no provider calls and no product edits.

| Measure | Direct fast critic | Fast critic with cached Jev hints |
|---|---:|---:|
| Scheduled and retained calls | 64 | 64 |
| Actions: accept / flag / error | 29 / 33 / 2 | 24 / 40 / 0 |
| Safe candidates accepted | 24 / 24 | 24 / 24 |
| False accepts | 5 | 0 |
| Strict author action matches | 47 / 64 | 52 / 64 |
| Frozen acceptable-action matches | 57 / 64 | 64 / 64 |
| Service median | 2254.752 ms | 2322.497 ms |
| Service p95, nearest rank | 4305.258 ms | 3722.285 ms |
| Fresh provider-reported cost | $0.278300 | $0.288742 |
| Output tokens | 11181 | 12188 |

The median increased 3.00456%; p95 decreased 13.54097%. The joint hypothesis that neither median nor p95 increases was not met. Fresh paid cost totals $0.567042. All 64 original Jev calls are attributed exactly once to the hinted arm, totaling $0.006124356 separately. Hinted fresh cost plus its attributed Jev cost is $0.294866356. Total fresh experiment cost plus prior Jev attribution is $0.573166356; the latter is not newly billed spend.

The two direct failures were both qs-016 repeats, with a schema-invalid basis value, unsupported_universal_claim. The calls returned HTTP 200, were billed $0.005596 and $0.005615, and remain in all 64-call cost, action and latency denominators. No unknown costs or missing token receipts were found. Completed-only direct median/p95 would be 2206.001/3956.748 ms; those conditional numbers do not replace the scheduled results.

The five direct false accepts cover four distinct cases: qs-019 repeat 1, qs-022 repeat 0, qs-024 repeat 0, and qs-031 both repeats. Two repeated outcomes on one fixture are not independent evidence of generalization. No finding semantics were adjudicated in this audit.

The audit performed 6048 checks, including the exact 32-case by two-repeat by two-arm grid, 128 unique rows and path receipts, 256 raw/dispatch artifacts, seeded interleaved schedule, exact wire hashes, forced tool and Anthropic Haiku route, decoded response bytes, original usage, derived actions, finding claim hashes and citation keys, timing containment and UTC order. All 128 actual requests differed within pairs only in the advisory slot; all 64 hinted slots matched original Jev raw labels and the identical prepared candidate/evidence state. No confidence or fixture-gold fields appeared in those structured hints.

The plan and live manifest, schedule and rubric were byte-identical. The four current runner hashes and 316 transitive source hashes matched the manifest. All 476 original-source artifacts matched their recorded hashes and exact inventory. These checks establish internal freeze consistency; file copies alone do not independently prove the historical wall-clock moment of preregistration. The raw provider fields report Anthropic and anthropic/claude-haiku-4.5 throughout; no independent invoice was queried.

The archive has no failure-denominator or bogus-hint-state defect that invalidates these numerical results. Cached hints exclude live Jev acquisition, source authorization and receipt finalization from measured latency. Findings and missed concerns still require the separate arm-blind semantic review. The same exposed 32 synthetic cases and two clustered repeats do not establish held-out quality, powered safety noninferiority or a production speed gain.

Evidence: /tmp/jev-fast-numerical-audit.json. Reproduction script: /tmp/audit_jev_fast.py. Inputs: /tmp/jev-fast-critic-20260920/live and /tmp/jev-quality-speed-20260920/live.
