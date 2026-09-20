# Assertion experiment final report review

PASS for publishing the bounded experimental result. No unresolved publication blocker found. This verdict verifies artifact joins, numerical claims and stated limits; it does not qualify production activation.

The reviewed report SHA256 is `0b8c3c6a36e7afe9834134e25be4895ab27faf7f28adc2bdb61e70975866bb92`. The changed benchmark README and `/tmp/jev-assertion-pr.md` were also reviewed. A subsequent link to this receipt does not change the evaluated claims.

## Independent checks

The independent scripts `/tmp/audit-jev-assertion.py` and `/tmp/audit-jev-assertion-semantic.py` use standard-library artifact reads, without importing the study implementation, calling providers or changing annotations. Numerical audit passed 2,961 retained and 10,190 fresh checks. All 320 raw provider response identities are unique. Local scheduled and DecisionRequest IDs are cohort-local, so joins include the cohort and manifest.

All 48 retained and 192 fresh raw critic outputs match the blinded responses and their exact mapping. All 53 retained and 127 fresh findings have corresponding annotation indices. Concern IDs refer to the correct assertion paths. Every mechanically valid finding binds the original assertion hash and citation keys. Coverage is independently derived only from fully valid findings; both mechanical errors receive zero applied findings, coverage and semantic-pass credit. All published paired comparisons reconcile, including newly missed concerns and diagnostic defect-target differences.

Fresh semantic passes are original direct/live/stress 21/27/13 and revised 27/31/17, each out of 32. Concern coverage is 10/12/7 versus 14/16/3, each out of 16. The direct instruction change has eight improvements and two regressions. Revised direct to revised live has four improvements and zero regressions, with no new missed concerns or defective targets in the frozen annotations.

The fresh revised-live sole failing path was inspected against both its revised-direct and original-live counterparts. The same unsupported causal objection already exists in both comparators; revised live drops the additional false-positive finding against the supported fan-speed assertion. This supports the report's cohort-specific observed no-new-harm statement. Defect-target counts alone would not establish that conclusion. Nine unsafe revised-stress paths were independently verified to flag only a supported neighboring assertion while omitting the actual concern.

Retained semantic passes are original 2/5/2 and revised 2/5/4, each out of eight. Retained revised-live still loses a previously covered concern. These diagnostic failures remain separate from fresh confirmation and are not hidden by aggregate totals.

The pre-unblind sensitivity items and packaged sensitivity tables reconcile. Fresh alternatives yield original 22/28/15 and revised 28/31/18; retained alternatives yield original 3/5/2 and revised 3/5/4. The original annotations remain unchanged and primary. Mechanical errors remain errors under sensitivity.

## Cost, latency and evidence boundaries

The cost tables, action counts, false accepts, supported accepts and rounded median/p95 values match raw accounting. Revised live versus revised direct increases fresh median latency 2.58% and cost 1.74%, while reducing p95 26.35%. The predeclared median-speed and cost targets fail. Wrong-hint robustness and the direct prompt's no-new-harm criterion also fail. The report does not claim activation, long-context validity, downstream task benefit or public-benchmark status.

Retained provider-reported cost is $0.194381488; fresh cost is $0.695284232; combined acquisition is $0.889665720. All failures stay in the denominators. Both invalid critic basis values, their $0.007781 combined cost, and the rejected Jev probability sum with $0.00005775 retained cost match raw receipts. Fresh natural-label accuracy distinguishes completed from scheduled assertions, including two unavailable labels after rejection.

The maximum fresh source text is 722 UTF-8 bytes; the largest serialized source packet is 848 bytes. The report correctly limits long-context extrapolation. Same-family review, earlier author/reviewer exposure, dependent repeats, separate cohorts, independently sampled live hints, excluded auth/publication work and shared-host timing are disclosed.

The retained devbox gate log records 391 passing tests and all three tasks completed. The independent preflight receipt records 59 selected checks including five adversarial probes. Read-only devbox comparisons confirm rows, summary, manifest, schedule and rubric are byte-identical between each live archive and its no-provider replay. Every local artifact link in the report resolves. Route claims are supported by the retained catalog and actual raw provider identities.

## Closed wording correction

The report initially said a stress output removed the correct grid assertion. No mutation occurred. The final wording says it recommended removing that assertion, accurately describing the finding disposition. No annotations, sources, provider receipts or experimental code were changed during this review.

## Immutable semantic evidence

- Retained annotation SHA256: `fd3250bf5d8212f9edc13f47527c99d6c8425a0aaac593abf5857cd4dd68b4c5`.
- Fresh annotation SHA256: `b63c236d93f5896e4255700b276927c352102ca0d4587c654c6f5ec04572807d`.
- Retained semantic audit SHA256: `295e02c5218cc97b082523e30f3cf99560f3727092473deee63768d89a40d60f`.
- Fresh semantic audit SHA256: `dc829fad03fd96bb58837328634f6e67f471289872941cc6bee225d70e49c74f`.

This verifier authored the fresh fixtures and had prior experiment exposure, but did not author the implementation or blind semantic annotations. The check is independent implementation/report verification within the same model family, not independent human gold or cross-family adjudication. No contact with the semantic reviewer occurred while its annotations were pending.
