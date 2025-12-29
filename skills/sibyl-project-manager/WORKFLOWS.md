# Project Manager Workflows

## Full Task Audit

### Step 1: Get the landscape

```bash
# All open work at once
sibyl task list --status todo,doing,blocked,review

# Or check specific statuses
sibyl task list --status doing,blocked  # Active + stuck
```

### Step 2: Review open tasks by priority

```bash
# High priority first
sibyl task list --status todo --priority critical,high

# Then medium/low
sibyl task list --status todo --priority medium,low
```

### Step 3: For each task, verify against code

Use Grep/Glob tools to check if the issue is resolved:

```bash
# Check if implementation exists
grep -r "function_name" apps/api/
grep -r "@router.post" apps/api/routes/
```

### Step 4: Archive completed tasks

```bash
sibyl task archive task_xxx --reason "Completed: [evidence with file:line]"
```

---

## Sprint Planning

### Step 1: Review high priority items

```bash
sibyl task list --status todo --priority critical,high
```

### Step 2: Check for blockers

```bash
sibyl task list --status blocked
```

### Step 3: Recommend sprint scope

Based on:
- 6-day cycle = ~4-6 meaningful tasks
- Mix of high impact + quick wins
- Dependencies resolved first

---

## Priority Rebalancing

### Step 1: Check current distribution

```bash
# High priority should be small
sibyl task list --status todo --priority critical,high

# Bulk of work should be here
sibyl task list --status todo --priority medium,low
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

## Quick Status Report

```bash
# Active work (doing + blocked + review)
sibyl task list --status doing,blocked,review

# Up Next (High Priority)
sibyl task list --status todo --priority critical,high
```

---

## Weekly Housekeeping

```bash
# Check stale work (doing/blocked should be small)
sibyl task list --status doing,blocked

# Clean up done tasks (archive with learnings)
sibyl task list --status done

# Priority sanity check (critical should be rare)
sibyl task list --status todo --priority critical
```
