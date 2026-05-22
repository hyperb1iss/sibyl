# auth

Authentication and credentials. `auth` logs the CLI into a Sibyl server, manages stored tokens, and
creates API keys for MCP clients and scripts.

Top-level aliases are available for the common path: `sibyl login`, `sibyl logout`, and
`sibyl whoami`.

## Commands

| Command                                         | Description                              |
| ----------------------------------------------- | ---------------------------------------- |
| [`sibyl auth login`](#auth-login)               | Log in to a server and save credentials  |
| [`sibyl auth status`](#auth-status)             | Show auth status for the current context |
| [`sibyl auth local-signup`](#auth-local-signup) | Create a local user and save its token   |
| [`sibyl auth set-token`](#auth-set-token)       | Set an auth token for a server           |
| [`sibyl auth clear-token`](#auth-clear-token)   | Clear auth tokens for a server           |
| [`sibyl auth api-key`](#auth-api-key)           | API key management                       |

---

## auth login

Login to a Sibyl server and save credentials. With no URL, logs in to the active context or the
default server. Login opens a browser for the auth flow unless `--no-browser` is set, or you can
pass `--email` and `--password` for local login.

### Synopsis

```bash
sibyl auth login [url] [options]
# Alias:
sibyl login [url] [options]
```

### Arguments

| Argument | Required | Description                                                |
| -------- | -------- | ---------------------------------------------------------- |
| `url`    | No       | Server URL. If omitted, uses the active context or default |

### Options

| Option                 | Short | Default | Description                                      |
| ---------------------- | ----- | ------- | ------------------------------------------------ |
| `--server`             | `-s`  | (none)  | Server base URL (alias for the positional URL)   |
| `--context`            | `-c`  | (none)  | Create or update a named context for this server |
| `--no-browser`         |       | false   | Print the URL instead of opening a browser       |
| `--timeout`            |       | 180     | Seconds to wait for approval/auth                |
| `--email`              | `-e`  | (none)  | Email for local login                            |
| `--password`           | `-p`  | (none)  | Password for local login                         |
| `--break-glass-reason` |       | (none)  | Incident reason for emergency local login        |
| `--insecure`           | `-k`  | false   | Disable SSL certificate verification             |

### Examples

```bash
# Log in to the active context or default server
sibyl login

# Log in to a specific server
sibyl login https://sibyl.example.com

# Log in and create a named context in one step
sibyl auth login https://prod.example.com -c prod

# Headless login (no browser)
sibyl auth login --no-browser

# Local email/password login
sibyl auth login -e stef@example.com -p "$SIBYL_PASSWORD"

# Emergency local login during an approved break-glass window
sibyl auth login \
  -e owner@example.com \
  -p "$SIBYL_BREAK_GLASS_PASSWORD" \
  --break-glass-reason "INC-123 IdP outage"
```

---

## auth status

Show auth status for the current context.

```bash
sibyl auth status
```

---

## auth local-signup

Create a local user and save the returned access token. Useful for local development servers.

### Synopsis

```bash
sibyl auth local-signup --email <email> --password <password> --name <name>
```

### Options

| Option       | Short | Required | Description            |
| ------------ | ----- | -------- | ---------------------- |
| `--email`    | `-e`  | Yes      | Email address          |
| `--password` | `-p`  | Yes      | Password (min 8 chars) |
| `--name`     | `-n`  | Yes      | Display name           |

### Example

```bash
sibyl auth local-signup \
  -e dev@localhost -p "devpassword" -n "Dev User"
```

---

## auth set-token

Set an auth token for a server directly. Defaults to the active context server.

### Synopsis

```bash
sibyl auth set-token <token> [options]
```

### Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `token`  | Yes      | Auth token  |

### Options

| Option     | Short | Description                                                  |
| ---------- | ----- | ------------------------------------------------------------ |
| `--server` | `-s`  | Server URL to set the token for (defaults to active context) |

---

## auth clear-token

Clear auth tokens for a server. Defaults to the active context server, or use `--all` to clear every
stored token.

### Synopsis

```bash
sibyl auth clear-token [options]
```

### Options

| Option     | Short | Description                                                 |
| ---------- | ----- | ----------------------------------------------------------- |
| `--server` | `-s`  | Server URL to clear tokens for (defaults to active context) |
| `--all`    | `-a`  | Clear tokens for ALL servers                                |

### Example

```bash
sibyl auth clear-token --all
```

---

## auth api-key

API key management. API keys authenticate MCP clients and scripts without a browser session. Keys
carry scopes and can be limited to specific projects and memory spaces.

| Subcommand                  | Description       |
| --------------------------- | ----------------- |
| `sibyl auth api-key list`   | List API keys     |
| `sibyl auth api-key create` | Create an API key |
| `sibyl auth api-key revoke` | Revoke an API key |

### auth api-key list

```bash
sibyl auth api-key list
```

### auth api-key create

```bash
sibyl auth api-key create --name <name> [options]
```

| Option              | Short | Default | Description                                          |
| ------------------- | ----- | ------- | ---------------------------------------------------- |
| `--name`            | `-n`  | (req.)  | Display name for the key (required)                  |
| `--live` / `--test` |       | `live`  | Use an `sk_live_` (default) or `sk_test_` key prefix |
| `--scopes`          |       | `mcp`   | Comma-separated scopes                               |
| `--projects`        |       | (none)  | Comma-separated graph project IDs the key may access |
| `--memory-spaces`   |       | (none)  | Comma-separated memory-space IDs the key may access  |
| `--expires-days`    |       | (none)  | Optional expiry in days (1-365)                      |

Available scopes include `mcp`, `api:read`, `api:write`, plus memory scopes. The full key value is
shown only once at creation.

#### Examples

```bash
# MCP key for a single project
sibyl auth api-key create --name "claude-mcp" --projects proj_abc123

# Read-only API key that expires in 90 days
sibyl auth api-key create --name "ci-readonly" \
  --scopes "api:read" --expires-days 90

# Test key scoped to a memory space
sibyl auth api-key create --name "agent-sandbox" --test \
  --memory-spaces space_main
```

### auth api-key revoke

```bash
sibyl auth api-key revoke <api_key_id>
```

| Argument     | Required | Description          |
| ------------ | -------- | -------------------- |
| `api_key_id` | Yes      | API key ID to revoke |

## Related Commands

- [`sibyl context`](./context.md) - Manage server/org/project contexts
- [`sibyl org`](./org.md) - Organization and member management
