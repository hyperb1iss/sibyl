# service

Install local daemon service files. `service` writes a native user-service definition that keeps the
embedded `sibyld` daemon running for your active local context: a launchd agent on macOS, a systemd
user unit on Linux. It writes the file but does not start it, so you stay in control of when the
daemon comes up.

This is for running Sibyl directly on the host. For a containerized deployment, use
[`sibyl docker`](./docker.md) or [`sibyl local`](./local.md).

## Running the Daemon

Three top-level commands run the embedded `sibyld` daemon natively, without Docker:

| Command       | What it does                                      |
| ------------- | ------------------------------------------------- |
| `sibyl serve` | Run the daemon in the foreground (Ctrl+C to stop) |
| `sibyl start` | Start the daemon in the background                |
| `sibyl stop`  | Stop the background daemon                        |

```bash
# First-run local setup, then run in the foreground
sibyl init --local
sibyl serve

# Or run it in the background and check health
sibyl start
sibyl doctor
sibyl stop
```

For a daemon that survives reboots, install a user-service file with
[`sibyl service install`](#service-install) below.

## Commands

| Command                                     | Description                                                   |
| ------------------------------------------- | ------------------------------------------------------------- |
| [`sibyl service install`](#service-install) | Write a native user-service file for the active local context |
| [`sibyl service path`](#service-path)       | Print the native service file path for this platform          |

---

## service install

Write a native user-service file for the active local context. The command prints the start command
for your platform but does not run it.

```bash
sibyl service install [options]
```

| Option        | Short | Default           | Description                        |
| ------------- | ----- | ----------------- | ---------------------------------- |
| `--host`      |       | `127.0.0.1`       | Host to bind                       |
| `--port`      | `-p`  | 3334              | Port to listen on                  |
| `--transport` | `-t`  | `streamable-http` | MCP transport                      |
| `--force`     | `-f`  | false             | Overwrite an existing service file |

### Examples

```bash
# Install the service file for the active local context
sibyl service install

# Bind a custom port and overwrite an existing file
sibyl service install --port 3344 --force
```

After install, start the service with the printed command:

```bash
# macOS
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/tech.hyperbliss.sibyl.plist

# Linux
systemctl --user enable --now sibyl.service
```

---

## service path

Print the native service file path for this platform.

```bash
sibyl service path
```

The path is `~/Library/LaunchAgents/tech.hyperbliss.sibyl.plist` on macOS and
`~/.config/systemd/user/sibyl.service` on Linux.

## Notes

- `service install` requires an active local context; switch to one with `sibyl context use local`
  or create one with [`sibyl init --local`](./init.md) first.
- The service launches `sibyld serve --embedded` and logs to `~/.sibyl/run/sibyld.service.log`.
- Native service files are only supported on macOS and Linux.

## Related Commands

- [`sibyl init`](./init.md) - Create the local context the service runs against
- [`sibyl doctor`](./doctor.md) - Verify daemon health after starting the service
- [`sibyl local`](./local.md) - Run a Docker-based local instance instead
