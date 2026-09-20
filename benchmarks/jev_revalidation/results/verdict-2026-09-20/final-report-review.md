# Verdict experiment final numerical and semantic report review

PASS. No publication blocker found in the reviewed report or packaged cohort evidence. The result supports stopping this verdict-format optimization and leaving default memory-validation activation off. The review does not qualify any critic replacement or production deployment.

Reviewed report SHA256: `d18ffe0a9ce3dc1ced5bea69a42985bb451a63d643f3ffebfff2fbb01bcf5ba0`. Scope-only additions linking this receipt and the separately frozen post-unblind diagnosis do not change the evaluated claims.

## Exact evidence joins

The independent local standard-library numerical auditor verified 5,594 retained checks and 10,671 fresh checks. It imports no study implementation. Complete request, route, prepared evidence, assertion hash, citation, hint, raw-response and dispatch bindings reconcile. Its independent verdict projection verifies every supported citation, unique assertion coverage and concern target, preserves inability reasons, and rejects a whole malformed response without salvage.

The independent semantic auditor verified all 288 raw-response-to-blind-map-to-annotation joins, all 218 finding annotations and all 283 verdict annotations. The count includes explicitly labeled malformed-string diagnostics without converting those strings into executable results. Primary finding coverage is recomputed from fully valid findings at the exact frozen concern target, with original citations. All four mechanically invalid critics have zero applied finding coverage and zero projected, raw or overall semantic credit.

The raw and consumer-facing surfaces remain separate. Overall pass is the conjunction of their independently recomputed pass predicates. Baseline raw and projected scores coincide. Four retained verdict outputs pass consumer review but fail their complete raw rationale review; the fresh cohort has no such whole-output divergence. Raw supported/unable text is not silently removed from review. The separate new-raw-defect diagnostics in the audit prevent interpreting a finding-only target comparison as a complete raw no-harm test.

## Primary and sensitivity results

Fresh overall passes are prefix direct/live/stress 22/29/14 and verdict 26/27/22, each out of 32. Valid concern coverage is 14/15/7 versus 14/14/13, each out of 16. Direct format change has six paired improvements and two regressions; live schema change has zero improvements and two regressions. The verdict Jev increment has three improvements and two regressions. Prefix direct to live has seven pass improvements and no pass regression but a newly missed concern and new defective consumer target on an already-failing path. The report correctly refuses a no-new-harm conclusion from those binary transitions.

Retained overall passes are prefix 11/13/6 and verdict 7/11/6, each out of 16. Retained verdict consumer passes are 9/12/7. The direct schema comparison has four overall regressions, two affecting projected output; the live schema comparison has two overall regressions, one affecting projected output. A retained misleading verdict falsely accepts one unsafe assertion. The report preserves those failures alongside fresh gains.

All five retained and eight fresh pre-unblind sensitivity overrides were independently applied to untouched copies. Every original field value matches the declared from-value; only the exact listed field flips and alternative scores change. Metadata remains unchanged apart from the explicit sensitivity provenance record. The independently recomputed sensitivity summaries match the packaged summaries: retained prefix 11/13/6, verdict 9/12/8; fresh prefix 22/30/14, verdict 28/29/23. No primary annotation changed. The simpler live policy remains stronger under both scoring treatments.

The root aggregation code's new_defect_targets metric covers consumer findings, not every extra raw rationale clause. Its stated diagnostic limits are necessary and preserved. The report makes no broader no-harm claim from that metric. The post-unblind prose diagnosis is separately identified rather than presented as blinded scoring.

## Accounting, timing and failure claims

The experiment dispatched 384 calls and observed 383 distinct provider response IDs. Retained cost is $0.486670070 known plus one unknown Jev billing amount; fresh cost is $0.974310400 known; combined known spend is $1.460980470 plus the unknown amount. No complete all-call billed total is inferred. Dispatch evidence proves a host attempt, not vendor receipt or billing of the timed-out request.

