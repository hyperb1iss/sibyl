---
title: Quick Start
description: Install Sibyl and run your first memory loop in five minutes
---

# Quick Start

This guide takes you from nothing to a running Sibyl with your first captured memory, in about five
minutes.

::: tip Working on Sibyl itself? This guide is for _using_ Sibyl. To set up the monorepo for
development, see [Installation](./installation.md). :::

## Step 1: Install Sibyl

The shell installer puts the CLI and the local daemon on your PATH:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh
```

Already use Homebrew?

```bash
brew install hyperb1iss/tap/sibyl
```

## Step 2: Start Sibyl

For a terminal-first local daemon:

```bash
sibyl init --local
sibyl serve
```

For the full API + web self-host stack:

```bash
sibyl docker init
sibyl docker up
```

| Service   | URL                   |
| --------- | --------------------- |
| Web UI    | http://localhost:3337 |
| API + MCP | http://localhost:3334 |

## Step 3: Finish setup in the browser

The first time you open the web UI, a setup wizard runs. It walks you through three things:

1. **API keys** — Sibyl needs an Anthropic key for entity extraction and an OpenAI or Gemini key for
   embeddings.
2. **Admin account** — the first account, which holds owner privileges.
3. **Connect** — how to start using Sibyl from the terminal or an agent.

## Step 4: Connect the CLI

Confirm the CLI can reach your local server:

```bash
sibyl health
```

Sibyl is now yours from any terminal.

## Step 5: Run the memory loop

Sibyl's core is a loop: **recall, act, remember, reflect**. Try it.

Capture something worth keeping:

```bash
sibyl remember "Async gotcha" \
  "Use asyncio.gather for concurrent awaits, not a sequential loop" \
  --kind pattern
```

Pull it back as working context:

```bash
sibyl recall "async concurrency" --intent build
```

Or search the whole graph by meaning:

```bash
sibyl search "running awaits at the same time"
```

Semantic search finds that memory even though you searched with different words.

## Step 6: Connect your AI agent

Sibyl earns its keep when your AI agent uses it too. Any agent can reach Sibyl through the `sibyl`
CLI, and MCP-capable agents (Claude Code, Codex, opencode, OpenClaw, and others) can connect to the
MCP endpoint.

Install the Sibyl skill and hooks for Claude Code and Codex:

```bash
sibyl local setup
```

For per-client MCP configuration and the agent prompt snippet, see [Agents & MCP](./claude-code.md).

## Where to go next

- [The Memory Loop](./memory-loop.md) — recall, act, remember, reflect
- [Capturing Knowledge](./capturing-knowledge.md) — what is worth saving
- [Task Management](./task-management.md) — plan and track work
- [Agents & MCP](./claude-code.md) — connect any AI agent

## Common commands

| Action           | Command                                      |
| ---------------- | -------------------------------------------- |
| Capture a memory | `sibyl remember "Title" "What matters"`      |
| Recall context   | `sibyl recall "goal" --intent build`         |
| Search the graph | `sibyl search "query"`                       |
| Create a task    | `sibyl task create --title "..."`            |
| Complete a task  | `sibyl task complete <id> --learnings "..."` |
| Link a repo      | `sibyl project link <id>`                    |
| Check health     | `sibyl health`                               |
