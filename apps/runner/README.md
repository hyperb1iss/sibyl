# Sibyl Runner

Distributed agent execution daemon for Sibyl. Runs on host machines to execute AI agents in isolated git worktrees.

## Installation

```bash
# Via Homebrew (macOS)
brew tap hyperb1iss/sibyl
brew install sibyl-runner

# Via pip/uv (any platform)
pip install sibyl-runner
# or
uv tool install sibyl-runner

# From source (development)
uv tool install --editable ./apps/runner
```

## Quick Start

```bash
# 1. Register this machine as a runner with your Sibyl server
sibyl-runner register --server https://sibyl.example.com --name "dev-machine"

# 2. Start the daemon
sibyl-runner run

# 3. Check status
sibyl-runner status
```

## Sandbox Mode

Sandbox mode is an env-bootstrapped runner path for ephemeral execution environments.

```bash
export SIBYL_SERVER_URL=https://sibyl.example.com
export SIBYL_RUNNER_ID=<runner-id>
export SIBYL_RUNNER_TOKEN=<runner-token>
export SIBYL_SANDBOX_ID=<sandbox-id>

# Optional toggles
export SIBYL_SANDBOX_MODE=true
export SIBYL_WORKTREE_BASE=/tmp/sibyl/worktrees
export SIBYL_SANDBOX_WORKTREE_BASE=/tmp/sibyl/sandboxes/$SIBYL_SANDBOX_ID/worktrees

sibyl-runner run --sandbox-mode
```

## How It Works

1. **Register** - Runner registers with Sibyl Core, receiving a unique ID
2. **Connect** - Daemon connects via WebSocket for real-time communication
3. **Receive** - Server assigns tasks based on project affinity and capacity
4. **Execute** - Runner creates isolated worktrees and runs agents
5. **Report** - Progress and results stream back to server

## Configuration

Config stored at `~/.config/sibyl/runner.yaml`:

```yaml
server_url: https://sibyl.example.com
runner_id: abc-123-def
name: dev-machine
max_concurrent_agents: 3
capabilities:
  - docker
  - gpu
worktree_base: ~/.sibyl/worktrees
```

## Architecture

```
┌─────────────────┐     WebSocket      ┌─────────────────┐
│   Sibyl Core    │◄─────────────────►│  sibyl-runner   │
│   (Server)      │                    │   (Daemon)      │
└─────────────────┘                    └─────────────────┘
                                              │
                                              ▼
                                       ┌─────────────────┐
                                       │  Git Worktrees  │
                                       │  (Isolated)     │
                                       └─────────────────┘
```

## Commands

| Command | Description |
|---------|-------------|
| `register` | Register this machine with a Sibyl server |
| `run` | Start the runner daemon |
| `status` | Show runner status |

## Docker Deployment

```bash
# Build the image (from repo root)
docker build -f apps/runner/Dockerfile -t sibyl-runner .

# Run with docker-compose (from apps/runner)
cd apps/runner
docker-compose up -d

# Or run directly
docker run -d \
  --name sibyl-runner \
  -e SIBYL_SERVER_URL=https://sibyl.example.com \
  -v sibyl-worktrees:/var/sibyl/worktrees \
  -v sibyl-config:/var/sibyl/config \
  sibyl-runner
```

### Docker Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIBYL_SERVER_URL` | - | Sibyl server URL (required) |
| `SIBYL_RUNNER_ID` | - | Runner ID for connection (`--runner-id` alternative) |
| `SIBYL_RUNNER_TOKEN` | - | Runner auth token used for WebSocket auth |
| `SIBYL_SANDBOX_ID` | - | Sandbox execution context identifier |
| `SIBYL_SANDBOX_MODE` | `false` | Enable sandbox mode (`true/false`) |
| `SIBYL_RUNNER_MODE` | - | Alternate mode selector (`sandbox` or `registered`) |
| `SIBYL_WORKTREE_BASE` | `/var/sibyl/worktrees` | Base directory for git worktrees |
| `SIBYL_SANDBOX_WORKTREE_BASE` | - | Sandbox-specific worktree base override |
| `SIBYL_CONFIG_DIR` | `/var/sibyl/config` | Config directory (runner.yaml) |

### First-time Setup

The runner must be registered before it can connect:

```bash
# Register the runner (creates runner.yaml)
docker exec -it sibyl-runner sibyl-runner register \
  --server https://sibyl.example.com \
  --name "cloud-runner-1"

# Restart to apply registration
docker restart sibyl-runner
```

### Sandbox Docker Image

`apps/runner/Dockerfile.sandbox` is optimized for sandbox mode and uses a devcontainer base image.

```bash
# Build sandbox image
docker build -f apps/runner/Dockerfile.sandbox -t sibyl-runner-sandbox .

# Run sandbox runner (env bootstrap, no register command required if runner ID is provided)
docker run --rm \
  -e SIBYL_SERVER_URL=https://sibyl.example.com \
  -e SIBYL_RUNNER_ID=<runner-id> \
  -e SIBYL_RUNNER_TOKEN=<runner-token> \
  -e SIBYL_SANDBOX_ID=<sandbox-id> \
  -v runner-worktrees:/workspace/.sibyl/worktrees \
  sibyl-runner-sandbox
```

## Development

```bash
# Run from source
cd apps/runner
uv run sibyl-runner --help

# Run tests
uv run pytest

# Type check
uv run pyright src
```
