---
title: Marketing Positioning
description: The positioning spine every Sibyl surface inherits across repo, docs, and sibyl.ist
---

# Marketing Positioning

This is the single source of truth for how Sibyl positions itself. Every public surface
(the GitHub README, the VitePress docs home, the sibyl.ist landing site, social and meta
tags) renders the same spine in its own length. Not identical copy. One story.

When a surface disagrees with this document, the surface is wrong. When this document
disagrees with reality, this document is wrong. Fix it here first, then propagate.

Validation snapshot: **2026-05-30**, against the competitive site survey in
`sibyl.ist/docs/research/sites/` (eleven competitors across four clusters) and our own
benchmark and license sources.

---

## The Spine

Six elements. Every surface inherits these and nothing contradicts them.

### 1. Category phrase: own it

**cross-agent memory**

We repeat this phrase in the hero, the README lede, meta tags, and copy until it is ours.
The reason is defensive. Graphon's funded PR is pushing "relational memory" as the category
term, and if a visitor pattern-matches Sibyl to "the cheap Graphon" they bounce. Owning a
distinct phrase early is how we avoid being read as a clone of a better-funded thing.

Do not drift to "persistent memory," "AI memory," or "memory layer" as the lead term.
Those are the crowded category words every competitor already shares. They are fine as
supporting SEO keywords, never as the headline.

### 2. The wedge: one line

**One self-hostable knowledge graph, shared across every coding agent you use.**

This is the position the survey found undefended. Mem0 and Zep sell single-app memory.
Letta is leaving memory for coding agents. Mastra treats memory as module four of five.
Cognee and Graphon lean enterprise infra. Nobody markets one graph shared across Claude
Code, Codex, OpenCode, Cursor, and the agents you build yourself. That cross-tool
unification is the headline, not a feature bullet.

Site hero (validated, keep): *"One CLI. One graph. Every AI tool you use, sharing memory."*

### 3. The how: the moat rivals cannot copy

**It lives in the CLI. If your agent can run a shell command, it already speaks Sibyl.**

Every competitor is API-first, SDK-first, or MCP-first. Sibyl is shell-first. The CLI is
the interaction surface. MCP is available for clients that prefer it, but the depth lives
in the CLI. This is the "codemode" angle: the industry is moving toward agents executing
code that calls tools rather than calling tools directly (Cloudflare Code Mode, Anthropic's
code-execution-with-MCP), and an agent's most native capability is running shell commands.

This is structural, not cosmetic. A rival cannot copy "shell-native" without rebuilding
their product around the terminal. It is the *how* underneath the wedge: the reason "every
agent" is literally true rather than aspirational.

Positioning weight: this is the **how**, not the headline. The hero stays on the wedge.
Codemode shows up one layer down (the subhead, a feature pillar, the README lede). It is
the proof that the wedge is real, not the first thing a visitor reads.

### 4. The flank: self-host free, the whole system

**Self-host the whole system, free. No capability paywall. AGPL-3.0.**

Self-hosting is the cluster's open flank, and it is wide open. Zep deprecated Community
Edition (April 2025); Mem0 paywalls the graph at $249/mo; Supermemory paywalls self-host at
$399/mo; Honcho's "open source core" has no self-host docs; Graphon is demo-gated. Every
funded rival abandoned or paywalled the audience that wants to run their own memory. Sibyl
runs the full system on a laptop, a Pi, or a $15 cloud box, with every feature.

State the license accurately. **Everything Sibyl ships is AGPL-3.0-only today**: the server
(`apps/api`), the CLI (`apps/cli`), and the core library (`packages/python/sibyl-core`) all
declare `AGPL-3.0-only`. The only Apache-2.0 in the tree is the internal `apps/e2e` test
harness, which is not a public SDK.

**Open decision before launch.** The sibyl.ist copy currently claims "AGPL-3.0 server,
Apache-2.0 SDK." That is not backed by the code. A permissive client/SDK license is a
legitimate strategy: it lowers adoption friction and answers the real "I cannot put AGPL in
my client code" objection. But the claim has to follow the code, not the reverse. Either
relicense the SDK/core to Apache-2.0 deliberately, or correct the copy to AGPL-3.0. Do not
ship the Apache claim while the license field says otherwise.

### 5. The proof: one number, one run, everywhere

**96.96% strict R@5 on LongMemEval-S, live API path, no LLM in the retrieval path.**

This is our credibility moat *and* our biggest discipline risk. The whole "honest
benchmarks" counter-position only works if the number is identical on every surface and
cited to one run. The category is in a trust crisis: MemPalace immolated its credibility
overclaiming 100%, and Mem0 and Zep are in a public benchmark mud-fight. Being the site
whose numbers are exact, caveated, and reproducible is a real wedge. Being the site whose
README and landing page cite *different* ceiling numbers throws that away.

Canonical result: run `26304777971`, sha `36032a25`,
[`docs/testing/longmemeval.md`](../testing/longmemeval.md).

| Metric      | Value                          |
| ----------- | ------------------------------ |
| `hit@5`     | 100.00%                        |
| `recall@5`  | **96.96%** strict multi-answer |
| `recall@10` | **98.90%**                     |
| `ndcg@5`    | 94.63%                         |
| Search p50  | 584 ms                         |
| Search p95  | 1,115 ms                       |

