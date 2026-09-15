# Ordinary evidence packets

The next fix should make the existing ordinary proposer and critic consume one shared, provenance-preserving evidence-packet representation. Reusing the existing complete controller view is necessary, but the measured retained failures still require several packets at the default policy. No paid run or source clone is armed by this design.

The implementation worktree is `/Users/bliss/dev/worktrees/sibyl/nova/ordinary-dream-episode-views`, created clean from local and remote main `90c042ce0821d24a6fbe6c97499718862713b28e`. The user's checkout and remote instances remain untouched.

## Diagnosis

The retained source archive (SHA-256 `8c597589c5ded445e40817b14458015741d9c09eb4919e11f90db8d3e01b3f15`) contains 233 signed admitted captures. The independently reproduced census qualifies 227 distinct scored episodes across 20 families: 190 passed and 37 task_failed. Five candidate_failed outcomes and one candidate_timeout remain authentic operational outcomes. Seven controller failures were not admitted. Ordinary preparation must retain the operational outcomes, with reported-evidence qualification.

The decoded-character census in `measurement.json` finds 186,459 to 1,237,420 characters per admitted capture, totaling 150,816,233. Every source exceeds the default 40,000-character budget before prompt or schema overhead.

| Retained failed episode | Raw characters | Complete shared view | Distinct string characters | Distinct line characters |
| --- | ---: | ---: | ---: | ---: |
| Interval capacity, half-open-capacity-sweep-111 | 979,194 | 93,891 | 52,878 | 42,002 |
| UTF-8 framing, utf8-record-framing-111 | 739,393 | 78,169 | 38,520 | 31,891 |

The section census in `projection-sections.json` finds 21 model requests, 21 responses, 20 tool calls and 20 tool results per failed episode. Existing exact-value sharing already reduces the interval view from 142,265 to 93,891 characters and the UTF-8 view from 116,941 to 78,169. Only 12,745 and 12,817 exact duplicate string characters remain. Even ideal removal of those duplicates cannot fit either complete view into 40,000 characters. The interval episode's distinct lines alone exceed the budget. These counts omit framing, reference and output-schema overhead.

The existing view includes changing commands, model output, tool results and budget messages. No single giant metadata field explains the remaining size. The largest string is 1,696 characters for interval capacity and 1,593 for UTF-8. Paging complete semantic units is justified for these measured traces; another blind character limit is not.

## Existing owners and failure boundary before this implementation

Source paths below are relative to `packages/python/sibyl-core/src/sibyl_core/` unless an application prefix is shown.

- The ordinary job in `apps/api/src/sibyl/jobs/reflection.py:109` selects 20 sources by default and clamps selection to 100. The selection clamp is independent of database paging and the complete-input budget.
- The ordinary cohort job in `apps/api/src/sibyl/jobs/ordinary_cohorts.py:39` groups by principal and scope, not task family. The service in `services/ordinary_cohort.py:85` retains whole source bytes and constructs partial episodes with unknown reported outcomes. Candidate failures do not independently reject a source or batch.
- The partitioner in `services/ordinary_cohort.py:329` measures whole episodes plus prompt, system and schema. Oversized singletons return to `HeuristicReflectionExtractor` through `services/dream_checkpoints.py:100` and `services/reflection.py:180`.
- The ordinary critic in `services/reflection_validation.py:230` supplies complete sources as reported evidence. The shared preparation in `tasks/memory_validation.py:115` projects only conditional procedures; ordinary reflection embeds complete source text. The critic's budget check at `services/reflection_validation.py:421` fails before model dispatch. Heuristic extraction therefore cannot rescue new automatic publication under the default policy.
- The reusable representation owners are `tasks/episode_evidence.py:324` (project_episode), `:360` (encode_episode_views), and `:373` (episode_projection_receipt). They already verify encoded aliases, classify original evidence coverage, preserve exact source citations, and disclose audit-only fields. Conditional proposal preparation in `tasks/consolidation.py:328` and conditional critique in `tasks/memory_validation.py:119` already consume them.

## Shared packet contract

