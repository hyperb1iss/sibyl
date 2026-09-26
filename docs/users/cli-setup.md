---
title: CLI Setup
description: Installing and authenticating the Sibyl CLI
---

# CLI Setup

The `sibyl` CLI is the fastest way to recall memory, capture learnings, manage tasks, and create API
keys for MCP clients.

## Connect To Your Server

Install the CLI and run [`sibyl setup`](../cli/setup.md) with your server's URL. It creates the
context, signs you in, and installs the skill and the Claude Code hook:

```bash
# macOS
brew install hyperb1iss/tap/sibyl && sibyl setup https://your-sibyl-host

# Linux, or anywhere with uv
uv tool install --upgrade sibyl-dev && sibyl setup https://your-sibyl-host

# Windows (PowerShell)
uv tool install --upgrade sibyl-dev; sibyl setup 'https://your-sibyl-host'
```

Keep `--upgrade` on the uv line: a plain `uv tool install` leaves an older CLI in place. When the
server needs a newer CLI, `sibyl setup` stops and prints the upgrade command. No uv yet? The shell
installer bootstraps it, then runs `sibyl setup`:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh -s -- --remote https://your-sibyl-host
```

To let your agent do all of this, give it one sentence:

```text
Set up Sibyl on this machine by following https://your-sibyl-host/agent
```

Copy the exact sentence from the web app's Connect card: `/agent` is served by the web app, so on a
deployment where the web app and API have separate origins the card points at `/api/setup/agent.md`
on the API instead. See [Hand It To An Agent](../cli/setup.md#hand-it-to-an-agent).

The sign-in opens the browser. On a team server behind corporate SSO, that browser flow uses the
same OIDC provider as the web app. You never enter provider API keys to connect; the server's
operator configures models once. To do the steps by hand, run `sibyl init --remote <url>` and then
`sibyl auth login`.

### A Local Server

A fresh Sibyl CLI defaults to `http://localhost:3334`, so if you run Sibyl locally with `sibyl up`,
`sibyl setup` with no URL connects it. `sibyl up` does not change your CLI's active context, though,
so if you previously pointed it at a remote server, switch back to the local context (it defaults to
localhost, so no URL is needed):

```bash
sibyl init --local        # or: sibyl config context use local
sibyl doctor
```

### Signing In By Hand

For headless terminals, print the login URL instead:

```bash
sibyl auth login --no-browser
```

After login, confirm the active context:

```bash
sibyl auth status
sibyl whoami
```

## Create An API Key

You only need a key for an MCP-only client or for automation; `sibyl setup` signs the CLI in without
one. Use API keys there, not copied browser cookies. In the web UI, open Settings, Security, API
Keys and create a key with the right scope. From the CLI:

```bash
sibyl auth api-key create --name "claude-code" --scopes mcp
```

For script access to the REST API, use explicit API scopes:

```bash
sibyl auth api-key create --name "ci-readonly" \
  --scopes api:read \
  --expires-days 90
```

The full key is shown once. Store it in your password manager or client secret store immediately.

## Daily Checks

```bash
sibyl context "current project context"
sibyl remember "Deployment gotcha" "The restore drill needs the export PVC mounted"
sibyl task list --status doing
```

If a command says auth is required, run `sibyl auth login` again. If an API key fails, revoke it in
Settings and create a new scoped key.
