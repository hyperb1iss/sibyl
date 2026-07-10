# recall

`recall` is a hidden compatibility alias for [`sibyl context`](./context.md). New instructions and
automation should use `context` directly.

```bash
sibyl context "wire up the password reset endpoint" --intent build
sibyl context "auth token refresh fails intermittently" --intent debug
sibyl context "plan the migration" --intent plan --budget 1200
```

## Compatibility

```bash
# Equivalent during the compatibility window
sibyl recall "wire up the password reset endpoint" --intent build
```

Both routes accept the same context layers, raw-memory filters, project scoping, diary identity,
JSON output, and token budget. The advertised intents are `build`, `plan`, `review`, `debug`, and
`general`.

## Related Commands

- [`sibyl context`](./context.md) - Load an agent-ready context pack
- [`sibyl remember`](./remember.md) - Store durable memory
- [`sibyl reflect`](./reflect.md) - Turn raw notes into memory candidates
- [`sibyl session`](./session.md) - Package a wake-up bundle