The packet owner should consume the existing complete projection, preserving immutable original source bytes outside the prompt. A deterministic manifest binds source ID, content hash, observed revision, incarnation and generation; the projection, citation and coverage receipts; and the complete ordered packet set. Packet count is never an episode count. All pages from one capture retain one source identity and one independence unit.

Partition complete semantic units against the actual stage prompt and output-schema budget. Reuse exact-value sharing within each packet, and repeat only the context needed to interpret the packet (with explicit references). Every projected semantic unit belongs to the manifest. Audit-only omissions stay disclosed and hash-bound. A unit that cannot fit must produce an explicit preparation failure; it cannot disappear from coverage. Byte-preserving subunits would require their own reviewed rule.

The ordinary proposer should process the entire manifest using the existing durable validation-stage owner. Each returned proposal identifies the packet actually observed and may cite only immutable ranges exposed by that packet. A source-grounded partial proposal cannot claim that unobserved conditions are absent or that every outcome is externally verified. Unknown outcomes and ordinary reported qualification remain visible.

The ordinary critic should reconstruct the packet and cited ranges from the original stored sources, never trust caller-provided excerpts. The current source authority and observation fences must still pass. Unknown citations, changed bytes, changed packet membership and claims citing audit-only fields must fail before publication. The proposer and critic must share the same packet-construction owner, with its identity included in replay and candidate bindings.

The job must record every page as returned, abstained, failed or pending. Advancing a UUID cursor means dispatch progress only. A source cannot be reported complete while any required page is unprocessed. Do not invent a second queue or an independent memory pipeline; adapt the existing preparation and durable stage contracts.

The selected consumer contract is one ordinary proposal per packet and ordinary critique against that same reconstructed packet. Every packet repeats source-wide goal and outcome context and preserves event order. Each candidate states that it was checked against an observed packet, with applicability beyond the packet unestablished. No full-source consistency verdict or cross-page learning verdict follows from a per-page no_findings result. Preserve contradictory candidates and critic findings as distinct results. Successful processing or explicit abstention must cover every required packet before the source pass is complete; failed or pending packets remain incomplete and resume through existing durable executions.

Each source's packet manifest is reconstructible from that original capture. A page is not an extra episode, and a candidate supported by one capture has one independent support unit. Complete multi-source preparation remains available when it fits. Paging long observations does not manufacture a passed/failed cross-source contrast. The packet binding must be protected by the persisted proposal execution and candidate lineage; mutable metadata alone is insufficient proof of what the proposer observed. Corrected descendants must retain the original packet boundary through their verified parent execution chain.

## Acceptance and next screen

Tests must exercise both ordinary stages through a real transport boundary with offline responses at the default policy. A fixture must reproduce the measured retained failure geometry, not only a small synthetic trace. Every required page must be accounted for and carry original-source provenance. Corrupted controller data, changed source observations, unknown packet references and citations outside allowed evidence must fail. A partial page run must remain incomplete and must resume without replaying completed paid stages. Source counts must remain unchanged when one source has many packets.

Shared-view integration alone is not the completed fix: the actual retained interval and UTF-8 views remain oversized without paging. The current frozen 24-cell study and its full raw baseline are untouched. No new paid calls, budget reservation, database write or clone launch is authorized by this artifact.

After packet qualification, the next unarmed screen retains the preselected interval_capacity and utf8_framing families, six existing tasks, four arms, and checkpoints zero and one, for a full 48-cell denominator. Missing packs and failed preparation remain explicit cells. Checkpoint zero is actual authorized pre-dream retrieval after source restoration and readiness, not a fabricated empty Sibyl pack. One full pass over a stable 233-source inventory would require eleven default 20-source calls plus one 13-source call, with actual invocation and page counts recorded separately. No solver outcomes feed the memory preparation.

The exact raw-retrieval and strong-summary input policy remains a blocking preregistration input. The new complete interval and UTF-8 histories contain 8,113,667 and 6,707,994 characters; the frozen 24-cell baseline cannot silently become shortened or be reused as if these inputs were identical. A bounded retrieval policy would be a disclosed new experiment.

