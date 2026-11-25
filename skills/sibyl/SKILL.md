---
name: sibyl
description:
  Persistent memory and task coordination for this project. Invoke for any prompt about past work,
  project state, in-progress tasks, prior decisions, gotchas, or capturing a non-obvious learning.
  Also covers semantic search across project knowledge and external docs, and sibylnet: reading
  your agent inbox, messaging another person's agents, and handing off tasks.
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
- "anything waiting for me" / "check my inbox" -> `sibyl net read @me`
- "ask ada's agent" / "get a review from X" -> `sibyl net who`, then
  `sibyl net post @<handle> "<ask>" --thread <subject>`
- "hand this to X" / "did they finish it" ->
  `sibyl task assign <id> @<handle> -m "<context>" --json`, then
  `sibyl net watch -t task:<id> --since <message.cursor> --once --json` in a loop (load `core`
  first: a watch without `--since` starts from now)
- "tell the team prod is on fire" (live state, never durable knowledge) ->
  `sibyl remember "<title>" "<body>" --scope team --scope-key <team-uuid> --expires 2h`; resolve
  early with `sibyl correct <id> --action stale --reason resolved`

Live status: `sibyl health` shows server reachability.

Useful follow-ups:

```bash
sibyl skill list
sibyl skill get workflows
sibyl skill get examples
```

Hooks are separate from skills. Install or update hooks only when the user explicitly wants
automatic prompt/session integration.
