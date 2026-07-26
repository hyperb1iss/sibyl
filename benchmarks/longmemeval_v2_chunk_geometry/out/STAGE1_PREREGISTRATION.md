# Stage 1 pre-registration — written BEFORE any Stage 1 result was computed

Timestamp of writing: before `stage1.py` was executed for the first time.
Author: measurement agent, Sibyl v1.2 A1 de-risking.

## Hypothesis under test (the "free ride")

The 82.6% selection ceiling was measured over fat states. Fat states are easy to
select because they contain a lot of text, so *something* in them matches the
query and the gold literal rides along for free. Slicing may destroy that free
ride: a slice that holds the gold literal but shares no query vocabulary becomes
a needle in a much larger haystack. If that is what happens, the known residual
(gold sharing no question vocabulary) gets structurally worse, and A2 (semantic
region scoring) must land BEFORE A1 rather than after.

## Metric definition (identical for every arm)

`recall@k` = the fraction of measurable questions for which the **union** of the
top-k retrieved units covers **every** gold phrase. This mirrors the repo's
`state_recall_at_k` semantics in `benchmarks/longmemeval_v2_diagnostics.py`
(`state_hit = len(state_phrase_indices) == len(answer_phrases)`).

Measurable question = phrase-eligible (all normalised phrases >= 8 chars, not in
the generic set) **and** source-complete (every phrase occurs somewhere in the
domain corpus). Abstention questions whose gold text appears nowhere in the
corpus are excluded — retrieval cannot be scored against a literal that does not
exist.

Retrieval arms, all offline and local:
- BM25 (Okapi, k1=1.2, b=0.75) over `normalize_text`-tokenised unit text.
- Dense: `sentence-transformers/all-MiniLM-L6-v2`, cosine. Units longer than the
  model window are embedded by mean-pooling 256-token windows. This is a PROXY
  for the production 1024-dim long-context embedder and is labelled as such
  everywhere. Mean-pooling favours the FAT arm (it lets a fat unit be
  represented by all of its content rather than its first window), so the proxy
  is conservative with respect to the hypothesis being tested.
- Fusion: reciprocal rank fusion (RRF, k=60) of BM25 and dense.

Corpora, per domain, over the identical 100-trajectory haystack:
- `FAT` — the chunk as indexed today (trajectory preamble + state header + tree).
- `SLICE` — the A1 slice as designed (header + breadcrumb + content only).
- `SLICE+GOAL` — ablation: the trajectory goal prefixed to the slice text, to
  test whether inherited parent context recovers any lost free ride.

## PRIMARY RULE (as briefed, literal)

Compare `SLICE` recall@10 against `FAT` recall@10, same k = 10 items, per domain
and pooled.

> **If slice recall@10 falls below fat-state recall@10, the free-ride hypothesis
> is CONFIRMED and A2 must precede A1.**

## SECONDARY RULE (budget-matched), pre-registered with the same force

k = 10 items is not a physical constant; it is a payload-budget knob. Ten fat
chunks carry roughly 135,000 characters. Ten slices carry roughly 10,000. A
literal item-for-item comparison therefore also measures a ~13x payload cut, not
only the loss of the free ride. So the following is fixed in advance:

Let `N_budget` = round(mean chars of the top-10 FAT units / mean chars of a
rendered slice), computed per domain from the measured data. Compare `SLICE`
recall@`N_budget` against `FAT` recall@10.

> **If SLICE recall@10 < FAT recall@10 but SLICE recall@`N_budget` >= FAT
> recall@10, the correct reading is NOT "A2 before A1". It is: slicing preserves
> reachability at equal payload budget, and the item cap must be re-tuned with
> the unit size. A1 may proceed, with the k-retuning named as a required part of
> the slice: the retrieval budget must be expressed in characters/tokens, not in
> items.**

> **If SLICE recall@`N_budget` < FAT recall@10, the free-ride hypothesis is
> CONFIRMED without qualification: slicing loses gold that the fat unit reached
> even when the payload budget is held constant. A2 must precede A1.**

## TERTIARY diagnostics, reported regardless of verdict

- Full recall@k curves for k in {1, 5, 10, 20, 50, 100, 200} for every arm.
- Median and p90 rank of the first gold-bearing unit per arm.
- Per-domain split (enterprise vs web) — the campaign has repeatedly found
  domain-split effects.
- Which questions lose: named question ids, their type, and the rank the gold
  unit fell to.
- BM25-only and dense-only alongside the fused arm, so the loss (if any) can be
  attributed to a lexical or a semantic failure.

## Falsifiers stated in advance

- If `SLICE` beats `FAT` at k=10 outright, the free-ride hypothesis is REFUTED
  and A1 may proceed as planned.
- If the verdict differs between enterprise and web, the verdict is reported as
  domain-split and neither domain's result is generalised.
- If the number of measurable questions is < 8 per domain, every recall figure
  is reported with its raw numerator/denominator and explicitly labelled as
  underpowered; no verdict is asserted more strongly than the n supports.
