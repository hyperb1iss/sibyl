# context

Manage CLI contexts. Contexts bundle server URL, organization, and project settings for easy
switching between environments (local, staging, production).

## Overview

A context contains:

- **Server URL**: Where the Sibyl API is running
- **Organization**: Which org to use (optional)
- **Default Project**: Fallback project for operations
- **Insecure**: Whether to skip SSL verification

## Commands

- `sibyl context` - Show current context
- `sibyl context pack` - Compile a context pack for an agent
- `sibyl context list` - List all contexts
- `sibyl context show` - Show context details
- `sibyl context create` - Create a context
- `sibyl context use` - Set active context
- `sibyl context update` - Update a context
- `sibyl context delete` - Delete a context
- `sibyl context clear` - Clear active context

---

## context (no subcommand)

Show the current active context.

### Synopsis

```bash
sibyl context [options]
```

### Options

| Option                   | Short | Description                                    |
| ------------------------ | ----- | ---------------------------------------------- |
| `--json`                 | `-j`  | JSON output                                    |
| `--quick` / `--validate` |       | Show local server/org/project/auth status only |

### Example

```bash
sibyl context
```

Output:

```
  Context: local
  (active)

  Server:   http://localhost:3334
  Org:      auto
  Project:  proj_abc123 (linked)
```

If a directory is linked, it shows `(linked)` next to the project. Use `--quick` for a fast local
status check that skips fetching full project detail.

---

## context pack

Compile a precise context pack for an agent. This is the lower-level command behind
[`sibyl recall`](./recall.md): it builds a goal-scoped bundle of tasks, decisions, and graph
neighbors. Use `recall` for everyday work and `context pack` when you need fine-grained control over
the pack.

### Synopsis

```bash
sibyl context pack <goal> [options]
```

### Arguments

| Argument | Required | Description             |
| -------- | -------- | ----------------------- |
| `goal`   | Yes      | Agent goal or user task |

### Options

| Option            | Short | Default  | Description                                                                        |
| ----------------- | ----- | -------- | ---------------------------------------------------------------------------------- |
| `--intent`        | `-i`  | `build`  | Agent intent: build, plan, ideate, research, review, debug, decide, learn, general |
| `--layer`         |       | `recall` | Context depth: `wake`, `recall`, `deep_search`                                     |
| `--domain`        | `-d`  | (none)   | Domain/category to bias retrieval                                                  |
| `--project`       | `-p`  | (auto)   | Project ID to scope context                                                        |
| `--agent`         |       | (none)   | Agent diary identity to include                                                    |
| `--all`           | `-a`  | false    | Use all accessible projects                                                        |
| `--limit`         | `-l`  | 24       | Maximum total context items (1-50)                                                 |
| `--related`       |       | on       | Include one-hop related graph context (`--no-related`)                             |
| `--related-limit` |       | 3        | Related items per context item (0-5)                                               |
| `--markdown`      | `-m`  | false    | Output compact Markdown for agent injection                                        |
| `--json`          | `-j`  | false    | JSON output                                                                        |

### Examples

```bash
# Compile a context pack for a goal
sibyl context pack "implement the password reset endpoint"

# Markdown output for direct agent injection
sibyl context pack "debug the auth refresh bug" --intent debug --markdown

# Deep search with a wider item budget
sibyl context pack "how synthesis verification works" \
  --layer deep_search --limit 40
```

---

## context list

List all configured contexts.

### Synopsis

```bash
sibyl context list [options]
```

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl context list
```

Output:

```
Contexts
        Name     Server                         Org        Project
───────────────────────────────────────────────────────────────────
*       local    http://localhost:3334          auto       none
        staging  https://staging.sibyl.io       myorg      proj_staging
        prod     https://sibyl.example.com      myorg      proj_main

* = active context
```

---

## context show

Show details of a specific context.

### Synopsis

```bash
sibyl context show [name] [options]
```

### Arguments

| Argument | Required | Description                       |
| -------- | -------- | --------------------------------- |
| `name`   | No       | Context name (defaults to active) |

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl context show prod
```

Output:

```
  Context: prod

  Server:   https://sibyl.example.com
  Org:      myorg
  Project:  proj_main
```

---

## context create

Create a new context.

### Synopsis

```bash
sibyl context create <name> [options]
```

### Arguments

| Argument | Required | Description                          |
| -------- | -------- | ------------------------------------ |
| `name`   | Yes      | Context name (e.g., 'prod', 'local') |

### Options

| Option       | Short | Default                 | Description           |
| ------------ | ----- | ----------------------- | --------------------- |
| `--server`   | `-s`  | `http://localhost:3334` | Server URL            |
| `--org`      | `-o`  | (auto)                  | Organization slug     |
| `--project`  | `-p`  | (none)                  | Default project ID    |
| `--use`      | `-u`  | false                   | Set as active context |
| `--insecure` | `-k`  | false                   | Skip SSL verification |
| `--json`     | `-j`  | false                   | JSON output           |

### Examples

