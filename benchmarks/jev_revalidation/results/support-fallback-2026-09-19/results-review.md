# Independent fallback results and protocol review

Verdict: PASS after two report corrections.
Date: 2026-09-19.
Reviewer: native same-family agent, separate from implementation and the
raw-receipt numerical-audit lane. No code or original artifacts changed.

## Reviewed evidence

Reviewed SUPPORT_FALLBACK_RESULTS.md, the retained compact artifacts under
results/support-fallback-2026-09-19, and the held-out fixture. Gold was
opened only after my blind annotation had been frozen separately.

Independently checked the selection content digest, selection file hash,
held-out fixture binding, evaluation artifact hashes for manifest/calls/
schedule, final design-freeze source hashes, the superseded freeze hash,
and the blind-annotation hash. All checked bindings agree. The compact
archive deliberately omits full request and raw response receipts; the
report points to their devbox archive, independently audited in another
lane. Credential-pattern scanning found no credentials in the compact
JSON artifacts or report.

## Numerical and claim checks

Retained call records independently sum with Decimal to:

- Grouped: 128 completed calls and attempts, USD 0.007106484.
- Singleton: 256 completed calls and attempts, USD 0.013163556.
- Combined: 384 completed calls and attempts, USD 0.020270040.

The selected threshold is 0.99. Only calibration cases support-009 and
support-012 are wholly supported and eligible after numeric deferral;
both have confidence 0.99 in both repeats. The retained grid selects the
highest tied qualifying threshold as specified.

The held-out fixture has 24 wholly supported and 40 unsafe candidates;
16 wholly supported candidates have multiple assertions. All unsafe
candidate rows already contain a non-supported prediction before
confidence or numeric gating. The report explicitly avoids attributing
error prevention to confidence.

The report table matches retained evaluation fields: bypasses 33, 35,
34, and 36 out of 128 for grouped guarded, grouped confidence-only,
singleton guarded, and singleton confidence-only. All four have zero
unsafe bypasses. The primary retained reasons are 80 unsupported,
12 numeric-text, and three low-confidence observations.

Both prediction sets have ten incorrect labels out of 256 assertions,
including four false supports on the two arithmetic cases repeated twice.
Their other unsupported assertions prevent whole-candidate bypass, as
reported. The report preserves the distinction between assertion failures
and candidate-level success.

The primary conditional break-even value is USD 0.007106484 / 33,
or USD 0.000215348. The report conditions that figure on equivalent
behavior and unmeasured avoided support-check costs. It makes no claim
of observed full-evaluation savings, critic equivalence, calibrated
confidence, or production activation.

## Closed report corrections

The original sentence excluding all identifiers from provider input was
too broad: source and assertion identifiers are intentionally retained
for evidence binding. The revised wording distinguishes those IDs from
fixture identifiers and keeps gold/category exclusions precise.

The reproduction flag now correctly reads --holdout-cases-sha.
Both corrections were verified in the final local report.

## Protocol and limitations

The report discloses that calibration was previously inspected, repeats
and assertions are correlated, and blind annotation used the same model
family. The sole annotation difference remains non-supported in both
annotations and changes no candidate-supported classification.

The immutable inherited summary retains a stale mixed-only multi-claim
limitation. The report explicitly identifies and corrects that sentence
without rewriting original output. Exact normalized case reuse detection
is not represented as detecting paraphrases.

The lexical numeric rule is described as conservative deferral, not
arithmetic verification. The proposed next step remains paired evaluation
with the full critic active. No publication or full-critic bypass is
licensed by this result. The implementation and freeze protocol were
independently reviewed before the held-out live execution; this review
does not extend the small synthetic result into deployment safety.
