# Sibyl development tasks

default:
    @just --list

# Lint (ruff + pyright)
lint:
    uv run ruff check .
    uv run pyright

# Fix (ruff fix + format)
fix:
    uv run ruff check --fix .
    uv run ruff format .

# Fix all (including unsafe fixes)
fix-all:
    uv run ruff check --fix --unsafe-fixes .
    uv run ruff format .

# Run tests
test *args:
    uv run pytest {{args}}

# Run the server
serve:
    uv run sibyl serve

# Install sibyl skills globally (Claude + Codex)
install-skills:
    @echo "Installing Sibyl skills..."
    @mkdir -p ~/.claude/skills ~/.codex/skills
    @ln -sfn "$(pwd)/.claude/skills/sibyl-knowledge" ~/.claude/skills/sibyl-knowledge
    @ln -sfn "$(pwd)/.claude/skills/sibyl-project-manager" ~/.claude/skills/sibyl-project-manager
    @ln -sfn "$(pwd)/.claude/skills/sibyl-knowledge" ~/.codex/skills/sibyl-knowledge
    @ln -sfn "$(pwd)/.claude/skills/sibyl-project-manager" ~/.codex/skills/sibyl-project-manager
    @echo "✓ Installed to ~/.claude/skills and ~/.codex/skills"
