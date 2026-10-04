# migrate

Move a project from your personal Sibyl into a shared team server, as yourself.

## Commands

- `sibyl migrate to-team` - Migrate one project's memories and graph into a team server

---

## migrate to-team

Reads one project from your local Sibyl and writes it into a team server through the ordinary
authenticated API, so everything lands owned by your account there. No cluster or server access is
involved: if you can sign in to the team server, you can migrate.

The command runs two passes:

1. **Raw memories.** The project's verbatim captures are replayed, with provenance recording each
   original id and timestamp.
2. **Graph.** The project's authored entities are created with the links between them: tasks with
   their status, priority, epic, parent, dependencies, and learnings; epics and milestones;
   decisions, error patterns, procedures, notes, episodes, plans, patterns, rules, and the rest.

What the target derives on its own is left behind and rebuilt there: topics and their mention links
from the memory projection, passages from long memories, and projected facts.

### Synopsis

```bash
sibyl migrate to-team --target-context <context> --project <project_id> [options]
```

### Options

| Option                  | Default                   | Description                                                     |
| ----------------------- | ------------------------- | --------------------------------------------------------------- |
| `--target-context`      | (required)                | Named context for the team server                               |
| `--project`             | (required)                | Source project id (`project_...`) to migrate                    |
| `--target-project`      | same id, then same name   | Target project id or name                                       |
| `--source-org`          | found automatically       | Source organization UUID, when several orgs hold the project    |
| `--dry-run`             | off                       | Print what would move, write nothing                            |
| `--graph / --no-graph`  | `--graph`                 | Include the graph pass                                          |
| `--share-private`       | off                       | Make your private memories visible to the project on the target |
| `--allow-personal-org`  | off                       | Migrate into your personal org instead of a team org            |
| `--source-surreal-url`  | `ws://localhost:8000/rpc` | Local SurrealDB endpoint                                        |
| `--source-surreal-user` | URL userinfo, else `root` | Local SurrealDB username                                        |
| `--source-surreal-pass` | URL userinfo, else `root` | Local SurrealDB password (prefer `SIBYL_SOURCE_SURREAL_PASS`)   |
| `--limit`               | none                      | Migrate at most N raw memories and N graph entities             |

### Before you start

```bash
# 1. Sign in to the team server; your login joins its team org
sibyl auth login https://sibyl.example.com --context team

# 2. Create the project on the team server if it is not there yet
sibyl -C team project create --name "Backend API"
```

The command refuses a personal org by default because the team could not see the result. Switch
with `sibyl -C team org switch <team-slug>`, or use `--allow-personal-org` when that personal org is
your intended destination.

### Example

```bash
# See what would move
sibyl migrate to-team --target-context team --project project_abc123 --dry-run

# Migrate
sibyl migrate to-team --target-context team --project project_abc123
```

A dry run prints the plan: entity counts by type and scope, the links it will declare, and what the
target will re-derive instead.

### Privacy

Memories that are private to you locally stay private to you on the team server: teammates cannot
read them, but your own recall and context packs there include them. Pass `--share-private` to make
them visible to everyone in the project instead. Rows flagged as holding a credential or token stay
private either way, and rows taken out of recall on your instance (contested or retired) are not
migrated.

### Re-running

A ledger under `~/.sibyl/migrations` binds progress to the source server and project, target server,
organization, and signed-in account. Running the command again skips completed rows and finishes
the rest. The ledger stores pending request bodies with private file permissions. Keep the ledger
until migration is complete.

Task status changes use the revision returned by the original creation transaction. If someone
edits the task before its status is set, the status change stops instead of replacing that edit.

Stable operation keys let the server replay a completed receipt when a response was lost. If the
server cannot confirm whether a write completed, migration stops with
`idempotency_reconciliation_required`. Preserve the ledger and ask your server administrator to
check the operation before continuing; repeated runs do not overwrite the row to resolve the doubt.

When a linked row failed to migrate, the next run can add the missing link after that row lands.
Link repair changes only relationships and task topology. The server checks the saved revision in
the same transaction, so a concurrent edit stops a new link write. If the link write completed but
its response was lost, the server can acknowledge the existing links without changing the row.

Older raw ledgers are adopted only after checking each receipt against the destination account,
organization, and project. An older graph ledger without server and account identity stops the run
for target-row verification; deleting that ledger can cause existing rows to be written again.

Re-running does not carry edits you made locally after a row was migrated. Once a project has moved,
work on it in the team server.

`--limit N` migrates at most N new raw memories and N new graph entities. Repeat the command to
advance through another batch; completed rows remain available for unfinished status and link work.

### Migrating as a team

Several people can migrate into the same team project. Each person's rows land under their own
account. The server protects same-titled memories with a recorded owner, including legacy private
ownership, and rejects a write when its qualified ID is occupied by another owner. Older rows with
no recorded owner can still be updated within the same project. When a teammate already created an
epic or milestone with the same name in the project, your tasks link to theirs instead of creating
a second one.

If you already wrote memories with the same titles in that project on the team server, the migration
updates those rows rather than adding copies.

### What changes on the way

- Ids can differ on the target. Each migrated row keeps its original id, timestamps, and scope under
  `metadata.migration`, along with the original title when a repeated title had to be numbered.
- Creation time on the target is the migration time; the original sits in
  `metadata.migration.origin_created_at`. A task's original start and completion times sit there
  too.
- An epic's status derives from its tasks, so its original status is kept only under
  `metadata.migration.origin_status`.
- The API declares links at creation and accepts `supersedes`, `contradicts`, `requires`,
  `supports`, and `decides` as typed links. Other link types (such as `DERIVED_FROM` or
  `USES_PROCEDURE`) arrive as untyped links, and the original type is recorded under
  `metadata.migration.coerced_edges`.

### Server compatibility

Upgrade the team server before migrating. The command checks the server's authenticated migration
capabilities before writing and refuses a server without protected retry support. Graph migration
also requires the server to advertise atomic ownership checks and additive link writes. A raw-only
run (`--no-graph`) needs only protected retry support. Link repair never falls back to rewriting
an entire entity.

Older servers can replace a teammate's same-titled memory or move a row out of another project.
Migration writes record their author and atomically check existing ownership and project before
writing. A concurrent first writer cannot replace the winning author's row. A second author's project, epic, or milestone with the same name in the same
project is refused. Older rows without a recorded author are protected across projects but can
still be updated within one project.