Rules for every benchmark mention:

1. **One run.** Cite run `26304777971` until a newer run is promoted to canonical in
   `longmemeval.md`. When that happens, propagate to every surface in the same change.
2. **Never round up.** 96.96%, not 97%. The precision is the honesty signal.
3. **Always carry the caveat.** `hit@5 = 100%` and strict `recall@5 = 96.96%` measure
   different things, and retrieval recall is a different axis from end-to-end QA accuracy.
   The retrieval-vs-QA distinction travels with the number, every time. See
   [`ai-memory-landscape.md`](../testing/ai-memory-landscape.md).
4. **Name the honest gaps.** No published QA-accuracy number yet, OpenAI embeddings, one
   dataset, no principled forgetting. Stating them is the asset, not the liability.

### 6. The character

**Built in the open, solo, dark neon. We build Sibyl with Sibyl.**

We cannot fake enterprise logos or "90,000+ developers." The honest assets read as *more*
trustworthy to a senior developer burned by hype: a live GitHub star count, an
agent-compatibility grid (Claude Code, Codex, OpenCode, Cursor), a working quickstart, the
SilkCircuit dark-neon identity that signals built-by-agent-developers-for-agent-developers,
and the solo-founder-in-the-open story. The design bar to clear is Honcho (8/10, dark,
neon). Win on craft and honesty: the two axes the funded field left open.

---

## What Sibyl is, and is not

The survey treats Sibyl as a pure memory product and even suggests saying "we are just
memory, not a framework." That is half right. Memory is the **wedge** because it is sharp
and ownable. But Sibyl is genuinely more: task workflow, source-grounded synthesis, source
ingestion, a real web UI, scoped multi-tenancy.

The stance: **lead with the memory wedge, let the breadth be the second act.** Cross-agent
memory gets you in the door. "And it compounds into a system: tasks, synthesis, one graph
for everything you know" is the reason you stay. Do not lead with the breadth (it dilutes
the wedge into "another platform"), and do not hide it (it is the retention story).

- **Is:** cross-agent memory, a knowledge graph, a memory loop, a task workflow,
  self-hostable, shell-native, source-preserving, graph-native (SurrealDB unifies graph,
  vector, full-text, and traversal).
- **Is not:** a coding agent (we serve them), an agent framework (we are the memory layer
  under one), a lock-in cloud (self-host any day, export is one command), a vector DB with a
  wrapper (the graph is the substrate).

---

## Surface-by-surface rendering

Each surface renders the spine at its own length. The wedge and the phrase are constant; the
detail scales.

| Surface | Lead | Carries |
| ------- | ---- | ------- |
| **sibyl.ist hero** | Wedge headline, cross-agent-memory subhead with the shell-native hook | install one-liner, hit@5 badge, two CTAs |
| **GitHub README lede** | cross-agent memory and the wedge, codemode as the how | quickstart, the proof number, "What You Get" breadth |
| **VitePress docs home** | Wedge as `hero.text`, problem-first framing | the loop, breadth features, quickstart |
| **Meta / OG / keywords** | "cross-agent memory" first keyword | self-hosted, LongMemEval, the agent names |
| **Social / launch** | Wedge, the honest benchmark, solo-in-the-open | screenshots, the graph, the star count |

Consistency checklist before any launch surface ships:

- [ ] Leads with the wedge, not "persistent memory."
- [ ] Uses "cross-agent memory" as the category phrase.
- [ ] Benchmark number is 96.96% R@5 / 98.90% R@10, cited to run `26304777971`, with the
      retrieval-vs-QA caveat.
- [ ] License stated accurately: AGPL-3.0 today. Do not claim an Apache SDK unless the code
      has been relicensed (see section 4).
- [ ] Self-host-free flank is loud, not buried.
- [ ] Codemode / shell-native present as the how, not the headline.

---

## Competitive context (why the spine is shaped this way)

Full survey: `sibyl.ist/docs/research/sites/`, a synthesis plus four cluster audits. The
load-bearing findings:

- **The wedge is undefended.** Four independent competitor sweeps converged on the same gap:
  nobody owns "one graph shared across every agent." Strongest signal competitive research
  gives. Do not second-guess the wedge.
- **Self-hosting is the open flank.** Every funded rival paywalled or killed it.
- **The benchmark category is in a trust crisis.** MemPalace is the cautionary tale: it
  overclaimed 100%, the community tore it apart in 48 hours, and it walked the number back.
  Honest, exact, caveated numbers are counter-positioned against the whole field.
- **The design bar is Honcho (8/10, dark neon).** Match it on craft. Dark neon is free
  differentiation, because every funded rival runs safe light-mode SaaS.
- **We will not out-distribute the funded teams.** Mem0 is AWS's exclusive Agent SDK memory
  partner; Mastra raised $35M; Cognee €7.5M; Graphon $8.3M seed. The site must convert hard
  the visitors it gets. Craft and honesty are the axes a bootstrap can take to 10/10.

The funded competitors win on reach. We win on craft, honesty, the undefended wedge, and the
one thing none of them can copy without rebuilding: it runs in the shell every agent already
speaks.
