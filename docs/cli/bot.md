# bot

Bot accounts: agent identities you sponsor. A bot is a member of your organization that signs in
only with API keys and never holds more on a project than you do. See
[Bot Accounts](/users/bot-accounts) for the rule and who can do what.

Every command that takes `<bot>` accepts a bot ID or the exact name of a bot you can see.

## Commands

- `sibyl bot create` - Create a bot you sponsor
- `sibyl bot list` - List bots (every bot for an org admin, your own otherwise)
- `sibyl bot show` - Show a bot, its sponsor and its project grants
- `sibyl bot grant` - Grant your bot a project role
- `sibyl bot revoke` - Remove a bot's grant on a project
- `sibyl bot transfer` - Give a bot a new sponsor (org admins)
- `sibyl bot delete` - Delete a bot and revoke its keys
- `sibyl bot key create` - Issue a key that authenticates as the bot
- `sibyl bot key list` - List a bot's keys
- `sibyl bot key revoke` - Revoke one of a bot's keys

---

## bot create

Create a bot you sponsor. Owners, admins and members can; viewers cannot.

```bash
sibyl bot create <name> [--json]
```

| Argument | Required | Description             |
| -------- | -------- | ----------------------- |
| `name`   | Yes      | Bot name, e.g. `oncall` |

---

## bot list

List bots. An org admin sees every bot in the organization; anyone else sees the bots they sponsor.
A bot whose sponsor left shows as `suspended`.

```bash
sibyl bot list [--json]
```

---

## bot show

Show a bot, its sponsor, its status and its project grants.

```bash
sibyl bot show <bot> [--json]
```

---

## bot grant

Grant your bot a role on a project, up to min(your own role on that project, contributor). Only the
bot's sponsor grants it access.

```bash
sibyl bot grant <bot> <project> [--role contributor|viewer] [--json]
```

| Option   | Short | Description                         |
| -------- | ----- | ----------------------------------- |
| `--role` | `-r`  | `contributor` (default) or `viewer` |
| `--json` | `-j`  | JSON output                         |

The role you grant is a ceiling, not a guarantee: on every request the bot holds the lower of its
grant and your current role on the project.

---

## bot revoke

Remove a bot's grant on a project. The sponsor or an org admin can.

```bash
sibyl bot revoke <bot> <project> [--json]
```

---

## bot transfer

Give a bot a new sponsor. Org admins only. The new sponsor must be a person who is an owner, admin
or member of the organization. Use this to revive a bot whose sponsor left.

```bash
sibyl bot transfer <bot> --to <user-id> [--json]
```

---

## bot delete

Delete a bot. Every key it holds stops working at once, and its private memories are scheduled for
purge after 30 days.

```bash
sibyl bot delete <bot> [--yes] [--json]
```

| Option  | Short | Description     |
| ------- | ----- | --------------- |
| `--yes` | `-y`  | Don't ask first |

---

## bot key create

Issue a key that authenticates as the bot. The sponsor or an org admin can. The secret prints once,
as JSON.

```bash
sibyl bot key create <bot> --name <name> [options]
```

| Option            | Description                                              |
| ----------------- | -------------------------------------------------------- |
| `--name`, `-n`    | Display name for the key (required)                      |
| `--scopes`        | Comma-separated scopes; default `mcp,api:read,api:write` |
| `--projects`      | Comma-separated project IDs to narrow the key to         |
| `--memory-spaces` | Comma-separated memory-space IDs to narrow the key to    |
| `--expires-days`  | Expire the key after this many days (1-365)              |
| `--live/--test`   | `sk_live_` (default) or `sk_test_` prefix                |

```bash
sibyl bot key create oncall --name pager --scopes mcp,api:read,api:write --expires-days 90
```

---

## bot key list

```bash
sibyl bot key list <bot> [--json]
```

---

## bot key revoke

```bash
sibyl bot key revoke <bot> <key-id>
```
