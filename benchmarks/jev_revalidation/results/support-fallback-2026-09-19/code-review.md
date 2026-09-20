# Independent fallback qualification code review

Verdict: PASS for offline calibration and held-out qualification.
Date: 2026-09-19.
Reviewer: native same-family agent, separate from implementation.

## Scope and frozen files

Reviewed support_fallback_policy.py, test_support_fallback_policy.py,
support_fallback_study.py, test_support_fallback_study.py, and the new
jev-support-fallback Moon task. No repository source was edited. No
credentials or provider calls were used. Held-out gold and model results
were not read. Earlier annotation used only the stripped blind cases.

Local and devbox SHA-256 values matched:

- Policy: b482f63ea506521c585e0ec6b07f595d45fa9207cd35b574308d1839c749fbb8
- Study: a380521b6d6877ccadb8a3c2e35ad17282535146a87aee7c3facc3677f1064b7
- Study tests: ec92a68236128894b01603b14693d8c36150ed64fab8d1c2a73a9727c9e99e20

## Closed finding

The earlier candidate reducer omitted arm provenance, and the selector
assumed missing arms were grouped. The correction preserves actual arm
and requires explicit grouped calibration. Missing-arm and reduced
singleton inputs now fail selection. Six focused devbox tests verified
this closure before blind annotation.

## Independent executed verification

Host: devbox-stef-gradial-com-main.
Worktree: /home/dev/dev/worktrees/sibyl/nova/jev-support-fallback.

Command:
moon run root:jev-revalidation-test -- -k support_fallback

Result: 39 passed, 154 deselected, exit code zero.
Moon task receipt: bdbc8439; pytest duration 4.04 seconds.

The executed cases cover both-repeat safe coverage and highest-threshold
ties; any-repeat unsafe bypass rejection; reject-all; missing observations
and confidence; input-only numeric routing; exclusion of gold from routing;
receipt reduction and arm provenance; frozen threshold reuse; complete
call costs; no economic estimate with unsafe bypass or unknown cost;
identical threshold for guarded and confidence-only scoring; fixture,
route, program, product, schedule, receipt, and repeat tampering;
the exact historical-runner exception; modified selection fields even
with recomputed content hashes; held-out hash changes; calibration overlap
by ID or normalized evidence content; and fingerprint independence from
labels, ordering, and whitespace.

## Static assessment

Calibration regenerates scheduled requests and verifies all raw receipts
against archived calls using the repaired source-support preflight. The
historical exception changes only the allowlisted runner hash and is
explicitly recorded. Product preparation, prompt, route, and schedule
bindings remain enforced.

Selection uses only grouped rows and exactly two repeats per candidate.
The fixed threshold grid maximizes safe candidates bypassed in both
repeats, requires zero unsafe candidates bypassed in either repeat, and
breaks ties toward the highest threshold. No qualifying safe selection
produces reject-all.

Evaluation recomputes the selector from the original calibration archive
and checks code, artifacts, candidate rows, threshold table, and threshold.
The held-out file hash is bound at selection, and ID or normalized whole-
case reuse is rejected. This detects exact normalized reuse, not all
paraphrases or related evidence; corpus independence remains a protocol
responsibility.

Guarded and confidence-only methods share the selected threshold. Routing
uses completeness, support labels, reported confidence, and input text;
gold is used only for fitting or offline scoring. Failed measurements
remain fallbacks. Results include pooled, repeated, distinct-candidate,
and multi-assertion outcomes. Every routing call contributes its observed
cost, and missing costs remain unknown. The conditional break-even value
is absent with any observed false bypass, zero bypasses, or unknown cost.

The numeric rule is a conservative explicit-token filter, not arithmetic
verification. Confidence is not calibrated probability. The module adds
no production activation, memory write, or authority to skip the whole
critic. The review passes the experiment implementation, not the eventual
threshold, model quality, cost-saving claim, or deployment eligibility.
