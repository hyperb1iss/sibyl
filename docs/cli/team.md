# team

Team memory management. Teams group users inside an organization, carry their own team memory space,
and can be granted access to projects as a unit.

## Commands

- `sibyl team list` - List teams in the active organization
- `sibyl team create` - Create a team and its team memory space
- `sibyl team add-member` - Add or update a team member
- `sibyl team remove-member` - Remove a team member
- `sibyl team link-project` - Grant a team access to a project
- `sibyl team unlink-project` - Remove a team's project access

---

## team list

List teams in the active organization.

### Synopsis

```bash
sibyl team list [options]
```

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl team list
```

---

## team create

Create a team and its team memory space.

### Synopsis

```bash
sibyl team create <name> [options]
```

### Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `name`   | Yes      | Team name   |

### Options

| Option          | Short | Description      |
| --------------- | ----- | ---------------- |
| `--slug`        |       | Stable team slug |
| `--description` | `-d`  | Team description |
| `--json`        | `-j`  | JSON output      |

### Examples

```bash
# Create a team
sibyl team create "Platform"

# Create with a stable slug and description
sibyl team create "Platform" --slug platform \
  --description "Owns infra, CI, and shared tooling"
```

---

## team add-member

Add or update a team member.

### Synopsis

```bash
sibyl team add-member <team_id> <user_id> [options]
```

### Arguments

| Argument  | Required | Description     |
| --------- | -------- | --------------- |
| `team_id` | Yes      | Team ID or slug |
| `user_id` | Yes      | User UUID       |

### Options

| Option   | Short | Default  | Description |
| -------- | ----- | -------- | ----------- |
| `--role` | `-r`  | `member` | Team role   |
| `--json` | `-j`  | false    | JSON output |

### Example

```bash
sibyl team add-member platform 1f08c55c-b67a-475b-b52b-922c675ff748
```

Running `add-member` again for the same user updates their team role.

---

## team remove-member

Remove a team member.

### Synopsis

```bash
sibyl team remove-member <team_id> <user_id> [options]
```

### Arguments

| Argument  | Required | Description     |
| --------- | -------- | --------------- |
| `team_id` | Yes      | Team ID or slug |
| `user_id` | Yes      | User UUID       |

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl team remove-member platform 1f08c55c-b67a-475b-b52b-922c675ff748
```

---

## team link-project

Grant a team access to a project.

### Synopsis

```bash
sibyl team link-project <team_id> <project_id> [options]
```

### Arguments

| Argument     | Required | Description                      |
| ------------ | -------- | -------------------------------- |
| `team_id`    | Yes      | Team ID or slug                  |
| `project_id` | Yes      | Project UUID or graph project ID |

### Options

| Option   | Short | Default               | Description                      |
| -------- | ----- | --------------------- | -------------------------------- |
| `--role` | `-r`  | `project_contributor` | Project role granted to the team |
| `--json` | `-j`  | false                 | JSON output                      |

### Example

```bash
sibyl team link-project platform proj_abc123
```

---

## team unlink-project

Remove a team's project access.

### Synopsis

```bash
sibyl team unlink-project <team_id> <project_id> [options]
```

### Arguments

| Argument     | Required | Description                      |
| ------------ | -------- | -------------------------------- |
| `team_id`    | Yes      | Team ID or slug                  |
| `project_id` | Yes      | Project UUID or graph project ID |

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

### Example

```bash
sibyl team unlink-project platform proj_abc123
```

---

## Related Commands

- [`sibyl org`](./org.md) - Organizations and member management
- [`sibyl project`](./project.md) - Project management and directory linking
- [Memory governance](./memory.md) - Memory spaces, sharing, and audit
