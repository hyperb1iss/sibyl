---
name: sibyl
description:
  Persistent memory and task coordination for this project. Invoke for any prompt about past work,
  project state, in-progress tasks, prior decisions, gotchas, or capturing a non-obvious learning.
  Also covers semantic search across project knowledge and external docs.
allowed-tools: Bash(sibyl:*)
---

# Sibyl

This stub points at the version-matched workflow shipped by the installed CLI.

**Load the compact contract at session start:**

```bash
sibyl skill get contract
```

It carries the five agent verbs, small enums, and hard rules in about 1k tokens. Before the first
write intent (`remember`, `correct`, or a task mutation), load the full version-matched pack:

```bash
sibyl skill get core
```

Load packs whole. Never pipe `sibyl skill get` through `head`/`tail`: the flag contracts and enum
tables sit past the fold, and truncated bootstraps are the leading cause of invented-flag errors.

Loading a pack is not the memory work itself: after `skill get`, still run the `sibyl context` /
task command you came for. Sessions that load the contract and skip the verb defeat the loop.

**Quick triggers (intent -> verb):**

- "what am i working on" -> `sibyl task list --status doing,blocked`
- "where did i leave off" -> `sibyl context "<goal>"`
- "have we hit this before" / "do we know X" -> `sibyl context "<topic>"`
- "remember this" / "write this up" / "save this insight" -> `sibyl remember "<title>" "<body>"`
- "that memory is wrong/stale/superseded" -> `sibyl correct <id>` then add `--action ...`

Live status: `sibyl health` shows server reachability.

Useful follow-ups:

```bash
sibyl skill list
sibyl skill get workflows
sibyl skill get examples
```

Hooks are separate from skills. Install or update hooks only when the user explicitly wants
automatic prompt/session integration.
