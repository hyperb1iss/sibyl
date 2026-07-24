# Context and Context Configuration

`sibyl context` recalls agent-ready working memory for a goal. Named CLI contexts bundle server URL,
organization, and project settings under `sibyl config context`.

## Overview

A context contains:

- **Server URL**: Where the Sibyl API is running
- **Organization**: Which org to use (optional)
- **Default Project**: Fallback project for operations
- **Insecure**: Whether to skip SSL verification

## Commands

- `sibyl context <goal>` - Compile a context pack for an agent
- `sibyl config context` - Show current CLI context
- `sibyl config context pack` - Compile a context pack via the config group
- `sibyl config context list` - List all contexts
- `sibyl config context show` - Show context details
- `sibyl config context create` - Create a context
- `sibyl config context use` - Set active context
- `sibyl config context link` - Pin a directory tree to a context
- `sibyl config context unlink` - Remove a directory's context pin
- `sibyl config context update` - Update a context
- `sibyl config context delete` - Delete a context
- `sibyl config context clear` - Clear active context

---

## config context (no subcommand)

Show the current active context.

### Synopsis

```bash
sibyl config context [options]
```

### Options

| Option                   | Short | Description                                    |
| ------------------------ | ----- | ---------------------------------------------- |
| `--json`                 | `-j`  | JSON output                                    |
| `--quick` / `--validate` |       | Show local server/org/project/auth status only |

### Example

```bash
sibyl config context
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

## context

Compile a precise context pack for an agent. The hidden `search` and `recall` compatibility aliases
route here too; new instructions should use `context`.

### Synopsis

```bash
sibyl context <goal> [options]
```

### Arguments

| Argument | Required | Description             |
| -------- | -------- | ----------------------- |
| `goal`   | Yes      | Agent goal or user task |

### Options

| Option      | Short | Default  | Description                                      |
| ----------- | ----- | -------- | ------------------------------------------------ |
| `--intent`  | `-i`  | `build`  | `build`, `plan`, `review`, `debug`, or `general` |
| `--layer`   |       | `recall` | `wake`, `recall`, or `deep_search`               |
| `--domain`  | `-d`  | (none)   | Domain/category to bias retrieval                |
| `--project` | `-p`  | (auto)   | Project ID to scope context                      |
| `--agent`   |       | (none)   | Agent diary identity to include                  |
| `--all`     | `-a`  | false    | Use all accessible projects                      |
| `--limit`   | `-l`  | 12       | Maximum context items (1-50)                     |
| `--related` |       | on       | Include one-hop related graph context            |
| `--audit`   |       | false    | Include full retrieval metadata                  |
| `--budget`  |       | (none)   | Approximate Markdown token budget                |
| `--json`    | `-j`  | false    | JSON output                                      |

### Raw-Memory Filters

These flags switch recall to verbatim raw memory and narrow which raw imports qualify:

| Option              | Default   | Description                                          |
| ------------------- | --------- | ---------------------------------------------------- |
| `--raw`             | false     | Recall verbatim raw memories                         |
| `--diary`           | false     | Recall a private agent diary                         |
| `--scope`           | `private` | Memory scope: `private`, `project`, `team`, or `org` |
| `--scope-key`       | (none)    | Project/team/shared scope key                        |
| `--participant`     | (none)    | Filter raw imports by participant                    |
| `--label`           | (none)    | Filter raw imports by adapter label                  |
| `--thread`          | (none)    | Filter raw imports by thread                         |
| `--occurred-after`  | (none)    | Filter raw imports after an ISO timestamp            |
| `--occurred-before` | (none)    | Filter raw imports before an ISO timestamp           |
| `--as-of`           | (none)    | Filter raw memory by validity timestamp              |

### Examples

```bash
# Compile a context pack for a goal
sibyl context "implement the password reset endpoint"

# Markdown output for direct agent injection
sibyl context "debug the auth refresh bug" --intent debug

# Deep search with a wider item budget
sibyl context "how synthesis verification works" \
  --layer deep_search --limit 40

# Verbatim raw memories from a team scope, time-bounded
sibyl context "auth outage timeline" \
  --raw --scope team --scope-key platform --occurred-after 2026-07-01
