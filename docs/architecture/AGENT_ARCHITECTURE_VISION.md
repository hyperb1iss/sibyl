# Agent Architecture Vision

> **Status:** Draft v0.1 **Last Updated:** 2026-01-04 **Epic:** Agent Harness (epic_f54e98ffb7fe)

## Executive Summary

Sibyl's Agent Harness evolves from a Claude-only local execution system to a **universal agent
orchestration platform** supporting:

1. **Multiple Execution Environments** - Local filesystem for development, cloud dev containers for
   production deployments
2. **Multi-Agent SDK Support** - Claude Agent SDK, OpenAI Codex SDK, Google ADK (Gemini)
3. **Project Workflow Mapping** - Configurable branching strategies, review processes, and
   deployment pipelines per project

This enables teams to deploy any AI coding agent on their tasks while Sibyl provides the shared
memory, coordination, and workflow enforcement layer.

---

## Current State

### What We Have

```
┌─────────────────────────────────────────────────────────────────┐
│                         AgentOrchestrator                       │
│  - Multi-agent coordination                                     │
│  - Task distribution                                            │
│  - Inter-agent messaging                                        │
│  - Health monitoring                                            │
└──────────────────────────────┬──────────────────────────────────┘
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
┌──────────────────────────┐    ┌──────────────────────────────────┐
│      AgentRunner         │    │        WorktreeManager           │
│  - Claude SDK wrapper    │    │  - Git worktree isolation        │
│  - Spawn/resume agents   │    │  - Branch management             │
│  - Hook integration      │    │  - Conflict detection            │
└──────────────────────────┘    └──────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────┐
│                        AgentInstance                             │
│  - Heartbeat loop            - Checkpoint management             │
│  - Message streaming         - Cost/token tracking               │
│  - Approval integration      - Session persistence               │
└──────────────────────────────────────────────────────────────────┘
```

### Models in Place

| Model             | Purpose                                                  |
| ----------------- | -------------------------------------------------------- |
| `AgentRecord`     | Persistent agent state (status, task, worktree, session) |
| `WorktreeRecord`  | Git worktree registry for agent isolation                |
| `ApprovalRecord`  | Human-in-the-loop approval queue                         |
| `AgentCheckpoint` | Session state for crash recovery                         |

### Existing Hooks

- **ApprovalService**: Pre-tool hooks for destructive commands, sensitive files
- **SibylContextService**: Knowledge injection from graph
- **WorkflowTracker**: Sibyl workflow compliance tracking
- **User Hooks**: Merging Claude Code user/project hooks

---

## Vision: Three Pillars

### Pillar 1: Execution Environments

#### Local Development Mode (Current)

```
Agent
   │
   ▼
┌──────────────────────────┐
│  Local Filesystem        │
│  ~/.sibyl-worktrees/     │
│  └── org/project/branch/ │
└──────────────────────────┘
```

- Agents work directly in git worktrees on the host filesystem
- Ideal for individual developers running agents locally
- Fast iteration, direct file access
- **Current implementation status:** ✅ Working

#### Cloud Deployment Mode (Planned)

```
Agent
   │
   ▼
┌──────────────────────────────────────────────────────────┐
│                    Container Orchestrator                 │
├──────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐                │
│  │  Dev Container  │  │  Dev Container  │  ...           │
│  │  Agent A        │  │  Agent B        │                │
│  │  /workspace     │  │  /workspace     │                │
│  │  └── cloned     │  │  └── cloned     │                │
│  │      repo       │  │      repo       │                │
│  └─────────────────┘  └─────────────────┘                │
└──────────────────────────────────────────────────────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │   Remote Git Host    │
              │   (GitHub, GitLab)   │
              └──────────────────────┘
```

**Key differences from local:**

1. **Container isolation** - Each agent gets its own dev container (devcontainer spec or custom)
2. **Clone instead of worktree** - Full repo clone in container, not worktree
3. **Branch push strategy** - Agents push branches to remote for PRs
4. **Ephemeral workspaces** - Containers can be destroyed after task completion
5. **Resource limits** - CPU/memory quotas per agent

**Container lifecycle:**

```python
class CloudExecutionManager:
    """Manages cloud-based agent execution environments."""

    async def provision_container(
        self,
        agent_id: str,
        repo_url: str,
        branch: str,
        devcontainer_config: dict | None = None,
    ) -> ContainerInfo:
        """Spin up a dev container for an agent."""

    async def destroy_container(self, agent_id: str) -> None:
        """Clean up container after agent completion."""

    async def execute_in_container(
        self,
        agent_id: str,
        command: list[str],
    ) -> CommandResult:
        """Run a command inside the agent's container."""
```

---

### Pillar 2: Multi-SDK Agent Support

