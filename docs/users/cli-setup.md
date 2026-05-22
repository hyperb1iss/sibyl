---
title: CLI Setup
description: Installing and authenticating the Sibyl CLI
---

# CLI Setup

The `sibyl` CLI is the fastest way to recall memory, capture learnings, manage tasks, and create API
keys for MCP clients.

## Install

Homebrew is the preferred install path on macOS and Linux:

```bash
brew install hyperb1iss/tap/sibyl
```

For remote-only installs, the shell installer skips the local daemon and web UI:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh -s -- --remote
```

If your environment distributes the Python package directly, install the CLI package with your
Python tool of choice:

```bash
python -m pip install sibyl-dev
```

## Point The CLI At Your Server

Create a remote context for the enterprise server:

```bash
sibyl init --remote https://sibyl.example.com
sibyl auth login
```

`sibyl auth login` opens the browser sign-in flow. In enterprise deployments, that browser flow uses
the same corporate OIDC provider as the web app.

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

MCP clients and automation should use API keys, not copied browser cookies. In the web UI, open
Settings, Security, API Keys and create a key with the right scope. From the CLI:

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
sibyl recall "current project context"
sibyl remember "Deployment gotcha" "The restore drill needs the export PVC mounted"
sibyl task list --status doing
```

If a command says auth is required, run `sibyl auth login` again. If an API key fails, revoke it in
Settings and create a new scoped key.
