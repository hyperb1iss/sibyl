---
title: Quick Start
description: Install Sibyl and run your first memory loop in five minutes
---

# Quick Start

This guide takes you from nothing to your first captured memory, in about five minutes.

::: tip Working on Sibyl itself? This guide is for _using_ Sibyl. To set up the monorepo for
development, see [Installation](./installation.md). :::

## Step 1: Connect to a Sibyl server

If your team already runs Sibyl, install the CLI and run `sibyl setup` with the server's URL:

```bash
# macOS
brew install hyperb1iss/tap/sibyl && sibyl setup https://sibyl.example.com

# Linux, or anywhere with uv
uv tool install --upgrade sibyl-dev && sibyl setup https://sibyl.example.com
```

[`sibyl setup`](../cli/setup.md) signs you in through the browser, installs the Sibyl skill for your
agents, and adds the Claude Code SessionStart hook. You can also give your agent one sentence and
let it run the same steps:

```text
Set up Sibyl on this machine by following https://sibyl.example.com/agent
```

Connected? Skip to [Step 3](#step-3-run-the-memory-loop).

## Step 2: No server yet? Run one locally

The shell installer starts the local API + web stack and opens the setup UI:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh
```

Already use Homebrew? Install the package, then start the local UI:

```bash
brew install hyperb1iss/tap/sibyl
sibyl up
```

| Service   | URL                   |
| --------- | --------------------- |
| Web UI    | http://localhost:3337 |
| API + MCP | http://localhost:3334 |

The first time you open the web UI, a setup wizard runs:

1. **Welcome:** names the model providers the server already has ready, if any.
2. **API keys:** shown only when no provider is ready. Sibyl needs a language model (Anthropic,
   OpenAI, Gemini, or Claude through Amazon Bedrock) and an embedding provider (OpenAI, Gemini, or
   Cohere Embed v4 through Bedrock).
3. **Admin account:** the first account, which holds owner privileges.
4. **Connect:** the one line that connects a terminal, and the sentence to hand an agent.

Then connect this machine:

```bash
sibyl setup
```

Without a URL, `sibyl setup` connects the server the CLI already talks to, which is
`http://localhost:3334` on a fresh install.

## Step 3: Run the memory loop

Sibyl's core is a loop: **recall, act, remember, reflect**. Try it.

Capture something worth keeping:

```bash
sibyl remember "Async gotcha" \
  "Use asyncio.gather for concurrent awaits, not a sequential loop" \
  --kind pattern
```

Pull it back as working context:

```bash
sibyl context "async concurrency" --intent build
```

Or load context across every accessible project:

```bash
sibyl context "review prior async concurrency lessons" --intent review --all
```

Semantic search finds that memory even though you searched with different words.

Pull the full record back with `sibyl show <id>`. You can also seed memory from past agent sessions:
`sibyl ingest claude-code <path>` and `sibyl ingest codex <path>` import transcript JSONL.

## Step 4: Use Sibyl from your agent

Sibyl earns its keep when your AI agent uses it too. After `sibyl setup`, an agent that supports
skills (Claude Code, Codex, and others) loads the workflow with `/sibyl`, and any agent that can run
a shell command reaches Sibyl through the `sibyl` CLI. Clients that only speak MCP connect to the
`/mcp` endpoint with an API key; see [Agents & MCP](./claude-code.md).

## Where to go next

- [The Memory Loop](./memory-loop.md): recall, act, remember, reflect
- [Capturing Knowledge](./capturing-knowledge.md): what is worth saving
- [Task Management](./task-management.md): plan and track work
- [Agents & MCP](./claude-code.md): connect any AI agent

## Common commands

| Action           | Command                                      |
| ---------------- | -------------------------------------------- |
| Connect a server | `sibyl setup <url>`                          |
| Capture a memory | `sibyl remember "Title" "What matters"`      |
| Load context     | `sibyl context "goal" --intent build`        |
| Search broadly   | `sibyl context "query" --all`                |
| Show a record    | `sibyl show <id>`                            |
| Create a task    | `sibyl task create --title "..."`            |
| Complete a task  | `sibyl task complete <id> --learnings "..."` |
| Link a repo      | `sibyl project link <id>`                    |
| Check health     | `sibyl health`                               |
