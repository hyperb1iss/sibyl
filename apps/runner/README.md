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
uv pip install -e apps/runner
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
| `SIBYL_WORKTREE_BASE` | `/var/sibyl/worktrees` | Base directory for git worktrees |
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