The original full-cohort source volume must first receive fresh ownership and stopped-consumer qualification. The archive names container `c835eaa4f2fc4da24ae6e98739df1bdb71fa2b4701d48e4bf661101377d07f2c` and volume `sibyl14-learning-source-09e8148ad4ee4ec592a409f548042059-data`; it contains no native checkpoint. Reuse the guarded cold-copy, native inventory/export, and restore owners with new reviewed bindings. Preserve all 233 admissions, 240 assignment slots, original authority and budget history. No current remote source-state claim is made here.

The proposed solver reservation is another $96 at the existing conservative $2 per cell, subject to fresh pricing and complete-request counts. If the current 24-cell claim adds $48 to the existing $707.2186368, the proposed next cumulative reservation would be $851.2186368, above the existing $762.4700928 ceiling. The separate $300 memory ceiling and every physical attempt, usage-unknown reservation and embedding cost remain cumulative; the current recovery's terminal ledger must be reconciled before a new envelope is proposed.

The routing pair, original dream 0/1/3/10 curves, sealed study, conversational nonregression, scale and host qualification remain separate open gates. Meeting the collection target establishes available experience, not learning or release readiness.

## Implementation checkpoint, September 14

The shared owner is `tasks/ordinary_packets.py`. Both ordinary proposal preparation and the ordinary critic reconstruct its projection, exact citations and complete manifest from the retained capture. Each packet contains complete model/tool exchanges, the source-wide goal and reported outcome, and original event indices. The manifest binds revision, incarnation and generation alongside the source hash. Audit-only fields remain disclosed through the existing complete projection receipt; a packet is never described as full history.

The packing policy measures the actual proposer and critic envelopes and output schemas. It reserves one quarter of the default input budget for the critic's generated-candidate representation. The reserve is counted once as a total envelope allowance; the actual critic representation includes the candidate twice. The reserve is a planning rule, not a fit guarantee. The final critic checks the complete rendered candidate and schema before dispatch. Oversized indivisible exchanges fail preparation. Oversized generated candidates retain the returned proposal and usage, with no critic send or automatic repaging.

The protected origin foundation is commit `885a44aa6f459fb6b60797cffcc789a705c5ccca`. The existing candidate transaction writes an immutable optional derivation origin. The packet consumer requires a valid returned proposal origin, derives corrected ancestry from immutable executions, and repeats execution dependency guards at dispatch and publication. Removing mutable candidate metadata cannot change the observed packet or reset the correction root. Historical purged origins remain archivable but unavailable for live review. Signed procedure corrections retain their separate guarded consolidation writer.

The job's source-pass flag records proposal-manifest coverage only. Every page result retains its operation ID, including a returned model result whose candidate write later failed. The run's model-usage index includes nested page executions. A partial pass retains successful candidates, exposes failed or pending pages, and resumes returned stages without another model send. The UUID selection cursor remains distinct from source-pass completion.

Offline acceptance exercised real OpenAI SDK proposer and critic adapters with a mock transport, using the exact two retained failed source files. Every page reached both stages at the default 40,000-character policy. These counts include the actual schema and candidate envelope, but measure characters, not billable tokens or dollars.

| Retained failed episode | Packets | Proposal plus critic calls | Summed packet payload characters | Summed canonical wire-request characters |
| --- | ---: | ---: | ---: | ---: |
| Interval capacity | 11 | 22 | 232,478 | 655,302 |
| UTF-8 framing | 8 | 16 | 168,921 | 480,896 |

The packet payload totals include repeated context across pages. The wire totals count each proposer and critic request separately. Replaying the first proposal sends zero additional requests. The original source still contributes one independent support unit. The artifact `ordinary-dream-packets-20260914/retained-metrics-v1.json` records the exact source hashes and test properties. Default transport reachability is implementation evidence; no learning, publication or release claim follows from the mock outputs.

Independent verification is still in progress. A native final-policy race failed an existing nonpacket publication control and is being isolated against the baseline before assigning regression scope. No paid packet call, full-cohort restore, or new experiment has been authorized by this checkpoint.
