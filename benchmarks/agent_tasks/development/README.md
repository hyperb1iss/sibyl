# Development coding tasks

Eight small Python repositories exercise four pairs of related contracts.
Each repository contains a failing implementation, a written behavior contract,
and public tests that run with Python's standard library. The runner's separate
checker also tests cases that a partial fix can miss.

| Family | First contract | Second contract |
| --- | --- | --- |
| Configuration presence | Preserve explicit falsey overrides | Treat a null patch value as deletion |
| Pagination | Preserve zero and empty-string cursors | Advance offsets through filtered pages |
| Time boundaries | Compare half-open windows as absolute instants | Include both entitlement dates |
| Event ordering | Select the greatest version per key | Apply every delta in arrival order |

The paired contracts require different fixes. For example, choosing the newest
state event is correct for versioned snapshots but drops required increments
from an ordered delta stream.

The task definitions in `tasks.json` use the existing runner's `Task` schema.
Paths and SHA-256 digests are relative to this directory. Copy the declared
artifacts into a frozen experiment root and include these task definitions in
its manifest. Use the [coding controller](../README.md) with explicit model,
tool, token and cost budgets. Only each task's `workspace` entries belong in the
candidate repository; the checker and validation reference edits remain outside.

Run the corpus checks from the repository root:

```sh
moon run root:agent-task-test -- -k development_corpus
```

The checks run each initial repository through the existing runner, validate a
complete repair, then confirm that a plausible partial repair passes public
tests but fails the separate checker. Reference edits live only in the test
harness and are applied to disposable validation copies. The harness uses a
synthetic controller and makes no model calls.

These tasks are exposed development material. They are not held-out evaluation
or learning experiences. The checker executes candidate Python under the same
user and does not provide sealed isolation. Passing corpus validation establishes
that the tasks and checks discriminate these repairs; it establishes no model
score or learning benefit. Freeze separate learning and held-out task families
before making a consolidation comparison.
