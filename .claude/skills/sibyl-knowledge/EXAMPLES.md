# Sibyl CLI Examples

Concrete examples showing the CLI in action.

---

## Search Examples

### Basic Search
```bash
uv run sibyl search "authentication patterns"
```

### Search with Type Filter
```bash
uv run sibyl search "error handling" --type pattern
uv run sibyl search "debugging tips" --type episode
uv run sibyl search "security rules" --type rule
```

### Search with Limit
```bash
uv run sibyl search "OAuth" --limit 5
```

### Complex Search
```bash
# Find Python patterns about async
uv run sibyl search "async await patterns" --type pattern

# Find debugging episodes
uv run sibyl search "connection timeout" --type episode
```

---

## Task Examples

### List Tasks
```bash
# All tasks in a project
uv run sibyl task list --project proj_auth

# Filter by status
uv run sibyl task list --project proj_auth --status todo
uv run sibyl task list --status doing
uv run sibyl task list --status blocked

# Filter by assignee
uv run sibyl task list --assignee alice

# JSON output
uv run sibyl task list --project proj_auth --format json
```

### Task Details
```bash
uv run sibyl task show task_abc123
```

### Start Task
```bash
uv run sibyl task start task_abc123 --assignee alice
# Output: Task started, branch: feature/task-abc123
```

### Block Task
```bash
uv run sibyl task block task_abc123 --reason "Waiting for design approval"
```

### Unblock Task
```bash
uv run sibyl task unblock task_abc123
```

### Submit for Review
```bash
uv run sibyl task review task_abc123 --pr "https://github.com/org/repo/pull/42"

# With commits
uv run sibyl task review task_abc123 \
  --pr "https://github.com/org/repo/pull/42" \
  --commits "abc1234,def5678,ghi9012"
```

### Complete Task
```bash
# Basic completion
uv run sibyl task complete task_abc123

# With time tracking
uv run sibyl task complete task_abc123 --hours 4.5

# With learnings (creates episode automatically)
uv run sibyl task complete task_abc123 \
  --hours 4.5 \
  --learnings "OAuth tokens must be refreshed 5 minutes before expiry to avoid race conditions"
```

### Archive Task
```bash
uv run sibyl task archive task_abc123 --yes
```

---

## Project Examples

### List Projects
```bash
uv run sibyl project list
```

### Show Project
```bash
uv run sibyl project show proj_auth
```

### Create Project
```bash
uv run sibyl project create \
  --name "API Gateway" \
  --description "Rate limiting, auth, and routing layer"
```

---

## Entity Examples

### List Entities by Type
```bash
uv run sibyl entity list --type pattern
uv run sibyl entity list --type episode
uv run sibyl entity list --type rule

# With filters
uv run sibyl entity list --type pattern --language python
uv run sibyl entity list --type rule --category security
```

### Show Entity
```bash
uv run sibyl entity show pattern_abc123
```

### Create Entity (Capture Learning)
```bash
# Episode for a debugging insight
uv run sibyl entity create \
  --type episode \
  --name "Redis connection pool exhaustion fix" \
  --content "When using Redis with high concurrency, pool size must match concurrent operations. Pool=10 with 20 concurrent requests causes exhaustion. Solution: pool >= max_concurrent * 1.5" \
  --category debugging \
  --languages python

# Pattern for a reusable approach
uv run sibyl entity create \
  --type pattern \
  --name "Exponential backoff with jitter" \
  --content "delay = min(max_delay, base_delay * 2^attempt) + random(0, jitter)" \
  --category resilience \
  --languages python,typescript
```

### Related Entities
```bash
uv run sibyl entity related pattern_abc123
```

### Delete Entity
```bash
uv run sibyl entity delete entity_xyz --yes
```

---

## Exploration Examples

### Related Entities (1-hop)
```bash
uv run sibyl explore related pattern_oauth
```

### Multi-hop Traversal
```bash
uv run sibyl explore traverse proj_auth --depth 2
```

### Task Dependencies
```bash
# Single task
uv run sibyl explore dependencies task_deploy

# Project-wide
uv run sibyl explore dependencies --project proj_api
```

### Find Path
```bash
uv run sibyl explore path pattern_auth task_login
```

---

## Output Formats

### JSON Output
```bash
uv run sibyl task list --format json
uv run sibyl entity list --type pattern --format json
uv run sibyl project list --format json
```

### CSV Output
```bash
uv run sibyl task list --format csv > tasks.csv
uv run sibyl entity list --type episode --format csv > episodes.csv
```

---

## Complete Workflow Example

A full feature implementation from start to finish:

```bash
# 1. Research phase
uv run sibyl search "user authentication" --type pattern
uv run sibyl search "OAuth implementation" --type episode

# 2. Check existing projects
uv run sibyl project list

# 3. Project exists, list its tasks
uv run sibyl task list --project proj_auth --status todo

# 4. Start the task
uv run sibyl task start task_oauth_google --assignee developer
# Output: Task started, branch: feature/oauth-google-login

# 5. Hit a blocker
uv run sibyl task block task_oauth_google \
  --reason "Need to register app in Google Cloud Console"

# 6. Blocker resolved, resume
uv run sibyl task unblock task_oauth_google

# 7. Implementation complete, submit PR
uv run sibyl task review task_oauth_google \
  --pr "https://github.com/org/repo/pull/123"

# 8. PR approved, complete with learnings
uv run sibyl task complete task_oauth_google \
  --hours 4.0 \
  --learnings "Google OAuth2 requires specific scopes: openid, email, profile. Token refresh should happen 5 min before expiry."

# 9. Check next task
uv run sibyl task list --project proj_auth --status todo
```

---

## Admin Examples

### Health Check
```bash
uv run sibyl health
```

### Statistics
```bash
uv run sibyl stats
```

### Configuration
```bash
uv run sibyl config
```

### Setup (First Time)
```bash
uv run sibyl setup
```
