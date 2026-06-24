# skill

Install the loader skill and print bundled markdown packs. `skill` manages the Sibyl skill stub that
Claude Code, Codex, and other assistants load to learn the memory loop, and it prints
version-matched markdown packs straight from the installed CLI bundle.

Called with no subcommand, `skill` prints the canonical loader markdown to stdout. Pass `--install`
to write it into your assistant skill roots instead.

## Synopsis

```bash
sibyl skill [options]
sibyl skill <command> [options]
```

## Options

| Option      | Short | Default | Description                                             |
| ----------- | ----- | ------- | ------------------------------------------------------- |
| `--install` |       | false   | Install the loader skill into assistant skill roots     |
| `--force`   |       | false   | Replace existing symlink or non-directory skill targets |
| `--quiet`   | `-q`  | false   | Suppress install status output                          |

## Commands

| Command                                 | Description                                          |
| --------------------------------------- | ---------------------------------------------------- |
| [`sibyl skill install`](#skill-install) | Install the stable Sibyl skill stub into skill roots |
| [`sibyl skill list`](#skill-list)       | List skill packs available from this CLI version     |
| [`sibyl skill get`](#skill-get)         | Print a version-matched markdown skill pack          |

Skill roots are `~/.claude/skills`, `~/.codex/skills`, and `~/.agents/skills`.

---

## skill install

Install the stable Sibyl skill stub into assistant skill roots.

```bash
sibyl skill install [options]
```

| Option    | Short | Default | Description                                             |
| --------- | ----- | ------- | ------------------------------------------------------- |
| `--force` |       | false   | Replace existing symlink or non-directory skill targets |
| `--quiet` | `-q`  | false   | Suppress install status output                          |

### Example

```bash
sibyl skill install --force
```

---

## skill list

List the skill packs bundled with this installed CLI version.

```bash
sibyl skill list
```

Packs include `core`, `quick`, `workflows`, `examples`, and `migration`.

---

## skill get

Print a version-matched markdown skill pack from the CLI bundle. Defaults to the `core` pack.

```bash
sibyl skill get [name]
```

| Argument | Required | Default | Description                                          |
| -------- | -------- | ------- | ---------------------------------------------------- |
| `name`   | No       | `core`  | Skill pack to print (`sibyl skill list` for choices) |

### Examples

```bash
# Print the core workflow pack
sibyl skill get

# Print the minimal subagent pack
sibyl skill get quick
```

## Notes

- The stub is intentionally stable: it points assistants at the version-matched packs rather than
  embedding the full contract, so a CLI upgrade refreshes guidance without re-installing.
- `--force` replaces existing symlinks or non-directory targets; without it, those roots are
  skipped.
- [`sibyl doctor`](./doctor.md) checks that the installed stub matches the canonical markdown and
  flags stale copies.

## Related Commands

- [`sibyl doctor`](./doctor.md) - Verify the skill stub is installed and current
- [`sibyl update`](./update.md) - Refresh skills as part of a self-update
- [`sibyl local`](./local.md) - Install skills and hooks together with `local setup`
