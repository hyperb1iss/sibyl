# org

Organizations. Each Sibyl organization is an isolated tenant with its own SurrealDB namespace
(`org_<uuid_hex>`). `org` lists organizations, creates them, switches the active org, and manages
members.

## Commands

| Command                             | Description                    |
| ----------------------------------- | ------------------------------ |
| [`sibyl org list`](#org-list)       | List organizations             |
| [`sibyl org create`](#org-create)   | Create an organization         |
| [`sibyl org switch`](#org-switch)   | Switch the active organization |
| [`sibyl org members`](#org-members) | Manage organization members    |

## Org Isolation

Every graph and memory operation is scoped to an organization. Switching orgs changes the namespace
the CLI reads and writes. Membership roles (`owner`, `admin`, `member`, `viewer`) govern what a user
can do inside an org.

---

## org list

List the organizations you belong to.

```bash
sibyl org list
```

---

## org create

Create a new organization. By default the CLI switches into the new org after creating it.

### Synopsis

```bash
sibyl org create --name <name> [options]
```

### Options

| Option     | Short | Default   | Description                                           |
| ---------- | ----- | --------- | ----------------------------------------------------- |
| `--name`   | `-n`  | (req.)    | Organization name (required)                          |
| `--slug`   |       | (derived) | Optional URL slug                                     |
| `--switch` |       | on        | Switch into the org after creating it (`--no-switch`) |

### Example

```bash
sibyl org create --name "Acme Engineering" --slug acme-eng
```

---

## org switch

Switch the active organization by slug.

### Synopsis

```bash
sibyl org switch <slug>
```

### Arguments

| Argument | Required | Description       |
| -------- | -------- | ----------------- |
| `slug`   | Yes      | Organization slug |

### Example

```bash
sibyl org switch acme-eng
```

---

## org members

Manage organization members.

| Subcommand                 | Description                 |
| -------------------------- | --------------------------- |
| `sibyl org members list`   | List all members of an org  |
| `sibyl org members add`    | Add a member to an org      |
| `sibyl org members remove` | Remove a member from an org |
| `sibyl org members role`   | Update a member's role      |

Roles: `owner`, `admin`, `member`, `viewer`.

### org members list

```bash
sibyl org members list <slug> [--json]
```

| Argument | Required | Description       |
| -------- | -------- | ----------------- |
| `slug`   | Yes      | Organization slug |

### org members add

```bash
sibyl org members add <slug> <user_id> [options]
```

| Argument  | Required | Description       |
| --------- | -------- | ----------------- |
| `slug`    | Yes      | Organization slug |
| `user_id` | Yes      | User ID to add    |

| Option   | Short | Default  | Description    |
| -------- | ----- | -------- | -------------- |
| `--role` | `-r`  | `member` | Role to assign |

### org members remove

```bash
sibyl org members remove <slug> <user_id> [options]
```

| Argument  | Required | Description       |
| --------- | -------- | ----------------- |
| `slug`    | Yes      | Organization slug |
| `user_id` | Yes      | User ID to remove |

| Option    | Short | Description       |
| --------- | ----- | ----------------- |
| `--force` | `-f`  | Skip confirmation |

### org members role

```bash
sibyl org members role <slug> <user_id> <role>
```

| Argument  | Required | Description                                    |
| --------- | -------- | ---------------------------------------------- |
| `slug`    | Yes      | Organization slug                              |
| `user_id` | Yes      | User ID                                        |
| `role`    | Yes      | New role: `owner`, `admin`, `member`, `viewer` |

### Examples

```bash
# List members
sibyl org members list acme-eng

# Add a member as an admin
sibyl org members add acme-eng user_abc123 --role admin

# Promote a member to admin
sibyl org members role acme-eng user_abc123 admin

# Remove a member without a prompt
sibyl org members remove acme-eng user_abc123 --force
```

## Related Commands

- [`sibyl auth`](./auth.md) - Authentication and API keys
- [`sibyl config context`](./context.md) - Bundle server, org, and project settings