#### Current: Claude Agent SDK Only

```python
# runner.py - current implementation
from claude_agent_sdk import ClaudeSDKClient
async with ClaudeSDKClient(options=self.sdk_options) as client:
    await client.query(self.initial_prompt)
```

#### Target: Universal Agent Interface

```
┌─────────────────────────────────────────────────────────────────┐
│                     UniversalAgentRunner                        │
│  Provides consistent interface regardless of underlying SDK     │
└───────────────────────────┬─────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ ClaudeAdapter │   │ CodexAdapter  │   │ GeminiAdapter │
│               │   │               │   │               │
│ claude_agent  │   │ @openai/      │   │ google-adk    │
│ _sdk          │   │ codex-sdk     │   │               │
└───────────────┘   └───────────────┘   └───────────────┘
```

#### SDK Comparison (Research Findings)

| Feature             | Claude Agent SDK     | OpenAI Codex SDK         | Google ADK              |
| ------------------- | -------------------- | ------------------------ | ----------------------- |
| **Language**        | Python               | TypeScript (Node.js 18+) | Python, TS, Go, Java    |
| **Session Resume**  | `resume=session_id`  | `resumeThread(id)`       | Interactions API        |
| **Hooks**           | Pre/Post tool hooks  | -                        | Agent callbacks         |
| **MCP Integration** | Native               | Codex-as-MCP-server      | Agent-to-agent protocol |
| **Cost Tracking**   | Built-in             | Via responses            | Via Vertex AI           |
| **Streaming**       | `receive_response()` | Async iterator           | Event-based             |

#### Adapter Interface

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator

