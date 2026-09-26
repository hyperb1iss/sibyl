# setup

Connect this machine to a Sibyl server in one command. `setup` picks or creates the context for the
server, signs you in through the browser, installs the Sibyl skill for your agents, and registers
the Claude Code SessionStart hook when Claude Code is installed. Every step checks first, so
re-running it only does what is missing.

Every install path ends with this command, and the web app's Connect card shows it with your
server's URL filled in:

```bash
# macOS
brew install hyperb1iss/tap/sibyl && sibyl setup https://your-sibyl-host

# Anywhere with uv
uv tool install --upgrade sibyl-dev && sibyl setup https://your-sibyl-host
```

## Synopsis

```bash
sibyl setup [url] [options]
```

## Options

| Option       | Short | Default          | Description                                   |
| ------------ | ----- | ---------------- | --------------------------------------------- |
| `url`        |       | current server   | Server URL, such as `https://sibyl.acme.io`   |
| `--yes`      | `-y`  | false            | Don't ask; for agents and scripts             |
| `--context`  | `-c`  | server host name | Context name to use or create                 |
| `--no-hooks` |       | false            | Skip the Claude Code SessionStart hook        |
| `--insecure` | `-k`  | false            | Skip TLS verification; allow http off-machine |

Without a URL, `setup` connects the server the CLI already talks to: the selected context, or
`http://localhost:3334`. Without a terminal, it runs as if `--yes` were passed.

Plain `http://` is accepted only for this machine (`localhost`, `127.0.0.0/8`, `::1`), because
sign-in sends a password or token over the connection. Use the server's https URL, or pass
`--insecure` on a network you trust. A context remembers `--insecure` only when you pass it; it is
never copied from another context for the same server.

Setup authenticates only with the login stored for the server it is connecting. A `SIBYL_AUTH_TOKEN`
in the environment is used only when `SIBYL_API_URL` names that same server; otherwise setup leaves
it out and warns that other commands in the shell still send it.

## What It Does

1. Checks the server is reachable and that this CLI meets the server's minimum version. When it does
   not, it prints the upgrade command for how the CLI was installed (`brew upgrade` or
   `uv tool install --upgrade`).
2. Selects the context that already points at the server, or creates one named after its host
   (adding the port when that name is taken, such as `local-3334`).
3. Signs in with the device flow unless a valid login exists. The browser page offers the server's
   SSO provider or its email and password sign-in. A new context becomes active only after sign-in
   succeeds; if it fails, the new context is removed and the previous one stays active.
4. Installs the skill into `~/.claude/skills`, `~/.codex/skills`, and `~/.agents/skills`.
5. Adds a SessionStart hook to `~/.claude/settings.json` that loads your active tasks and recent
   memory when a Claude Code session starts. Only hooks exactly as Sibyl's installer writes them
   (`python3 ~/.claude/hooks/sibyl/<script>.py`) are replaced; every other hook stays where it was.
   When the file already has hooks configured, a timestamped backup is written first. Codex and
   other agents have no hook.

## Hand It To An Agent

The server publishes the same steps as markdown for an AI coding agent. Paste the sentence from the
web app's Connect card into Claude Code, Codex, or any coding agent. On a server where one origin
fronts both the web app and the API, it reads:

```text
Set up Sibyl on this machine by following https://your-sibyl-host/agent
```

The document lives in two places:

| URL                               | Served by                                | Works on                        |
| --------------------------------- | ---------------------------------------- | ------------------------------- |
| `<web-origin>/agent`              | The web app, relaying the API's document | The web app's origin            |
| `<api-origin>/api/setup/agent.md` | The API                                  | The API's origin (always there) |

When the web app and API run on separate origins, the Connect card uses the API form, because the
`/agent` page does not exist on the API's origin.

The agent installs the CLI, runs `sibyl setup <url> --yes`, relays the sign-in to you, and confirms
with `sibyl whoami` and `sibyl doctor`.

## Related Commands

- [`sibyl doctor`](./doctor.md) - Verify the setup afterwards
- [`sibyl init`](./init.md) - Create a context without signing in
- [`sibyl auth`](./auth.md) - Sign in or out by hand
- [`sibyl skill`](./skill.md) - Install or print the skill on its own