```

---

## config context pack

Compile a precise context pack for an agent from the config group. Same compiler as `sibyl context`,
with pack-tuning flags and a wider intent list.

### Synopsis

```bash
sibyl config context pack <goal> [options]
```

### Arguments

| Argument | Required | Description             |
| -------- | -------- | ----------------------- |
| `goal`   | Yes      | Agent goal or user task |

### Options

| Option            | Short | Default  | Description                                                                  |
| ----------------- | ----- | -------- | ---------------------------------------------------------------------------- |
| `--intent`        | `-i`  | `build`  | `build`, `plan`, `ideate`, `research`, `debug`, `decide`, `learn`, `general` |
| `--layer`         |       | `recall` | `wake`, `recall`, or `deep_search`                                           |
| `--domain`        | `-d`  | (none)   | Domain/category to bias retrieval                                            |
| `--project`       | `-p`  | (auto)   | Project ID to scope context                                                  |
| `--agent`         |       | (none)   | Agent diary identity to include                                              |
| `--all`           | `-a`  | false    | Use all accessible projects                                                  |
| `--limit`         | `-l`  | 24       | Maximum total context items (1-50)                                           |
| `--related`       |       | on       | Include one-hop related graph context                                        |
| `--related-limit` |       | 3        | Related items per context item (0-5)                                         |
| `--markdown`      | `-m`  | false    | Output compact Markdown for agent injection                                  |
| `--audit`         |       | false    | Include full retrieval metadata                                              |
| `--budget`        |       | (none)   | Approximate Markdown token budget                                            |
| `--json`          | `-j`  | false    | JSON output                                                                  |

### Example

```bash
sibyl config context pack "plan the retrieval refactor" --intent plan --markdown
```

---

## config context list

List all configured contexts.

### Synopsis

```bash
sibyl config context list [options]
```

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl config context list
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

## config context show

Show details of a specific context.

### Synopsis

```bash
sibyl config context show [name] [options]
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
sibyl config context show prod
```

Output:

```
  Context: prod

  Server:   https://sibyl.example.com
  Org:      myorg
  Project:  proj_main
```

---

## config context create

Create a new context.

### Synopsis

```bash
sibyl config context create <name> [options]
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
sibyl config context create local --server http://localhost:3334

# Create production context and activate it
sibyl config context create prod \
  --server https://sibyl.example.com \
  --org myorg \
  --project proj_main \
  --use

# Create staging with self-signed cert
sibyl config context create staging \
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

## config context use

Set the active context. This affects all subsequent commands.

### Synopsis

```bash
sibyl config context use <name> [options]
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
sibyl config context use prod
```

Output:

```
Switched to context 'prod'
  Server: https://sibyl.example.com
```

---

## config context link

Pin a directory tree to a context so commands run there route to its server. Unlike
`sibyl project link`, this binds only the context (server/org), not a project, so new repositories
under the tree route to the right server before they are linked to a specific project.

### Synopsis

```bash
sibyl config context link <name> [options]
```

### Arguments

| Argument | Required | Description                           |
| -------- | -------- | ------------------------------------- |
| `name`   | Yes      | Context name to pin to this directory |

### Options

| Option   | Short | Default | Description    |
| -------- | ----- | ------- | -------------- |
| `--path` | `-p`  | (cwd)   | Directory path |

### Example

```bash
cd ~/work && sibyl config context link work
```

---

## config context unlink

Remove the context pin from a directory. Any project link on the directory is kept.

### Synopsis

```bash
sibyl config context unlink [options]
```

### Options

| Option   | Short | Default | Description    |
| -------- | ----- | ------- | -------------- |
| `--path` | `-p`  | (cwd)   | Directory path |

### Example

```bash
sibyl config context unlink --path ~/work
```

---

## config context update

Update an existing context.

### Synopsis

```bash
sibyl config context update <name> [options]
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
sibyl config context update prod --server https://new-sibyl.example.com

# Change default project
sibyl config context update staging --project proj_new_staging

# Clear organization (use auto-detect)
sibyl config context update local --org auto

# Clear default project
sibyl config context update dev --project none

# Enable insecure mode
sibyl config context update staging --insecure

# Disable insecure mode
sibyl config context update staging --secure
```

---

## config context delete

Delete a context.

### Synopsis

```bash
sibyl config context delete <name>
```

### Arguments

| Argument | Required | Description            |
| -------- | -------- | ---------------------- |
| `name`   | Yes      | Context name to delete |

### Example

```bash
sibyl config context delete old-staging
```

Output:

```
Deleted context 'old-staging'
```

If you delete the active context:

```
Deleted context 'local'
No active context. Use 'sibyl config context use <name>' to set one.
```

---

## config context clear

Clear the active context. Falls back to legacy `server.url` from config.

### Synopsis

```bash
sibyl config context clear
```

### Example

```bash
sibyl config context clear
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
sibyl config context create local --server http://localhost:3334 --use
sibyl health
```

Everything else on this page is for people juggling multiple servers or orgs.

### Development Setup

```bash
# Create contexts for different environments
sibyl config context create local --server http://localhost:3334 --use
sibyl config context create staging --server https://staging.sibyl.io --org myorg
sibyl config context create prod --server https://sibyl.example.com --org myorg

# Switch between environments
sibyl config context use local
sibyl config context use staging
sibyl config context use prod
```

### CI/CD Integration

```bash
# In CI pipeline
sibyl config context create ci \
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
sibyl config context create work --server https://sibyl.company.com --org company
sibyl config context create personal --server https://sibyl.io --org personal

# Switch organizations
sibyl config context use work
sibyl config context use personal
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

- `sibyl config context` - Manage named server, org, and project contexts
- [`sibyl auth login`](./auth.md) - Log in and create a context in one step
- [`sibyl project link`](./project.md) - Link directory to project
- [`sibyl config`](./index.md) - Configuration management
