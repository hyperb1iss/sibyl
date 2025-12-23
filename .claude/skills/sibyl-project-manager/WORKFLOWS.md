# Project Manager Workflows

## Full Task Audit

When asked to audit tasks:

### Step 1: Get the landscape

```bash
# Count by status
echo "=== Status Counts ==="
for s in todo doing blocked review done; do
  echo "$s: $(sibyl task list --status $s 2>&1 | jq 'length')"
done
```

### Step 2: List all open non-auth tasks

```bash
sibyl task list --status todo 2>&1 | python3 -c "
import json, sys
data = json.load(sys.stdin)
for task in sorted(data, key=lambda t: t.get('metadata', {}).get('priority', 'z')):
    meta = task.get('metadata', {})
    if meta.get('feature') == 'auth':
        continue
    p = meta.get('priority', '-')
    print(f'{p:8} | {task[\"id\"][-12:]} | {task[\"name\"][:55]}')"
```

### Step 3: For each task, verify against code

Use grep/glob to check if the issue is resolved:

```bash
# Example: Check if a function exists
grep -r "function_name" src/sibyl/

# Example: Check if a route exists
grep -r "@router.post" src/sibyl/api/routes/

# Example: Check if component exists
ls web/src/components/
```

### Step 4: Archive completed tasks

```bash
sibyl task archive task_xxx --reason "Completed: [evidence with file:line]"
```

---

## Sprint Planning

When planning a sprint:

### Step 1: Review high priority items

```bash
sibyl task list --status todo 2>&1 | jq -r '.[] | select(.metadata.priority == "critical" or .metadata.priority == "high") | "[\(.metadata.priority)] \(.id[-12:]): \(.name)"'
```

### Step 2: Check for blockers

```bash
sibyl task list --status blocked 2>&1 | jq -r '.[] | "\(.id[-12:]): \(.name) - \(.metadata.blocked_reason // "no reason")"'
```

### Step 3: Identify dependencies

Look for tasks that reference each other or share feature tags.

### Step 4: Recommend sprint scope

Based on:
- 6-day cycle = ~4-6 meaningful tasks
- Mix of high impact + quick wins
- Dependencies resolved first

---

## Priority Rebalancing

When priorities seem off:

### Step 1: Get current priority distribution

```bash
sibyl task list --status todo 2>&1 | jq -r 'group_by(.metadata.priority) | .[] | "\(.[0].metadata.priority): \(length)"'
```

### Step 2: Identify misclassified tasks

**Too High:**
- Polish/optimization marked critical
- Nice-to-haves marked high

**Too Low:**
- Bugs marked low
- Blocking issues marked medium

### Step 3: Update priorities

```bash
sibyl task update task_xxx --priority medium
```

---

## Data Cleanup

When data quality issues arise:

### Step 1: Find suspicious entries

```bash
# Tasks with test-like names
sibyl task list 2>&1 | jq -r '.[] | select(.name | test("^(Batch|Test|Perf|Sample)")) | "\(.id)\t\(.name)"'
```

### Step 2: Find duplicates

```bash
sibyl task list 2>&1 | jq -r '.[].name' | sort | uniq -d
```

### Step 3: Find orphaned tasks

```bash
sibyl task list 2>&1 | jq -r '.[] | select(.metadata.project_id == null) | "\(.id)\t\(.name)"'
```

### Step 4: Archive garbage (verify first!)

```bash
sibyl task archive task_xxx --reason "Cleanup: test data"
```

---

## Quick Status Report

Generate a quick status for standup:

```bash
echo "=== PROJECT STATUS ==="
echo ""
echo "In Progress:"
sibyl task list --status doing 2>&1 | jq -r '.[] | "  - \(.name)"'
echo ""
echo "Blocked:"
sibyl task list --status blocked 2>&1 | jq -r '.[] | "  - \(.name): \(.metadata.blocked_reason // "?")"'
echo ""
echo "Ready for Review:"
sibyl task list --status review 2>&1 | jq -r '.[] | "  - \(.name)"'
echo ""
echo "Up Next (High Priority):"
sibyl task list --status todo 2>&1 | jq -r '.[] | select(.metadata.priority == "high" or .metadata.priority == "critical") | "  - [\(.metadata.priority)] \(.name)"'
```

---

## Task Verification Checklist

When verifying a specific task is complete:

### Backend Tasks
```bash
# Check implementation
grep -r "function_or_class_name" src/sibyl/

# Check tests
grep -r "test_function" tests/

# Check route (if API)
grep -r "@router" src/sibyl/api/routes/ | grep "endpoint_name"
```

### Frontend Tasks
```bash
# Check component
ls web/src/components/ | grep -i "component_name"

# Check page
ls web/src/app/

# Check hooks
grep "use.*Name" web/src/lib/hooks.ts
```

### CLI Tasks
```bash
# Check command
grep "@app.command" src/sibyl/cli/ | grep "command_name"
```

---

## Batch Operations

### Archive multiple tasks

```bash
# After verification, archive in sequence
sibyl task archive task_aaa --reason "Done: X" 2>&1
sibyl task archive task_bbb --reason "Done: Y" 2>&1
sibyl task archive task_ccc --reason "Irrelevant: Z" 2>&1
```

### Update multiple priorities

```bash
sibyl task update task_aaa --priority high 2>&1
sibyl task update task_bbb --priority medium 2>&1
```

---

## Weekly Housekeeping

Run weekly:

1. **Check for stale "doing" tasks** (stuck in progress)
```bash
sibyl task list --status doing 2>&1 | jq 'length'
```

2. **Review blocked tasks** (may need escalation)
```bash
sibyl task list --status blocked 2>&1 | jq -r '.[] | .name'
```

3. **Clean up completed work** (done but not archived)
```bash
sibyl task list --status done 2>&1 | jq -r '.[] | "\(.id[-12:])\t\(.name)"'
```

4. **Priority sanity check**
```bash
sibyl task list --status todo 2>&1 | jq -r 'group_by(.metadata.priority) | .[] | "\(.[0].metadata.priority): \(length)"'
```
