# Sibyl CLI Examples

Concrete examples showing the CLI in action.

> ⚠️ **Common Mistakes to Avoid:**
>
> - `sibyl task add` → Use `sibyl task create --title "..."`
> - `--json` flag → JSON is default (no flag needed)
> - `-t "Title"` → Use `--title "..."` (`-t` is for table output)
> - `jq '.[].title'` → Use `jq '.[].name'` (field is `name`)

---

## Search Examples

### Basic Search

```bash
sibyl search "authentication patterns"
```

### Search with Type Filter

```bash
sibyl search "error handling" --type pattern
sibyl search "debugging tips" --type episode
sibyl search "security rules" --type rule
```

### Search with Limit

```bash
sibyl search "OAuth" --limit 5
```

### Complex Search

```bash
# Find Python patterns about async
sibyl search "async await patterns" --type pattern

# Find debugging episodes
sibyl search "connection timeout" --type episode
```

---

## Task Examples

### List Tasks

```bash
# All tasks in a project
sibyl task list --project proj_auth

# Filter by status
sibyl task list --project proj_auth --status todo
sibyl task list --status doing
sibyl task list --status blocked

# Filter by assignee
sibyl task list --assignee alice

# JSON is the default output (no flag needed)
sibyl task list --project proj_auth
```

### Task Details

```bash
sibyl task show task_abc123
```

### Start Task

```bash
sibyl task start task_abc123 --assignee alice
# Output: Task started, branch: feature/task-abc123
```

### Block Task

```bash
sibyl task block task_abc123 --reason "Waiting for design approval"
```

### Unblock Task

```bash
sibyl task unblock task_abc123
```

### Submit for Review

```bash
sibyl task review task_abc123 --pr "https://github.com/org/repo/pull/42"

# With commits
sibyl task review task_abc123 \
  --pr "https://github.com/org/repo/pull/42" \
  --commits "abc1234,def5678,ghi9012"
```

### Complete Task

```bash
# Basic completion
sibyl task complete task_abc123

# With time tracking
sibyl task complete task_abc123 --hours 4.5

# With learnings (creates episode automatically)
sibyl task complete task_abc123 \
  --hours 4.5 \
  --learnings "OAuth tokens must be refreshed 5 minutes before expiry to avoid race conditions"
```

### Archive Task

```bash
sibyl task archive task_abc123 --yes
```

---

## Project Examples

### List Projects

```bash
sibyl project list
```

### Show Project

```bash
sibyl project show proj_auth
```

### Create Project

```bash
sibyl project create \
  --name "API Gateway" \
  --description "Rate limiting, auth, and routing layer"
```

---

## Entity Examples

### List Entities by Type

```bash
sibyl entity list --type pattern
sibyl entity list --type episode
sibyl entity list --type rule

# With filters
sibyl entity list --type pattern --language python
sibyl entity list --type rule --category security
```

### Show Entity

```bash
sibyl entity show pattern_abc123
```

### Create Entity (Capture Learning)

```bash
# Episode for a debugging insight
sibyl entity create \
  --type episode \
  --name "Redis connection pool exhaustion fix" \
  --content "When using Redis with high concurrency, pool size must match concurrent operations. Pool=10 with 20 concurrent requests causes exhaustion. Solution: pool >= max_concurrent * 1.5" \
  --category debugging \
  --languages python

# Pattern for a reusable approach
sibyl entity create \
  --type pattern \
  --name "Exponential backoff with jitter" \
  --content "delay = min(max_delay, base_delay * 2^attempt) + random(0, jitter)" \
  --category resilience \
  --languages python,typescript
```

### Related Entities

```bash
sibyl entity related pattern_abc123
```

### Delete Entity

```bash
sibyl entity delete entity_xyz --yes
```

---

## Exploration Examples

### Related Entities (1-hop)

```bash
sibyl explore related pattern_oauth
```

### Multi-hop Traversal

```bash
sibyl explore traverse proj_auth --depth 2
```

### Task Dependencies

```bash
# Single task
sibyl explore dependencies task_deploy

# Project-wide
sibyl explore dependencies --project proj_api
```

### Find Path

```bash
sibyl explore path pattern_auth task_login
```

---

## Output Formats

### JSON Output (Default)

```bash
# JSON is the default - no flag needed
sibyl task list
sibyl entity list --type pattern
sibyl project list
```

### Table Output (Human-Readable)

```bash
# Use -t for table format
sibyl task list -t
sibyl entity list --type pattern -t
```

### CSV Output

```bash
sibyl task list --csv > tasks.csv
sibyl entity list --type episode --csv > episodes.csv
```

---

## Complete Workflow Example

A full feature implementation from start to finish:

```bash
# 1. Research phase
sibyl search "user authentication" --type pattern
sibyl search "OAuth implementation" --type episode

# 2. Check existing projects
sibyl project list

# 3. Project exists, list its tasks
sibyl task list --project proj_auth --status todo

# 4. Start the task
sibyl task start task_oauth_google --assignee developer
# Output: Task started, branch: feature/oauth-google-login

# 5. Hit a blocker
sibyl task block task_oauth_google \
  --reason "Need to register app in Google Cloud Console"

# 6. Blocker resolved, resume
sibyl task unblock task_oauth_google

# 7. Implementation complete, submit PR
sibyl task review task_oauth_google \
  --pr "https://github.com/org/repo/pull/123"

# 8. PR approved, complete with learnings
sibyl task complete task_oauth_google \
  --hours 4.0 \
  --learnings "Google OAuth2 requires specific scopes: openid, email, profile. Token refresh should happen 5 min before expiry."

# 9. Check next task
sibyl task list --project proj_auth --status todo
```

---

## Admin Examples

### Health Check

```bash
sibyl health
```

### Statistics

```bash
sibyl stats
```

### Configuration

```bash
sibyl config
```

### Setup (First Time)

```bash
sibyl setup
```