All reported table values and relative median, p95 and cost changes reconcile with raw usage and complete scheduled denominators. Fresh direct explicit verdicts cost 52.87% more and increase median latency 33.06%. Compared with prefix plus Jev, verdict plus Jev costs 62.51% more and has lower semantic pass count. Median direct critic output is 267.5 prefix tokens versus 470 verdict tokens. These results support the bounded stopping decision rather than a production-general claim.

The retained timeout lasts approximately 30 seconds and its full fallback path takes 33.063 seconds. The unknown bill and tail event remain visible. The other rejected Jev response has probability mass 0.99 and receives empty-hint fallback. The four critic failure categories match raw output: duplicate verdict, two arrays returned as strings, and an invalid basis enum. Every observed critic response reports finish_reason=tool_calls; none reports token truncation. No repair or paid retry appears in the complete raw inventory.

Host-recorded service windows do not overlap. The fresh first path begins 0.865582 seconds after the last retained path ends. Provider request windows also have zero cross-cohort overlap. Shared-host conditions and omitted retrieval/auth/publication work remain explicit limitations; the small-sample p95 is descriptive, not an SLO or rare-event bound.

## Packaging and verification provenance

Both packaged cohort copies match their original cases, rubric, blind map, primary review, primary summary, exact sensitivity overrides, alternate review and alternate summary byte-for-byte. All relative report links resolve. Primary annotation SHA256 values remain:

- Retained: `20f7bcd8101dfb664bb2b7cdcc02fa4b89b87d2a3aaa9aadd4447e5237ad1db7`.
- Fresh: `ef84608d06a8f0ea6a6fdfcaee3af72a47347c767b82fada52a97ce16a5eb8a2`.

Read-only devbox log checks confirm 425 passing tests with all three gate tasks complete, six compatibility probes, and two root spot-check commands with two checks apiece. Byte comparisons on the devbox confirm rows, summary, manifest, schedule and rubric are identical between each live archive and its no-provider replay. Local scheduled IDs are cohort-scoped; their reuse is disclosed and never mistaken for duplicate provider responses.

The report correctly identifies the baseline as the preceding assertion-prefix critic, the intervention as schema plus instructions, and the review as contract-visible/hint-mode-blind. Prior author/reviewer exposure, dependent pairs and repeats, separate retained/fresh cohorts, and the absence of downstream or publication measurements are explicit. The current source-support shadow default remains false. No source, fixture, annotation or provider receipt was changed during this audit.

## Audit receipts

- Retained raw audit: `/tmp/jev-verdict-retained-numerical-audit.json`, SHA256 `74ed32dd1a0377cc9773755b72b150b8a1b94e81c2a197a7441a25f8918b7b3f`.
- Fresh raw audit: `/tmp/jev-verdict-fresh-numerical-audit.json`, SHA256 `d934cd53ac14d97e7ea735ac398256f95502285dca58a9df3a87e666bcd00dc1`.
- Retained semantic audit: `/tmp/jev-verdict-retained-semantic-audit.json`, SHA256 `a0543e00db8664c1a7c915107b6a5012e53895604a5dd5f45ebceb59756cd3fe`.
- Fresh semantic audit: `/tmp/jev-verdict-fresh-semantic-audit.json`, SHA256 `1b7593e6bb1849bd9b688e58d4522e7ac23d02b6cd1203f2543a404a01df560a`.
- Exact sensitivity audit: `/tmp/jev-verdict-sensitivity-audit.json`, SHA256 `079b67ddb8b1fb0350315548ea7321d48f9ef26a05647212e11f850741fa28f8`.

This verifier authored the fresh fixtures and has historical experiment exposure, but did not author the implementation or blinded semantic annotations. The work independently verifies implementation outputs and aggregation within the same model family; it is not human gold, a new semantic annotation or cross-family review. No contact with the semantic reviewer occurred while its current annotations were pending. All new work in this lane was read-only artifact analysis, with no provider calls or test reruns.
