# brief

One-shot lean context brief for injecting into a subagent prompt. `brief` is a stripped-down sibling
of [`sibyl recall`](./recall.md): given a goal, it prints wake-layer markdown only, with no skill
ceremony, no related-graph expansion, and no JSON envelope. Pipe or paste the output straight into a
worker agent's prompt.

## Synopsis

```bash
sibyl brief <goal> [options]
```

## Arguments

| Argument | Required | Description           |
| -------- | -------- | --------------------- |
| `goal`   | Yes      | Subagent goal or task |

## Options

| Option      | Short | Default | Description                                       |
| ----------- | ----- | ------- | ------------------------------------------------- |
| `--intent`  | `-i`  | `build` | Agent intent (see [`recall`](./recall.md#intent)) |
| `--project` | `-p`  | (auto)  | Project ID                                        |
| `--all`     | `-a`  | false   | Use all accessible projects                       |
| `--budget`  |       | 1500    | Token budget for the rendered brief (100-8000)    |

## Examples

```bash
# Lean brief for a worker agent
sibyl brief "implement the password reset endpoint"

# Bias toward debugging context with a smaller budget
sibyl brief "auth token refresh fails intermittently" --intent debug --budget 800

# Inject straight into a subagent prompt
PACK=$(sibyl brief "wire up OAuth2")
```

## Notes

- `brief` always uses the `wake` layer with related-graph expansion off, so it is faster and leaner
  than `recall`. Reach for `recall` when you need a deeper pack, raw memories, a diary, or JSON.
- The project resolves from the current directory unless you pass `--project` or `--all`.

## Related Commands

- [`sibyl recall`](./recall.md) - Full working context pack with more layers and options
- [`sibyl context`](./context.md) - Lower-level context compilation
- [`sibyl session`](./session.md) - Wake-up bundle for a new session
