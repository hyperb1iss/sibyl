# Source-support qualification

Grouped questions reduced the measured cost of the same synthetic source-support workload by 39.2%, from $0.007886424 to $0.004793712. The existing source-support adapter is still unsuitable for skipping the paid semantic critic: it falsely supported an incorrect subtraction and an assertion backed by conflicting measurements.

The experiment ran 256 calls on `stef-gradial-com-main` for $0.012680136. All calls completed with observed usage. Only synthetic evidence was sent to Jev, with no replacement calls or production memory changes. These measurements establish source-support call costs, not savings on a complete evaluation run.

## Results

A fresh corpus contains 48 synthetic candidates and 80 indexed assertions. Sixteen candidates have three assertions; the remaining 32 have one. Both request geometries ran twice against the same original evidence and frozen product questions.

| Measurement | Grouped by candidate | One assertion per call |
| --- | ---: | ---: |
| Calls | 96 | 160 |
| Correct assertion labels | 153/160 (95.6%) | 154/160 (96.3%) |
| False support among unsupported assertions | 4/112 | 3/112 |
| Unsafe candidates cleared | 2/80 | 1/80 |
| Clearance precision | 16/18 (88.9%) | 16/17 (94.1%) |
| Fully supported candidates cleared | 16/16 | 16/16 |
| Input tokens | 114,136 | 187,772 |
| Provider-reported cost | $0.004793712 | $0.007886424 |
| Median call latency | 137 ms | 137 ms |
| 95th-percentile call latency | 282 ms | 266 ms |

A candidate clears only when every indexed assertion completes and is labeled supported. Clearance is an offline diagnostic, without publication authority. Failed assertions would remain in the accuracy denominator, but this run had none. Latency describes provider calls, not end-to-end evaluation latency.

The grouped arm scored 76/80 and 77/80 across repeats. The singleton arm scored 77/80 in both. Each repeat reuses the same cases, and assertions within a candidate share evidence; the pooled outcomes are not independent safety samples.

## Failure analysis

The incorrect subtraction case states that a shelf held 18 jars and exactly five were removed. Jev supported the claim that 14 remained in three of four calls. The grouped label on this single-assertion candidate has the same semantic input as the singleton label, so the difference does not establish a grouping effect. Arithmetic belongs in deterministic checks.

The conflicting-measurement case provides two active signed sheets for the same sample and session: one says 20 grams, the other 25. Both geometries supported the 20-gram assertion in both repeats. Another unsupported assertion prevented the whole candidate from clearing. Candidate-level safety therefore conceals this assertion-level failure.

Both geometries also called an unproven universal contradicted rather than insufficient. An unresolved parcel reference alternated between ambiguous and insufficient. All three cross-geometry label disagreements occurred in single-assertion cases with identical semantic inputs. No label difference was observed on the multi-assertion candidates.

On multi-assertion candidates alone, both geometries scored 92/96 with identical labels. Grouping reduced their cost from $0.005448156 to $0.002355444 (56.77%).

These failures identify two requirements for further integration: retain deterministic numerical validation, and qualify explicit handling of conflicting evidence. False-support answers carried provider confidence from 0.51 to 0.66. These scores are uncalibrated; a cutoff selected after inspecting these cases would need fresh evaluation before supporting any routing decision.

## Experimental controls and limits

The fixture adapter calls the existing reflection-validation and source-support preparation owners. Evidence remains original UTF-8 bytes, with byte citations, explicit provenance, and source-observation bindings. The questions use the adapter's unchanged instructions and options. Gold labels, categories, and rationales never enter provider input.

Grouped requests ask every indexed assertion together. Singleton requests select one question and its matching host subject while preserving the entire state, including sibling candidate assertions. A seeded schedule interleaves both geometries. Source support must come from original evidence; sibling assertions are not corroboration.

A separate agent authored the corpus without reading prompts or results. Another agent annotated the 80 assertions without seeing gold labels, categories, rationales, or results and agreed on every label. The annotator shared the model family, so agreement does not establish human gold or independent-family validation.

All sixteen multi-assertion candidates contain an unsupported assertion. The eight wholly supported candidates have one assertion each. The reported coverage therefore says nothing about rejection of valid multi-assertion candidates. The corpus also omits projected evidence, procedures, long production packets, and downstream answer quality.

## Evaluation cost implications

The current screen48 configuration pins memory work to Opus. Retained historical receipts report $273.268480 across 149 memory execution rows, combining proposal generation, critique, and correction. The receipts do not isolate critic-only spending. The solver receipts report $5.56557994 across 47 known-cost cells, plus one cell with unknown cost. Reservations are not billed spend.

Source support addresses part of the expensive validation stage, but the critic also evaluates scope and causality and produces cited findings for correction. The current shadow integration adds spending while that critic still runs. Neither the 39.2% grouping reduction nor the low absolute Jev cost establishes a complete-evaluation saving or permission to omit the critic.

## Evidence and reproduction

The committed evidence directory, `results/source-support-2026-09-19/`, contains manifests, the schedule, assertion outputs, call accounting, blind annotations, and the exact executed runner source. Full request/observation receipts and verification logs remain at:

```text
stef-gradial-com-main:/home/dev/dev/eval-runs/jev-support-qualification-20260919
```

The live run used runner SHA-256 `8020c00ba15f487695961ffa5ad8761d015a52b331ef6e3ad78c66c44a37f96a`. Predictions, call accounting, and summary replayed byte-for-byte with that version. Independent review then found that a damaged replay archive could lose known original accounting while appearing dispatched. The corrected runner rejects missing, invalid, or inconsistent receipts before creating replay output. The original run and manifest remain immutable; the archived executed source explains the manifest's earlier runner hash.

The corrected implementation passed 154 tests, lint, and typecheck on the devbox. Independent review passed 17 targeted tests and validated all 256 original receipts with the corrected checker. Root spot-checked 21 tests and the lint/typecheck gates. A separate numerical audit reconstructed every request digest, observation, and prediction from the raw receipts. Both reviews used the same model family.

Use fresh output directories. A live run requires the dedicated Decisions credential; preparation and replay make no provider calls.

```sh
moon run root:jev-support-study -- --output-dir /absolute/prepared
moon run root:jev-support-study -- --output-dir /absolute/live --live
moon run root:jev-support-study -- --output-dir /absolute/replay \
  --replay /absolute/live
moon run root:jev-revalidation-test root:jev-revalidation-lint \
  root:jev-revalidation-typecheck
```

Replay requires the same program hashes as its source run. Replaying the archived experiment requires its preserved runner version; the corrected runner applies to new runs and rejects damaged archives.
