# Claim revalidation experiment

Measure whether Jev recognizes changes to an existing claim, and whether a separate policy would allow retirement. The harness writes experiment receipts only. It does not access Sibyl memory or apply lifecycle changes.

Read the [measured results](RESULTS.md) before selecting a prompt. The first 84 cases favored direct Choice; a prospective 56-case comparison improved decomposition and exposed a false retirement in both direct prompts. The committed evidence includes per-case predictions and call accounting.

The [original-source and validity study](SOURCE_VALIDITY_RESULTS.md) adds 56 cases with full source passages and typed temporal policy. Source-only assessment scored 98/112 dispositions; summary agreement reduced that to 64/112. Those scores use supplied authority, claim kind, and validity metadata. The study provides preparation and analysis commands, preserved failures, and per-call accounting.

The [claim-kind ablation](CLAIM_KIND_RESULTS.md) isolates one supplied metadata value with an otherwise identical prompt. On 48 fresh cases repeated twice, correct kind raised temporary-overlay recall from 17/24 to 20/24 and policy agreement from 87/96 to 89/96. The study includes matched evidence pairs, annotation sensitivity, and a separate diagnostic replication.

The fixture contains 84 original synthetic pairs authored separately from the prompts. The prompts and scoring protocol were frozen before the prompt author opened the cases. Labels are agent-authored diagnostic expectations, not human annotations or a public benchmark. A separate blind annotation checks agreement without seeing the original labels or Jev answers.

The comparison includes:

- The existing pairwise reflection heuristic, invoked with an incoming claim and one prior memory. Its outputs are proposals, and its semantic coverage is narrower than the model arms.
- One seven-way Choice question per pair.
- Four Choice questions per pair (event form, scope, effect, replacement), combined by fixed code.

The decomposed arm uses Choice labels. It does not reproduce Invalidate's Noul probabilities or import its thresholds. Source authority and effective timestamps stay outside the model request. Ground-truth labels, categories, and rationales never enter provider input.

A proposed contradiction or replacement permits simulated retirement only when the event is authoritative and demonstrably newer. An older event retains the memory. Missing, equal, or untrusted chronology routes the proposal to review. The first experiment compares singleton and seven-pair batches, each repeated twice, without tuning prompts against the results.

## Run

All tasks run from the workspace root through Moon:

```sh
moon run root:jev-revalidation-test
moon run root:jev-revalidation-lint root:jev-revalidation-typecheck
moon run root:jev-revalidation -- --help
```

Live runs require the dedicated `SIBYL_DECISION_OPENROUTER_API_KEY` environment variable and the explicit `--live` switch. Use a new output directory for each run. The adapter retains its pinned model, provider, privacy routing preferences, deadline, and no-retry behavior.

```sh
moon run root:jev-revalidation -- \
  --cases benchmarks/jev_revalidation/cases.json \
  --out /absolute/path/to/new-run \
  --arms direct,decomposed --batch-size 1 --repeats 2 --live
```

Preserve the manifest, requests, observations, predictions, and reports together. Failed requests remain in case denominators. Usage fields absent from provider responses remain unknown. Call latency describes the chosen request geometry, not end-to-end search latency. Repeated observations of the same case are not independent samples.

The default prompt version is `v1`. To reproduce the second comparison, use `--cases benchmarks/jev_revalidation/holdout.json` and run each of `--prompt-version v1` and `--prompt-version v2` into separate output directories. V2 keeps the same relation composition and policy, changing only scope and current-state wording.

For offline replay, replace `--live` with `--replay /absolute/path/to/original-run` and retain the original cases, version, arms, batch size, and repeats. Replay validates request identities and prompt hashes. Its usage and latency describe the original calls, not new network activity.

## Evidence limits

The pairwise heuristic invocation is a controlled comparison. It bypasses extraction and supplies `kind="claim"` to make that heuristic eligible; it is not a deployed end-to-end baseline. Duplicate detection concerns the incoming candidate, while contradiction findings ask for review. Neither is an automatic retirement of the prior memory.

Even a perfect score on these cases would not establish calibration or a benefit to reader answers. A later study must measure lost historical evidence, retrieval of replacement evidence, and downstream answer accuracy on independent data. Real-data activation still requires provider/account privacy acceptance and explicit cohort authorization.

The inspiration is [Invalidate](https://github.com/chopratejas/invalidate/tree/d6ade60108b8064bafaee425fd8f9e78683dbd82). Its event cursors and isolated retirement rechecks are useful experiments; Sibyl keeps its existing correction and source-integrity owners.

## Background readiness

The [offline background experiment](BACKGROUND_READINESS_RESULTS.md) asks how much
speed the frozen quality-speed routing rule could gain if Jev answers were ready
before validation. Even its ideal ceiling improves median latency only 4.88% on
that corpus. The runner validates the retained archive, uses paired baseline
critic samples for misses and accounts for late background calls. It has no live
mode and does not enable product bypass.

## Fast critic with Jev hints

The [paired fast-critic study](results/fast-critic-2026-09-20/report.md) compares
128 fresh Haiku calls with and without cached Jev hints. Hints removed five false
accepts and improved reviewed concern coverage, but introduced two paired semantic
regressions. Median critic time rose 3.00%; full Jev acquisition is not measured.
The retained archive includes frozen inputs, per-call outcomes and blind review.

## Fresh cases with live Jev acquisition

The [held-out challenge study](results/heldout-fast-2026-09-20/report.md) adds
24 fresh cases, actual Jev acquisition and intentionally wrong hints. Live hints
improved blind semantic passes from 22/48 to 38/48 at nearly unchanged latency
and cost, with one paired semantic regression. Wrong hints reduced supported
acceptance sharply. The separate quality, speed and robustness gates prevent
treating the aggregate improvement as production qualification.
