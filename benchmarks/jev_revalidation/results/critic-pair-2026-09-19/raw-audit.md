# Independent whole-critic paired audit

The receipt audit passes. The registered experiment does **not qualify** for conditional savings: 24 of 33 critic calls return valid no-findings outcomes, while nine abstain. All 33 calls completed HTTP 200 with known cost. The exact selection contains 17 distinct candidates and matches every bypass pair reconstructed from the frozen prior grouped Jev receipts.

Raw outputs reveal two different causes of abstention:

- Seven calls contain no findings and put affirmative “all supported / no concern” explanations in `abstention_reason`. These read as completed reviews encoded in the wrong output field, not inability to assess evidence. They remain abstentions under the frozen product contract.
- Both repeats of `fh-008` contain an actual `misleading_certainty` finding with disposition `qualify`. The critic objects that “Pema described the rehearsal as rushed” drops the source's “felt rushed” phrasing and transcript framing. The claim hash is correct, but the finding cites `fh-008-source-1` instead of the supplied citation ID `s0`. Product mechanical validation therefore returns abstain with no submission. The raw concerns must not disappear from interpretation merely because validated submissions are null.

The seven affirmative abstentions are `fh-011` repeat 0, `fh-022` repeat 1, `fh-010` repeat 0, `fh-020` repeat 0, `fh-007` repeats 0 and 1, and `fh-017` repeat 1. None describes genuine inability to assess the supplied evidence. This semantic reading is descriptive only and does not revise the registered verdict. The two raw findings are critic proposals, not proof the candidate is wrong.

| Measure | Independently reconstructed value |
| --- | ---: |
| Selected critic calls | 33 |
| Distinct candidates | 17 |
| Valid no-findings outcomes | 24 |
| Abstentions | 9 |
| Raw findings | 2, both mechanically rejected |
| Unknown cost calls | 0 |
| Critic input / output tokens | 78,149 / 3,155 |
| Original critic cost | $0.469620 |
| All 128 grouped Jev calls | $0.007106484 |
| Unqualified arithmetic difference | $0.462513516 |
| Qualified conditional avoided cost | None |

The difference is accounting arithmetic, not an established savings result. Any abstention disqualifies the experiment under its frozen rule. Even the seven affirmative explanations cannot rescue the result: the other two outputs contain substantive raw concerns, despite their invalid citations.

The standalone standard-library audit imports neither scorer nor product validator and does not read `summary.json`. It checks the frozen plan hash; exact selection from all 128 prior grouped receipts; prior observation accounting; original critic request and dispatch bytes; product prompt literal; candidate, observation and assertion hashes; exact retained Jev semantic input; original source text and provenance; full citation byte ranges; forced tool schema; raw provider/model; tool output shape; finding hashes and citation IDs; derived outcomes; and raw usage costs. All hashes and individual raw findings/reasons remain in `raw-audit.json`.

Reproduce locally:

```sh
python3 /tmp/jev-critic-pair-20260919/raw_audit.py > /tmp/jev-critic-pair-20260919/raw-audit-output.json
```

The script accepts `--live`, `--prior`, `--selection`, `--repo`, `--cases`, and `--out` for equivalent evidence locations. The plan hash is pinned to `908a1a2fb5485b15d415b63ff49c2d94a24a168f8d3f42a9b24ba49b7417951e`.

Selection was retrospective after Jev inspection, and the cohort contains synthetic plain reflections. Same-family critic agreement is not independent truth or production safety evidence. OpenRouter transport differs from direct Anthropic production. Cancellation of the unchanged 95 fallback paths and correction behavior is an untested counterfactual assumption. No production activation, complete-run savings percentage, or end-to-end latency claim follows.
