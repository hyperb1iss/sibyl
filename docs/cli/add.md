# add

`add` is a hidden compatibility alias for [`sibyl remember`](./remember.md). New instructions and
automation should use `remember`, which preserves the raw source before graph projection.

```bash
sibyl remember "JWT refresh failure" \
  "Redis TTL expiry leaves a stale token key; regenerate on WRONGTYPE." \
  --kind error_pattern --domain auth
```

## Compatibility

The alias accepts the `remember` grammar:

```bash
sibyl add "JWT refresh failure" \
  "Redis TTL expiry leaves a stale token key; regenerate on WRONGTYPE." \
  --kind error_pattern
```

The hidden legacy `--type` spelling remains accepted for migration, but `--kind` is canonical. Old
graph-add flags such as `--title`, `--category`, `--language`, and `--skip-conflicts` are not part
of the agent contract.

Use the advertised kinds `episode`, `decision`, `procedure`, `error_pattern`, `rule`, `plan`,
`idea`, `claim`, `artifact`, `session`, and `note`. Run `sibyl remember --help` for compatibility
kinds and the complete option set.

## Related Commands

- [`sibyl remember`](./remember.md) - Store durable raw-first memory
- [`sibyl capture`](./capture.md) - Quick capture with an auto-derived title
- [`sibyl context`](./context.md) - Load existing project memory before writing
- [`sibyl correct`](./memory.md) - Inspect or correct source memory
