---
title: Run Sibyl for Yourself
description: The solo self-host path - one command to a private memory graph on your own machine
---

# Run Sibyl for Yourself

Sibyl's default shape is personal. No company, no identity provider, no hosted Sibyl tenant. One
command brings up a private knowledge graph on your own machine, you create your own owner account,
and every agent you run talks to it at `localhost`. Your memory graph lives on your hardware and
stays there. The one caveat: extraction and embeddings call the AI providers you configure
(Anthropic for entity extraction, OpenAI or Gemini for embeddings), so that text is sent to those
APIs the same way it would be with any tool that uses them.

This page is the end-to-end solo path. If you administer Sibyl for a team behind corporate SSO, see
[Self-Hosting & Admin](../admin/installing.md) instead.

## What You Get

- A local SurrealDB knowledge graph, running on your box
- The full memory loop (`context → act → remember → reflect`) from any terminal
- A web UI at `http://localhost:3337` for the graph explorer, tasks, and the memory workspace
- The `sibyl` CLI as your agents' main interface (any tool that runs shell commands can use it),
  with an MCP endpoint at `http://localhost:3334/mcp` for MCP-native clients
- One owner account: you

## Step 1: Install and Start

The batteries-included path runs the whole stack (API, web UI, SurrealDB) in Docker and opens the
setup wizard when it is ready:

```bash
# Homebrew (macOS / Linux)
brew install hyperb1iss/tap/sibyl
sibyl up

# or the shell installer
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh
```

`sibyl up` is the front door for personal use. On first run it reads `OPENAI_API_KEY` and
`ANTHROPIC_API_KEY` from your environment if they are set, generates its own secrets, and writes a
compose file plus `.env` under `~/.sibyl/local` before opening your browser. If those keys are not
in your environment, you add them in the setup wizard instead (Step 2). Everything binds to
`127.0.0.1`, so nothing is exposed to your network.

::: tip Prefer no Docker? Run the embedded daemon directly with `sibyl init --local` then
`sibyl serve`. See [Keeping It Running](#keeping-it-running) for the daemon options. :::

## Step 2: Finish Setup in the Browser

The first time the web UI opens at `http://localhost:3337`, a short wizard runs:

1. **API keys:** Sibyl needs an Anthropic key for entity extraction and an OpenAI or Gemini key for
   embeddings. Keys you enter in the wizard are stored encrypted in your local database.
2. **Your owner account:** the first account you create holds owner privileges. This is local
   username/password auth; there is no external sign-in to configure. After setup, new accounts are
   invite-only unless you deliberately turn on public signups.
3. **Connect:** the wizard shows the MCP config and agent prompt snippet for your tools.

That is the whole account story for a solo install. No OIDC, no role claims, no identity provider.

## Step 3: Point the CLI at Your Local Server

A fresh Sibyl CLI already defaults to `http://localhost:3334`, so on a clean setup you can go
straight to a health check:

```bash
sibyl health
```

`sibyl up` starts the server but does not change your CLI's active context. If you previously
pointed the CLI at a remote server, create or switch to the local context explicitly (it defaults to
localhost, so no URL is needed):

```bash
sibyl init --local        # or: sibyl config context use local
sibyl doctor
```

Sibyl is now yours from any terminal.

## Step 4: Connect Your Agent

The primary way an agent uses Sibyl is the `sibyl` CLI. If a tool can run a shell command, it can
use Sibyl: the agent runs `sibyl context`, `sibyl remember`, and the other task or correction verbs
against the local server from Step 3. This is the recommended path: it is lighter weight than MCP
(less token overhead) and every Sibyl command is available. Sign in once with `sibyl auth login` if
a write reports that authentication is required.

Teach your agent the workflow by installing the Sibyl skill (and, for Claude Code, the session
hooks):

```bash
sibyl local setup
```

This installs the `sibyl` skill for Claude Code and Codex. In a client that supports skills,
`/sibyl` loads the full memory-loop workflow; run `sibyl local setup --snippet` to print a prompt
snippet you can paste into any agent's instructions instead. See
[Working with Agents](./working-with-agents.md) and [Skills & Hooks](./skills.md).

### Prefer MCP tools?

MCP-native clients can also connect to the endpoint at `http://localhost:3334/mcp`. Reach for MCP
only when a client works better with structured tool calls; otherwise the CLI is the lighter path.
`sibyl up` generates a signing secret on first run, so MCP auth is on by default. Create a scoped
key and add it to your client config:

```bash
sibyl auth api-key create --name "claude-code" --scopes mcp
```

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp",
      "headers": {
        "Authorization": "Bearer sk_live_replace_me"
      }
    }
  }
}
```

For an unauthenticated local-only endpoint, set `SIBYL_MCP_AUTH_MODE=off` and drop the header. See
[Agents & MCP](./claude-code.md) and [MCP Configuration](./mcp-configuration.md) for per-client
details.

## Step 5: Run the Memory Loop

The payoff. Capture something worth keeping, then pull it back:

```bash
# Remember a hard-won gotcha
sibyl remember "Async gotcha" \
  "Use asyncio.gather for concurrent awaits, not a sequential loop" \
  --kind error_pattern

# Load it as working context before you act
sibyl context "async concurrency" --intent build

# Or load context across every accessible project
sibyl context "review prior async concurrency lessons" --intent review --all
```

Link a repo so commands auto-scope to it:

```bash
cd ~/dev/my-project
sibyl project link <project_id>
```

Everything you capture lives in your own graph. On a solo install you naturally work in a single
auto-created org, so you can ignore org and multi-tenancy concepts entirely until you have a reason
to care about them.

## Keeping It Running

You have three ways to run the server on your own machine. Pick one:

| Command                                               | What it is                                        | Best for                                       |
| ----------------------------------------------------- | ------------------------------------------------- | ---------------------------------------------- |
| [`sibyl up` / `sibyl local`](../cli/local.md)         | Batteries-included Docker stack, `~/.sibyl/local` | The default. Easiest personal instance.        |
| [`sibyl serve` / `start` / `stop`](../cli/service.md) | Embedded native daemon, no Docker                 | Lightweight, no container runtime              |
| [`sibyl docker`](../cli/docker.md)                    | Pinned Docker stack with explicit image tags      | Reproducible upgrades, worker/crawler services |

Common lifecycle commands:

```bash
sibyl up            # start the local stack and open the UI
sibyl down          # stop it (data persists)
sibyl local status  # is it running?
sibyl local logs    # tail the logs
```

To keep the daemon alive across reboots without Docker, install a native user service:

```bash
sibyl service install
```

## Going Remote Later

Nothing about the solo path locks you in. If you later want Sibyl on a small cloud VM you can reach
from anywhere, the [single-host Ansible guide](../deployment/ansible.md) provisions one box with
TLS, pairs cleanly with Tailscale for a private zero-public-port setup, and keeps the same
local-first auth. Point any CLI at it with `sibyl init --remote https://your-host`.

## Where to Go Next

- [The Memory Loop](./memory-loop.md): context, act, remember, reflect
- [Capturing Knowledge](./capturing-knowledge.md): what is worth saving
- [Agents & MCP](./claude-code.md): connect any AI agent
- [Skills & Hooks](./skills.md): automatic context injection
- [Single-Host Deployment](../deployment/ansible.md): your own always-on instance on a VM
