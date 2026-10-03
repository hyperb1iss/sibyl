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
| `--limit`               | none                      | Replay at most N raw memories                                   |

### Before you start

```bash
# 1. Sign in to the team server; your login joins its team org
sibyl auth login https://sibyl.example.com --context team

# 2. Create the project on the team server if it is not there yet
sibyl -C team project create --name "Backend API"
```

The command refuses to migrate into a personal org, because the team could not see the result. If
your login landed in a personal org, switch first with `sibyl -C team org switch <team-slug>`.

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
them visible to everyone in the project instead.

### Re-running

Ledgers under `~/.sibyl/migrations` map every migrated row to its id on the target, and every write
carries an idempotency key. Re-running after an interruption resumes where it stopped and never
duplicates what already landed, so it is safe to run the command again at any time.

### What changes on the way

- Ids can differ on the target. Each migrated row keeps its original id, timestamps, and scope under
  `metadata.migration`, along with the original title when a repeated title had to be numbered.
- Creation time on the target is the migration time; the original sits in
  `metadata.migration.origin_created_at`.
- The API declares links at creation and accepts `supersedes`, `contradicts`, `requires`,
  `supports`, and `decides` as typed links. Other link types (such as `DERIVED_FROM` or
  `USES_PROCEDURE`) arrive as untyped links, and the original type is recorded under
  `metadata.migration.coerced_edges`.

### Server version

Team servers before 1.4.4 let one member's write replace another member's memory when both used the
same title. Upgrade the team server to 1.4.4 or newer before several people migrate into it.
