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

**Load the core pack before knowledge work:**

```bash
sibyl skill get core
```

You get the full `recall -> act -> remember -> reflect` loop, every CLI verb with the flags that
actually exist on this machine, context pack usage, error patterns to avoid, and lock-conflict
handling. Don't guess the verbs from training data; the CLI schema changes between releases.

**Quick triggers (intent -> verb):**

- "what am i working on" -> `sibyl task list --status doing,blocked`
- "where did i leave off" -> `sibyl recall "<goal>"`
- "have we hit this before" / "do we know X" -> `sibyl search "<topic>"`
- "remember this" / "write this up" / "save this insight" -> `sibyl remember "<title>" "<body>"`
- "that memory is wrong/stale/superseded" -> `sibyl blame <id>` then `sibyl correct <id> ...`
- "consolidate this session" -> `sibyl reflect "<notes>" --persist`

Live status: `sibyl context` shows project link and auth; `sibyl health` shows server reachability.

Useful follow-ups:

```bash
sibyl skill list
sibyl skill get workflows
sibyl skill get examples
```

Hooks are separate from skills. Install or update hooks only when the user explicitly wants
automatic prompt/session integration.