class AgentSDKAdapter(ABC):
    """Abstract base for SDK adapters."""

    @abstractmethod
    async def initialize(self, config: AgentConfig) -> str:
        """Initialize agent session, return session_id."""

    @abstractmethod
    async def execute(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Execute prompt and stream events."""

    @abstractmethod
    async def resume(self, session_id: str, prompt: str) -> AsyncIterator[AgentEvent]:
        """Resume from previous session."""

    @abstractmethod
    async def checkpoint(self) -> dict:
        """Capture state for persistence."""

    @abstractmethod
    def get_usage(self) -> UsageMetrics:
        """Return tokens/cost consumed."""


@dataclass
class AgentEvent:
    """Normalized event from any SDK."""
    event_type: Literal["text", "tool_call", "tool_result", "error", "done"]
    content: str | dict
    metadata: dict
```

#### Codex Integration Strategy

Codex exposes itself as an MCP server with two tools:

1. **`codex()`** - Start a session with config (prompt, approval policy, sandbox mode)
2. **`codex-reply()`** - Continue session with conversation ID

```python
class CodexAdapter(AgentSDKAdapter):
    """Adapter for OpenAI Codex via MCP bridge."""

    def __init__(self):
        # Codex runs as TypeScript MCP server
        # We communicate via MCP protocol
        self._mcp_client: MCPClient | None = None
        self._conversation_id: str | None = None

    async def initialize(self, config: AgentConfig) -> str:
        # Start Codex MCP server if not running
        await self._ensure_codex_mcp_running()

        # Call codex() tool to start session
        result = await self._mcp_client.call_tool(
            "codex",
            {
                "prompt": config.system_prompt,
                "approval_policy": "auto",  # Sibyl handles approvals
                "sandbox": "workspace-write",
            }
        )
        self._conversation_id = result["conversation_id"]
        return self._conversation_id
```

#### Gemini/ADK Integration Strategy

Google ADK is multi-language with Python support:

```python
from google.adk import Agent, LlmAgent
from google.adk.tools import FunctionTool

class GeminiAdapter(AgentSDKAdapter):
    """Adapter for Google Gemini via ADK."""

    async def initialize(self, config: AgentConfig) -> str:
        # Create ADK agent with Sibyl tools
        self._agent = LlmAgent(
            name=config.agent_name,
            model="gemini-3-pro",
            instruction=config.system_prompt,
            tools=self._create_sibyl_tools(),
        )

        # ADK provides session management via Interactions API
        self._session = await self._agent.create_session()
        return self._session.id

    def _create_sibyl_tools(self) -> list[FunctionTool]:
        """Expose Sibyl capabilities as ADK tools."""
        return [
            FunctionTool(fn=self._sibyl_search, name="sibyl_search"),
            FunctionTool(fn=self._sibyl_add, name="sibyl_add"),
            FunctionTool(fn=self._sibyl_task_update, name="sibyl_task_update"),
        ]
```

---

### Pillar 3: Flexibility & Configuration

**Core principle:** Sibyl provides sensible defaults but never locks users into a specific workflow.
Every aspect of agent execution should be configurable at multiple levels.

#### Configuration Hierarchy

```
spawn() options  →  User settings  →  Project config  →  Org defaults  →  Sibyl defaults
    (highest)                                                              (lowest)
```

Each level can override the one below. Runtime options always win.

#### User-Level Settings

```yaml
# ~/.config/sibyl/settings.yaml (or via CLI: sibyl config set)
agent:
  # Where worktrees live - default: ~/.sibyl-worktrees
  worktree_base: ~/dev/.worktrees

  # Branch naming patterns - use {task}, {agent}, {date}, {short_id}
  branch_template: "feat/{task}-{short_id}" # default: "agent/{short_id}-{task}"

  # Default isolation strategy
  isolation_mode: worktree # or: branch, clone, none

  # Auto-cleanup worktrees after merge
  cleanup_merged: true
  cleanup_after_hours: 72

  # Post-setup commands (run after worktree/clone is created)
  # These make the workspace usable (install deps, generate files, etc.)
  setup_commands:
    - "pnpm install"
    - "cp .env.example .env.local"
  setup_timeout: 300 # seconds
```

#### Runtime Spawn Options

When spawning an agent (UI, CLI, or API), users can override defaults:

```python
# API example
await orchestrator.spawn_agent(
    prompt="Implement the login page",
    task=task,
    # Isolation strategy - choose what makes sense for this task
    isolation=IsolationStrategy.WORKTREE,  # or BRANCH, CLONE, NONE

    # Branch naming - override template or specify exact name
    branch_name="feature/login-page",  # explicit
    # OR
    branch_template="fix/{task}",  # template override

    # Worktree location - override default base
    worktree_path="/tmp/quick-fix",  # explicit path

    # Skip creating isolation entirely (work in main repo)
    create_worktree=False,
)
```

#### Isolation Strategies

| Strategy   | Use Case                  | How It Works                          |
| ---------- | ------------------------- | ------------------------------------- |
| `WORKTREE` | Parallel work, local dev  | Git worktree in configurable location |
| `BRANCH`   | Simple tasks, shared repo | New branch, no separate directory     |
| `CLONE`    | Cloud/container mode      | Full repo clone (ephemeral)           |
| `NONE`     | Quick fixes, exploration  | Work directly in current branch       |

#### Project Workflow Config (Optional)

Projects _may_ define workflows, but these are **suggestions not requirements**. Sibyl will warn
(not block) when agents deviate.

```yaml
# .sibyl/workflow.yaml (optional per-project)
version: 1

# Workspace setup - runs after worktree/clone is created
setup:
  commands:
    - "pnpm install"
    - "cp .env.example .env.local"
    - "pnpm db:generate" # Generate Prisma client, etc.
  timeout: 300
  # Can also specify platform-specific commands
  commands_darwin: # macOS
    - "brew bundle --no-lock"
  commands_linux:
    - "sudo apt-get update && sudo apt-get install -y libfoo"

# Suggestions for this project - agents will see these but can override
suggestions:
  branching:
    preferred_strategy: "feature-branch"
    main_branch: "main"
    suggested_prefix: "agent/"

  testing:
    recommend_before_commit:
      - "pnpm lint"
      - "pnpm typecheck"
    recommend_before_merge:
      - "pnpm test"

# Hard constraints (will block, not just warn)
constraints:
  blocked_paths:
    - ".env*"
    - "secrets/"
  require_tests_pass: true # only blocks merge, not commit

# Team conventions (shown to agents, not enforced)
conventions:
  commit_style: "conventional"
  pr_template: ".github/pull_request_template.md"
```

**Key differences from previous version:**

- `suggestions` vs `constraints` — soft vs hard rules
- No enforced branching strategy
- Testing is recommended, only blocking at merge time
- Conventions are documentation, not enforcement

#### Configuration Resolution

```python
@dataclass
class AgentSpawnConfig:
    """Resolved configuration for spawning an agent."""
    isolation: IsolationStrategy
    branch_name: str
    worktree_base: Path | None
    worktree_path: Path | None  # Explicit path overrides base

    @classmethod
    def resolve(
        cls,
        spawn_options: SpawnOptions,
        user_settings: UserSettings,
        project_config: ProjectConfig | None,
    ) -> "AgentSpawnConfig":
        """Resolve config from hierarchy - spawn options win."""

class ConfigResolver:
    """Resolves agent spawn configuration from multiple sources."""

    async def resolve(self, spawn_options: SpawnOptions) -> AgentSpawnConfig:
        """Load and merge configs: spawn > user > project > defaults."""

    def format_branch_name(self, template: str, context: dict) -> str:
        """Apply template with {task}, {agent}, {date}, {short_id}."""
```

#### Isolation Strategy Implementation

```python
class IsolationStrategy(StrEnum):
    WORKTREE = "worktree"  # Git worktree (current default)
    BRANCH = "branch"      # Branch only, no separate dir
    CLONE = "clone"        # Full clone (cloud mode)
    NONE = "none"          # Work in place

class IsolationManager:
    """Creates isolated workspaces based on strategy."""

    async def create_workspace(
        self,
        strategy: IsolationStrategy,
        config: AgentSpawnConfig,
    ) -> Workspace:
        """Create workspace using configured strategy."""
        match strategy:
            case IsolationStrategy.WORKTREE:
                return await self._create_worktree(config)
            case IsolationStrategy.BRANCH:
                return await self._create_branch(config)
            case IsolationStrategy.CLONE:
                return await self._create_clone(config)
            case IsolationStrategy.NONE:
                return Workspace(path=self.repo_path, branch=None)
```

---

## Implementation Phases

### Phase 1: Foundation (Current + Near-term)

**Goal:** Solidify current Claude-only local execution, complete git integration

| Task                                        | Epic            | Priority |
| ------------------------------------------- | --------------- | -------- |
| Build merge orchestration for worktrees     | Git Integration | Critical |
| Implement conflict detection with git diff  | Git Integration | High     |
| Build conflict resolution UI                | Git Integration | High     |
| Implement automatic PR creation             | Git Integration | High     |
| Integrate test suite execution before merge | Git Integration | High     |
| Enable cross-project agent management       | Git Integration | Medium   |

### Phase 2: Workflow Engine

**Goal:** Project-specific workflow configuration and enforcement

| Task                                    | Epic | Priority |
| --------------------------------------- | ---- | -------- |
| Define workflow configuration schema    | New  | High     |
| Implement WorkflowEngine core           | New  | High     |
| Add pre-commit/pre-merge hook execution | New  | High     |
| Build PR creation with conventions      | New  | Medium   |
| Add workflow validation in hooks        | New  | Medium   |

### Phase 3: Multi-SDK Support

**Goal:** Add Codex and Gemini as execution backends

| Task                                | Epic | Priority |
| ----------------------------------- | ---- | -------- |
| Define AgentSDKAdapter interface    | New  | High     |
| Refactor runner.py to use adapters  | New  | High     |
| Implement CodexAdapter (MCP bridge) | New  | Medium   |
| Implement GeminiAdapter (ADK)       | New  | Medium   |
| Add SDK selection to spawn API      | New  | Medium   |
| SDK-agnostic hook system            | New  | Medium   |

### Phase 4: Cloud Execution

**Goal:** Dev containers for cloud deployments

| Task                                 | Epic | Priority |
| ------------------------------------ | ---- | -------- |
| Design container orchestration layer | New  | High     |
| Implement CloudExecutionManager      | New  | High     |
| Add devcontainer spec support        | New  | Medium   |
| Clone-based workspace management     | New  | Medium   |
| Container resource management        | New  | Medium   |
| Remote git push/PR workflow          | New  | High     |

---

## Open Questions

### Technical

1. **Container orchestration backend** - Docker Compose? Kubernetes? Cloud Run?
2. **Codex MCP bridge** - Run TypeScript server from Python? Or sidecar?
3. **ADK version pinning** - Which versions of each SDK to target?

### Product

1. **SDK selection UX** - How do users choose which agent to deploy on a task?
2. **Cost attribution** - How to handle different pricing across SDKs?
3. **Capability mapping** - Some agents are better at certain tasks - expose this?

### Workflow

1. **Default workflows** - Provide templates for common setups (GitHub Flow, GitLab Flow)?
2. **Workflow migration** - How to handle workflow config changes mid-task?
3. **Multi-repo projects** - Some projects span multiple repositories

---

## Appendix: SDK Documentation

### Claude Agent SDK

- Session management via `ClaudeSDKClient`
- Hooks: `PreToolUse`, `PostToolUse`, `StopAssistant`
- Resume: Pass `resume=session_id` to options

### OpenAI Codex SDK

- NPM: `@openai/codex-sdk`
- MCP server: `codex mcp-server`
- Tools: `codex()`, `codex-reply()`
- Model: `codex-mini-latest` ($1.50/1M input, $6/1M output)

### Google Agent Development Kit

- PyPI: `google-adk`
- Workflow agents: Sequential, Parallel, Loop
- A2A Protocol for inter-agent communication
- Interactions API for stateful sessions

---

## Sources

- [Codex SDK Documentation](https://developers.openai.com/codex/sdk/)
- [Codex with Agents SDK Guide](https://developers.openai.com/codex/guides/agents-sdk/)
- [Google Agent Development Kit](https://google.github.io/adk-docs/)
- [Gemini 3 Developer Features](https://blog.google/technology/developers/gemini-3-developers/)
