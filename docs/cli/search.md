# search

`search` is a hidden compatibility alias for [`sibyl context`](./context.md). It now returns the
same agent-ready context pack and accepts the same goal-oriented options.

New instructions and automation should call `context` directly:

```bash
sibyl context "implement authentication" --intent build
sibyl context "debug intermittent token refresh failures" --intent debug --limit 8
sibyl context "review deployment guidance" --intent review --all
```

## Compatibility

```bash
# Equivalent during the compatibility window
sibyl search "implement authentication" --intent build
```

The old graph-search grammar is not accepted by `search`. Flags such as `--type`, `--graph-only`,
and `--docs-only` belonged to the pre-convergence command. Existing operator automation can use the
hidden `sibyl graph-search` escape while migrating, but agent prompts should use `context`.

Use `sibyl show <id>` when a returned context preview is not enough. Record material usage with
`sibyl cite <id...>` or `sibyl cite <id...> --misled`.

## Related Commands

- [`sibyl context`](./context.md) - Load an agent-ready context pack
- [`sibyl show`](./show.md) - Retrieve full graph or raw-memory content by ID
- [`sibyl remember`](./remember.md) - Store durable raw-first memory
- [`sibyl correct`](./memory.md) - Inspect or correct source memory
