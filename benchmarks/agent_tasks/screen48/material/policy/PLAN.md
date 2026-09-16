# Recall policy for the next 48-cell screen

Use the existing native retrieval engine, the existing raw fulltext/vector recall owner, and the entire 20-reference summary library. Give every arm the same 90,000-token memory allowance and unchanged solver budgets. This is one proposed policy, not an armed experiment. No new summary index, family router, or retrieval model is needed.

The six tasks remain venue-capacity-report, rack-power-windows, inclusive-slot-occupancy, hex-stream-journal, record-separator-channel, and independent-message-decoder. Each appears in four arms at checkpoints zero and one. The resulting denominator is 48. The new manifest and attempt IDs do not exist yet.

## Source and query boundary

All 233 signed admitted source views remain in the raw inventory and the summary construction corpus. The 227 qualified distinct scored episodes are an analysis count, not permission to discard six admitted observations. Preserve all 20 training families, source bytes, revisions, observation identities, and source-wide projection receipts. Ordinary packet pages do not become additional sources.

The only task query is the complete public prompt with whitespace normalized by the existing context rule (`' '.join(prompt.strip().split())`). Preserve the original prompt bytes separately for the solver. Use this same normalized query and fixed reader/project authority for native and raw recall; attach its hash to the summary receipt, although the full summary library requires no query-based selection. Do not append a domain, task ID, future family label, expected mechanism, reference solution, private test, or solver result. Workspace files are counted for fit but are not added to the retrieval query.

Training family metadata may group the 233 sources into the already proposed 20-reference library. Every family is included for every task. The map from a future task to its learning family stays outside retrieval and packing. Source training labels that already occur within authentic episode text remain unchanged. The six-task selection is inherited, never reconsidered based on outcomes.

## The three memory arms

| Arm | Candidate owner | Fixed membership and ordering |
| --- | --- | --- |
| Native | `retrieval.search.context_search` with `build_context_retrieval_plan` | Actual authorized restored graph and raw state at each checkpoint. Use all native signals, `facets=()`, `facet_types={}`, `types=None`, `facet=None`, `limit=50`, `include_content=True`, default weights and configured embedding provider. Preserve returned order and every SearchResponse filter/diagnostic receipt. |
| Raw retrieval | `services.content_raw_recall.recall_raw_memory_with_sources` | Restrict each database lane to the original 233 capture UUIDs before ranking and limits. Request `limit=233`, require the existing fulltext and vector lanes to complete, preserve their scores/order and the existing RRF order. No graph candidates or newly dreamed rows enter this arm. |
| Strong summary | Existing proposed 28 complete-source maps plus eight reductions | Include every one of the 20 completed final references, ordered by the original training family ID. No task-aware family selection and no second search index. Preserve all child/source lineage. |

The native call is the engine used by the context compiler, with an explicitly declared 50-result engine limit and whole-item renderer. It is not a claim that the web/CLI context renderer uses these settings. The engine exposes candidate-source failures that the display compiler can collapse into fallback behavior. Require nondegraded source/fusion receipts. A verified empty native result is valid at checkpoint zero; an exception, failed lane or missing authority receipt is not an empty pack.

Raw ranking uses the existing stored title/raw-content index and existing dense vectors. The solver receives the complete semantic controller view reconstructed from the exact selected original bytes. Index text and exposed text therefore differ by the existing, disclosed projection. Do not rebuild or silently replace embeddings, and qualify their existing model/dimension/source bindings before using the dense lane. Require successful `raw_fulltext` and `raw_vector` receipts, not merely `degraded=False`: the embedding helper can return no vector without an exception. An embedding or lane failure leaves the raw pack missing; it does not silently become lexical-only recall.

The present raw API's `source_ids` filter targets the separate `source_id` column. The required minimal change is an optional `capture_ids` argument through `_RawMemoryRecallFilters`, `_raw_recall_filters`, `_recall_raw_memory_result` and both public recall functions. The shared WHERE must apply `uuid IN $capture_ids` before fulltext, vector and lexical ranking/limits. `None` preserves normal behavior; an explicit empty collection matches nothing. A post-filtered mixed top-k is not equivalent. The native database control must verify that many closer nonmember vectors cannot crowd out the original members. That implementation and its qualification remain separate from this design.

## Whole-item packing and counting

Use one small packing adapter over owner-returned candidates. Native records that refer to one of the original raw captures receive the same complete view hydration as the raw arm. The existing candidate owner supplies type `raw_memory` and ID `raw_memory:<capture UUID>`; resolve that exact typed ID, not the separate upstream `source_id` metadata. Preserve whole published native items and their public provenance otherwise. Preserve partial native passage identity and source ranges as partial passages; never relabel them complete original sources. Do not call the display renderer or `pack_naive_results`, whose character truncation and oversized-first-item exception do not implement this policy.

For native and raw, append complete blocks in owner-returned order until the next block would exceed either fit check. Stop at that first overflow and record the remaining ranked suffix as budget-omitted. Do not truncate a block, skip a large item to favor smaller ones, pad a short pack, or append a lower-ranked alternative. An oversized first item makes that task's pack missing. The offline corpus check proves every original raw singleton fits the proposed allowance; newly published native items still require actual checks.

For summary, require all 20 references to fit together. A missing map, reduction, reference, authority check, or oversized complete library makes the summary arm missing for the affected checkpoint. Do not send a partial library, shorten an output, select a family, or silently change the proposed 4,096-token/16,384-character summary output policy. The construction recipe and output ceilings are still proposed and unarmed.