```bash
# Create local development context
sibyl context create local --server http://localhost:3334

# Create production context and activate it
sibyl context create prod \
  --server https://sibyl.example.com \
  --org myorg \
  --project proj_main \
  --use

# Create staging with self-signed cert
sibyl context create staging \
  --server https://staging.internal:3334 \
  --insecure
```

Output:

```
Created context 'prod'
Set as active context
  Server:  https://sibyl.example.com
  Org:     myorg
  Project: proj_main
```

---

## context use

Set the active context. This affects all subsequent commands.

### Synopsis

```bash
sibyl context use <name> [options]
```

### Arguments

| Argument | Required | Description              |
| -------- | -------- | ------------------------ |
| `name`   | Yes      | Context name to activate |

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl context use prod
```

Output:

```
Switched to context 'prod'
  Server: https://sibyl.example.com
```

---

## context update

Update an existing context.

### Synopsis

```bash
sibyl context update <name> [options]
```

### Arguments

| Argument | Required | Description            |
| -------- | -------- | ---------------------- |
| `name`   | Yes      | Context name to update |

### Options

| Option       | Short | Description                               |
| ------------ | ----- | ----------------------------------------- |
| `--server`   | `-s`  | New server URL                            |
| `--org`      | `-o`  | New org slug (use 'auto' to clear)        |
| `--project`  | `-p`  | New default project (use 'none' to clear) |
| `--insecure` | `-k`  | Skip SSL verification                     |
| `--secure`   |       | Re-enable SSL verification                |
| `--json`     | `-j`  | JSON output                               |

### Examples

```bash
# Update server URL
sibyl context update prod --server https://new-sibyl.example.com

# Change default project
sibyl context update staging --project proj_new_staging

# Clear organization (use auto-detect)
sibyl context update local --org auto

# Clear default project
sibyl context update dev --project none

# Enable insecure mode
sibyl context update staging --insecure

# Disable insecure mode
sibyl context update staging --secure
```

---

## context delete

Delete a context.

### Synopsis

```bash
sibyl context delete <name>
```

### Arguments

| Argument | Required | Description            |
| -------- | -------- | ---------------------- |
| `name`   | Yes      | Context name to delete |

### Example

```bash
sibyl context delete old-staging
```

Output:

```
Deleted context 'old-staging'
```

If you delete the active context:

```
Deleted context 'local'
No active context. Use 'sibyl context use <name>' to set one.
```

---

## context clear

Clear the active context. Falls back to legacy `server.url` from config.

### Synopsis

```bash
sibyl context clear
```

### Example

```bash
sibyl context clear
```

Output:

```
Cleared active context
Using legacy server.url from config
```

---

## Context Priority

When resolving project context, the CLI checks in this order:

1. `--context` / `-C` global flag (highest priority)
2. `SIBYL_CONTEXT` environment variable
3. Active context's default project
4. Path-based project link (from current directory)

### Override with Flag

```bash
# Use different project for one command
sibyl --context proj_other task list
sibyl -C proj_other task list
```

### Override with Environment

```bash
# Use different project for shell session
export SIBYL_CONTEXT=proj_other
sibyl task list  # Uses proj_other
```

---

## Common Workflows

### Solo / Local Only

Most personal installs never need more than one context. `sibyl up` sets up a local context for you
automatically; to create it by hand, this is the whole setup:

```bash
sibyl context create local --server http://localhost:3334 --use
sibyl health
```

Everything else on this page is for people juggling multiple servers or orgs.

### Development Setup

```bash
# Create contexts for different environments
sibyl context create local --server http://localhost:3334 --use
sibyl context create staging --server https://staging.sibyl.io --org myorg
sibyl context create prod --server https://sibyl.example.com --org myorg

# Switch between environments
sibyl context use local
sibyl context use staging
sibyl context use prod
```

### CI/CD Integration

```bash
# In CI pipeline
sibyl context create ci \
  --server "$SIBYL_URL" \
  --org "$SIBYL_ORG" \
  --use

# Or use environment variable
export SIBYL_CONTEXT=proj_ci
sibyl task list --status todo
```

### Multiple Organizations

```bash
# Create context per org
sibyl context create work --server https://sibyl.company.com --org company
sibyl context create personal --server https://sibyl.io --org personal

# Switch organizations
sibyl context use work
sibyl context use personal
```

## Configuration File

Contexts are stored in `~/.sibyl/config.toml`:

```toml
[context]
active = "local"

[contexts.local]
server_url = "http://localhost:3334"
org_slug = ""
default_project = ""
insecure = false

[contexts.prod]
server_url = "https://sibyl.example.com"
org_slug = "myorg"
default_project = "proj_main"
insecure = false

[contexts.staging]
server_url = "https://staging.internal:3334"
org_slug = "myorg"
default_project = ""
insecure = true
```

## Related Commands

- [`sibyl recall`](./recall.md) - Recall a working context pack (built on `context pack`)
- [`sibyl auth login`](./auth.md) - Log in and create a context in one step
- [`sibyl project link`](./project.md) - Link directory to project
- [`sibyl config`](./index.md) - Configuration management
