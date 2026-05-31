# Contributing to Sibyl

Thanks for your interest in Sibyl. This guide covers how to set up a development
environment, the quality gates every change must pass, and how we work.

Sibyl is dogfooded: **we use Sibyl to build Sibyl.** Tasks, learnings, and decisions
for this project live in Sibyl itself.

## Development Setup

```bash
# One-line setup (installs proto, moon, the toolchain, and dependencies)
./setup-dev.sh              # macOS / Linux
pwsh -File .\setup-dev.ps1  # Windows (PowerShell 7+)

# Or manually
curl -fsSL https://moonrepo.dev/install/proto.sh | bash
proto use                  # Installs node, pnpm, python, uv
proto install moon
uv sync && pnpm install

# Configure
cp .env.example .env
# SIBYL_JWT_SECRET is auto-generated in dev. Add at least one LLM provider key
# and an embedding key (SIBYL_OPENAI_API_KEY or SIBYL_GEMINI_API_KEY).

# Install the CLIs in editable mode and start the stack
moon run install-dev
moon run dev
```

`moon run dev` starts local SurrealDB, the API on `:3334`, and the web UI on `:3337`,
with background jobs running in-process. Verify with
`curl http://localhost:3334/api/health`.

## Monorepo & moon

Sibyl is a moonrepo monorepo. **Always use `moon run`** for lint, test, build, and
typecheck. It caches results, runs only what changed, and respects cross-package
dependencies. Raw `pnpm` or `uv` commands bypass that graph.

```bash
moon run :check           # Lint + typecheck + test for the current project
moon run :lint            # Lint
moon run :test            # Test
moon run :typecheck       # Typecheck
moon run api:test         # Target a specific package
moon run core:check       # Full check on sibyl-core
```

Python packages use the Astral stack: **uv** (dependencies), **ruff** (lint/format),
**ty** (type checking). Never run `uv pip`; use `uv add`, `uv sync`, or `uv run`. The
web app uses **pnpm**, **Biome**, and **Vitest**.

## Quality Gates

Every change must pass `moon run :check` before review:

- **Lint:** ruff (Python), Biome (TypeScript)
- **Typecheck:** ty (Python), `tsc` (TypeScript)
- **Tests:** pytest (Python), Vitest (web)

End-to-end tests under `apps/e2e/` require a running stack. See
[`apps/e2e/README.md`](apps/e2e/README.md).

## Commit Conventions

Sibyl uses [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): short summary`.

- Common types: `feat`, `fix`, `refactor`, `docs`, `style`, `test`, `chore`, `perf`,
  `build`, `ci`.
- Subject line in imperative mood, 72 characters or fewer, no trailing period.
- Every commit gets a body explaining why, wrapped at 72 characters.

## Pull Requests

1. Fork and branch from `main` (`feat/...`, `fix/...`, `docs/...`).
2. Make focused, atomic commits.
3. Run `moon run :check` and make sure it passes.
4. Open a PR against `main` with a clear description of what changed and why.
5. CI must be green before merge.

## License

Sibyl is licensed under **Apache-2.0**. By contributing, you agree that your
contributions are licensed under the same terms. See [LICENSE](LICENSE).