Count the exact final memory bytes with the retained Qwen tokenizer. Then construct the controller's actual initial body using `Controller._messages`, `Controller._body`, the real tool schema and cumulative-budget message, and its pinned chat template. Require both:

1. Complete memory message at most 90,000 local Qwen tokens.
2. Complete initial request tokens + all initial workspace tokens + the full 8,000-token output allowance strictly below 204,800.

The second check reuses the prior conservative route allowance. Local counts are reproducible admission measurements, not native provider counts, pricing or a guarantee about future tool history. Retain body bytes/hash, rendered-template hash, tokenizer/config/controller identities, workspace hashes and each count. Recount every actual task/arm/checkpoint input before a launch proposal. Do not fill unknown actual rows with source-only examples.

## Source-only fit evidence

The attached measurement reconstructs every admitted episode with `project_episode`, `encode_episode_views` and `episode_projection_receipt`. The 233 independently encoded views total 6,392,356 Qwen tokens. Their individual range is 14,017 to 64,056; the largest framed singleton is 64,202. A whole-source baseline containing the full corpus cannot fit this solver context.

The largest-view source contains 571,205 tokens as its original serialized capture, compared with 64,056 in the complete semantic view. The source-only example in `native-raw-shape-example.json` confirms why native raw hits need the same hydration. The example was selected solely by view size and is not an observed native retrieval result.

The proposed 20 summary texts have a summed separate ceiling of 81,920 tokens. Empty framing measured 1,679 reference tokens, leaving 8,080 between the separate text ceilings and the common allowance. Token counts are not exactly additive across boundaries, and actual hashes and summary text do not exist yet. Actual complete-library counting is mandatory.

The geometry control packs the largest complete source first, then stops when the next does not fit. Its 64,202-token pack produces complete initial requests of 64,906 to 64,928 tokens across the six public prompts. Adding every workspace file and the full output allowance yields 73,105 to 73,295. The measurement also counted all 233 singleton packs against all six prompts. This stress ordering is explicitly not a retrieval selection and must never be used to create an experimental pack. Actual native/raw rankings and summary outputs remain unobserved.

## Equal ceilings do not mean equal usage

Retain Qwen qwen3-coder-next, seed zero, 2,000,000 cumulative input tokens, 8,000 output tokens, 20 tools, $2 per cell, 600 seconds for the controller, 120 seconds for the checker, 30 seconds per tool, and 256 MB. These are proposed retained settings pending the new manifest, complete counts and budget envelope. No old claim is reused.

The controller repeats the memory message on every model request alongside growing tool history. At 21 hypothetical requests, a 90,000-token memory contributes an arithmetic 1,890,000 tokens before other messages. Only 110,000 of the cumulative input allowance remains for all other repeated content. The proposed separate summary text ceilings contribute 1,720,320 before framing and other messages. A 131,072-token allowance would contribute 2,752,512 and was rejected on this budget arithmetic alone.

The common allowance therefore fits the largest raw source and plausibly the entire proposed summary library, but does not guarantee all 20 tools remain usable. Actual history, output, cost or time can bind earlier. The existing controller checks reported cumulative usage after each call and can cross a limit on that last call; this policy does not claim a new predictive per-request token guard. Report each actual budget termination with its usage. Do not raise a ceiling, replay a task, or narrow source selection in response to a result. Equal ceilings compare whole memory pipelines with their actual usage, not equalized token consumption.

## Checkpoints and denominator

Restore and qualify the complete source inventory before checkpoint zero. Capture actual native pre-dream retrieval. Build raw packs and the summary library from the same source-only snapshot before any solver output is available to memory preparation. Keep raw and summary bytes identical at checkpoint one after fresh source-authority checks. Only native memory may change through the declared full ordinary source pass. Solver runs cannot write back into the preparation database or become memory sources.

Each pack receipt binds the complete eligible catalog, all selected IDs and exact block/source hashes, ranked but budget-omitted IDs, and eligible IDs not returned by the retrieval owner. The categories are disjoint and exhaustive. Native records also retain item-content and provenance hashes; summary records cover all 20 references and every contributing source. Unknown or changed source identity, stale authorization, a degraded lane, missing reference or count failure produces a missing pack with its reason. A summary missing any required source contribution cannot claim full-corpus coverage.

Keep all 48 scheduled cells in reports, including missing packs, preparation failures, task failures, controller failures and unstarted work. There are no replacement task IDs or successful-case selection. No confidence claim or learning conclusion follows from this design. The prior completed diagnostic and its budget history remain immutable.

## Concrete next implementation

Finish the capture UUID filter in the existing raw owner. Add one adapter that hydrates retained views, renders whole blocks, counts the actual controller request, and emits the catalog/selection/omission receipt. Reuse the native search and raw recall owners directly. Summary packing only concatenates the complete 20-reference library. Existing authority, source projection, controller, runner and report validators retain their roles.

Before activation, the adapter needs focused controls for nonmember KNN crowding, empty versus absent capture filters, no future-family input, changed source observations, complete prefix packing, oversized first items, complete-summary refusal, exact request counting and missing-pack denominator retention. A native restore, proposed summary construction, fresh pricing/envelope and complete 48-cell manifest still need qualification. This artifact authorizes none of those operations and reserves no money.
