# Archive

Browse the raw archive created by quick memory capture. This is the write-once sidecar that keeps
the original payload before graph extraction reshapes it.

## Usage

```bash
sibyl archive list
sibyl archive list --surface dashboard
sibyl archive show <capture_id>
```

## Commands

### `sibyl archive list`

List archived raw captures for the current organization.

```bash
sibyl archive list --limit 20
sibyl archive list --type pattern
sibyl archive list --surface cli
```

### `sibyl archive show`

Show one archived raw capture with verbatim content.

```bash
sibyl archive show 7f7b4b91-1cd8-47c5-a03e-59e2df96e6d0
```

## Notes

- The archive is populated by quick capture flows that send `metadata.capture_mode=quick`.
- `list` shows summaries; `show` returns the full raw content.

## Related Commands

- [`sibyl capture`](./capture.md) - Quick capture that feeds the archive
- [`sibyl remember`](./remember.md) - Typed memory-loop writes
- [Memory governance](./memory.md) - Inspect and audit raw memory sources
