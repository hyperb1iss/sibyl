# Sibyl Runner

Distributed agent execution daemon for Sibyl. Runs on host machines to execute AI agents in isolated git worktrees.

## Installation

```bash
# From source (development)
uv pip install -e apps/runner

# Via Homebrew (coming soon)
brew install sibyl-runner
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
