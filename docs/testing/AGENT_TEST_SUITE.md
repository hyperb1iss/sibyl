# Agent Test Suite Architecture

## Overview

The agent test suite validates the Agent Harness infrastructure across multiple layers:

1. **Unit Tests** (mocked) - Fast, CI-friendly, no API keys required
2. **Integration Tests** (real services, mocked LLM) - Test infrastructure without LLM costs
3. **Live Model Tests** (real Claude API) - Validate actual agent behavior with real models

## Test Categories

### Layer 1: Unit Tests (Existing)

Location: `apps/api/tests/test_agents.py`

- AgentRunner spawn/stop lifecycle
- AgentInstance state transitions
- AgentOrchestrator coordination
- Fire-and-forget helpers

**Run with:**
```bash
SIBYL_MOCK_LLM=true uv run pytest apps/api/tests/test_agents.py
```

### Layer 2: Infrastructure Integration Tests

Location: `apps/api/tests/test_agents_infra.py`

Tests that need real services (FalkorDB, Redis) but mock the LLM:

- WorktreeManager create/cleanup with real git operations
- IntegrationManager rebase/merge workflows
- CheckpointManager persistence to graph
- ApprovalService Redis pub/sub
- Lock contention scenarios

**Run with:**
```bash
SIBYL_MOCK_LLM=true uv run pytest apps/api/tests/test_agents_infra.py
```

### Layer 3: Live Model Tests

Location: `apps/api/tests/live/test_agents_live.py`

Tests with real Claude API calls:

- Agent completes simple coding task
- Agent uses tools correctly (Read, Write, Bash)
- Multi-turn conversation handling
- Approval flow interruption
- Checkpoint and resume
- Cost tracking accuracy

**Run with:**
```bash
# Requires ANTHROPIC_API_KEY
uv run pytest apps/api/tests/live/ -v --live-models
```

## Test Configuration

### Environment Variables

```bash
# Required for live tests
ANTHROPIC_API_KEY=sk-ant-...

# Optional: Override defaults
SIBYL_TEST_MODEL=claude-sonnet-4-20250514  # Default model for tests
SIBYL_TEST_MAX_TOKENS=1024                  # Token limit per request
SIBYL_TEST_MAX_TURNS=5                      # Max conversation turns
SIBYL_TEST_TIMEOUT=120                      # Seconds before timeout
```

### pytest Markers

```python
# Skip in CI (expensive)
@pytest.mark.live_model
async def test_agent_completes_task(): ...

# Slow integration tests
@pytest.mark.slow
async def test_large_worktree_cleanup(): ...

# Requires specific services
@pytest.mark.requires_redis
@pytest.mark.requires_falkordb
async def test_approval_pubsub(): ...
```

### conftest.py Additions

```python
def pytest_addoption(parser):
    parser.addoption(
        "--live-models",
        action="store_true",
        default=False,
        help="Run tests that call real LLM APIs (requires API keys)",
    )
    parser.addoption(
        "--cost-limit",
        type=float,
        default=1.0,
        help="Maximum cost in USD for live model tests",
    )

def pytest_configure(config):
    config.addinivalue_line("markers", "live_model: tests requiring real LLM API calls")
    config.addinivalue_line("markers", "slow: tests taking >30s")
    config.addinivalue_line("markers", "requires_redis: tests requiring Redis")
    config.addinivalue_line("markers", "requires_falkordb: tests requiring FalkorDB")

def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live-models"):
        skip_live = pytest.mark.skip(reason="need --live-models to run")
        for item in items:
            if "live_model" in item.keywords:
                item.add_marker(skip_live)

@pytest.fixture(scope="session")
def live_model_config():
    """Configuration for live model tests."""
    return LiveModelConfig(
        model=os.getenv("SIBYL_TEST_MODEL", "claude-sonnet-4-20250514"),
        max_tokens=int(os.getenv("SIBYL_TEST_MAX_TOKENS", "1024")),
        max_turns=int(os.getenv("SIBYL_TEST_MAX_TURNS", "5")),
        timeout=int(os.getenv("SIBYL_TEST_TIMEOUT", "120")),
    )
```

## Live Test Scenarios

### 1. Basic Agent Execution

**Goal:** Verify agent can receive prompt, think, and respond.

```python
@pytest.mark.live_model
async def test_agent_basic_response(live_model_config, tmp_worktree):
    """Agent responds coherently to simple prompt."""
    runner = AgentRunner(...)

    instance = await runner.spawn(
        prompt="What is 2 + 2? Reply with just the number.",
        model=live_model_config.model,
        max_tokens=100,
    )

    response = await instance.execute()

    assert "4" in response.content
    assert instance.record.status == AgentStatus.COMPLETED
    assert instance.record.total_tokens > 0
```

### 2. Tool Usage Validation

**Goal:** Verify agent correctly uses file tools.

```python
@pytest.mark.live_model
async def test_agent_reads_file(live_model_config, tmp_worktree):
    """Agent uses Read tool to examine file contents."""
    # Setup: Create a file in worktree
    test_file = tmp_worktree / "test.py"
    test_file.write_text("def hello(): return 'world'")

    runner = AgentRunner(...)
    instance = await runner.spawn(
        prompt=f"Read {test_file} and tell me what the hello function returns.",
        allowed_tools=["Read"],
    )

    response = await instance.execute()

    assert "world" in response.content.lower()
    # Verify Read tool was called
    assert any(m.tool_name == "Read" for m in instance.messages if hasattr(m, 'tool_name'))
```

### 3. Multi-Turn Conversation

**Goal:** Verify stateful conversation handling.

```python
@pytest.mark.live_model
async def test_agent_multi_turn(live_model_config):
    """Agent maintains context across turns."""
    runner = AgentRunner(...)
    instance = await runner.spawn(prompt="My favorite number is 42.")

    # First turn
    await instance.send("Remember that number.")

    # Second turn - verify memory
    response = await instance.send("What's my favorite number?")

    assert "42" in response.content
```

### 4. Approval Flow

**Goal:** Verify agent pauses for dangerous operations.

```python
@pytest.mark.live_model
@pytest.mark.requires_redis
async def test_agent_approval_pause(live_model_config, approval_service):
    """Agent pauses when attempting dangerous operation."""
    runner = AgentRunner(...)
    instance = await runner.spawn(
        prompt="Delete all files in /tmp/test_dir",
        enable_approvals=True,
    )

    # Start execution in background
    exec_task = asyncio.create_task(instance.execute())

    # Wait for approval request
    approval = await asyncio.wait_for(
        approval_service.wait_for_request(instance.id),
        timeout=30,
    )

    assert approval.type == "bash_command"
    assert "rm" in approval.command or "delete" in approval.summary.lower()

    # Deny and verify agent handles it
    await approval_service.deny(approval.id, reason="Test denial")

    result = await exec_task
    assert instance.record.status != AgentStatus.FAILED
```

### 5. Checkpoint & Resume

**Goal:** Verify session can be persisted and resumed.

```python
@pytest.mark.live_model
async def test_agent_checkpoint_resume(live_model_config, checkpoint_manager):
    """Agent can be checkpointed and resumed."""
    runner = AgentRunner(...)
    instance = await runner.spawn(prompt="Count from 1 to 10, one number at a time.")

    # Let it run a few turns
    await instance.send("Start counting")
    await instance.send("Continue")

    # Checkpoint
    checkpoint = await instance.checkpoint()
    original_messages = len(instance.messages)

    # Stop and resume
    await instance.stop(reason="checkpoint_test")

    resumed = await runner.resume_from_checkpoint(checkpoint.id)

    assert len(resumed.messages) >= original_messages
    # Continue and verify context
    response = await resumed.send("What number were you on?")
    assert any(str(n) in response.content for n in range(1, 11))
```

### 6. Cost Tracking

**Goal:** Verify token/cost accounting is accurate.

```python
@pytest.mark.live_model
async def test_agent_cost_tracking(live_model_config):
    """Agent accurately tracks token usage and costs."""
    runner = AgentRunner(...)
    instance = await runner.spawn(prompt="Write a haiku about coding.")

    response = await instance.execute()

    # Verify tokens tracked
    assert instance.record.total_tokens > 0
    assert instance.record.input_tokens > 0
    assert instance.record.output_tokens > 0

    # Verify cost calculation (rough sanity check)
    expected_cost = calculate_cost(
        instance.record.input_tokens,
        instance.record.output_tokens,
        live_model_config.model,
    )
    assert abs(instance.record.cost_usd - expected_cost) < 0.001
```

## Fixtures

### tmp_worktree

Creates a real git worktree in a temp directory, cleans up after.

```python
@pytest.fixture
async def tmp_worktree(tmp_path, worktree_manager):
    """Create a temporary git worktree for testing."""
    # Initialize a git repo
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    await run_git(repo_path, "init")
    await run_git(repo_path, "commit", "--allow-empty", "-m", "Initial")

    # Create worktree
    worktree = await worktree_manager.create(
        task_id="test_task",
        branch_name="test-branch",
        repo_path=repo_path,
    )

    yield Path(worktree.path)

    # Cleanup
    await worktree_manager.cleanup(worktree.id)
```

### mock_claude_sdk

Replaces Claude SDK with controllable mock for infrastructure tests.

```python
@pytest.fixture
def mock_claude_sdk():
    """Mock Claude SDK for testing without API calls."""
    responses = []

    class MockClient:
        async def create_message(self, **kwargs):
            if responses:
                return responses.pop(0)
            return MockMessage(content="Mock response")

        def queue_response(self, response):
            responses.append(response)

    with patch("anthropic.AsyncAnthropic", return_value=MockClient()):
        yield MockClient()
```

## Cost Controls

### Budget Enforcement

```python
class CostTracker:
    """Track cumulative test costs and enforce limits."""

    def __init__(self, limit_usd: float = 1.0):
        self.limit = limit_usd
        self.spent = 0.0
        self._lock = asyncio.Lock()

    async def record(self, cost: float) -> None:
        async with self._lock:
            self.spent += cost
            if self.spent > self.limit:
                raise CostLimitExceeded(
                    f"Test cost ${self.spent:.4f} exceeds limit ${self.limit:.2f}"
                )

    def report(self) -> str:
        return f"Total test cost: ${self.spent:.4f} / ${self.limit:.2f}"

@pytest.fixture(scope="session")
def cost_tracker(request):
    limit = request.config.getoption("--cost-limit")
    tracker = CostTracker(limit_usd=limit)
    yield tracker
    print(f"\n{tracker.report()}")
```

### Token Limits

```python
@pytest.fixture
def limited_agent_config(live_model_config, cost_tracker):
    """Agent config with strict token limits for testing."""
    return AgentConfig(
        model=live_model_config.model,
        max_tokens=512,  # Keep responses short
        max_turns=3,     # Limit conversation length
        cost_tracker=cost_tracker,
    )
```

## CI/CD Integration

### GitHub Actions

```yaml
# .github/workflows/test-agents.yml
name: Agent Tests

on:
  pull_request:
    paths:
      - 'apps/api/src/sibyl/agents/**'
      - 'apps/api/tests/test_agents*.py'
      - 'apps/api/tests/live/**'
  schedule:
    - cron: '0 6 * * *'  # Daily at 6am UTC

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run unit tests (mocked)
        run: |
          SIBYL_MOCK_LLM=true uv run pytest apps/api/tests/test_agents.py -v

  integration-tests:
    runs-on: ubuntu-latest
    services:
      falkordb:
        image: falkordb/falkordb:latest
        ports: ['6380:6379']
      redis:
        image: redis:7
        ports: ['6379:6379']
    steps:
      - uses: actions/checkout@v4
      - name: Run infrastructure tests
        run: |
          SIBYL_MOCK_LLM=true uv run pytest apps/api/tests/test_agents_infra.py -v

  live-model-tests:
    runs-on: ubuntu-latest
    if: github.event_name == 'schedule' || contains(github.event.pull_request.labels.*.name, 'run-live-tests')
    steps:
      - uses: actions/checkout@v4
      - name: Run live model tests
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          uv run pytest apps/api/tests/live/ -v --live-models --cost-limit=2.0
```

## File Structure

```
apps/api/tests/
├── conftest.py                    # Shared fixtures + pytest config
├── test_agents.py                 # Unit tests (existing)
├── test_agents_infra.py           # Infrastructure integration tests
├── test_agents_worktree.py        # Worktree manager tests
├── test_agents_integration.py     # IntegrationManager tests
├── live/
│   ├── __init__.py
│   ├── conftest.py               # Live test fixtures
│   ├── test_agents_live.py       # Basic agent tests
│   ├── test_tools_live.py        # Tool usage tests
│   ├── test_approval_live.py     # Approval flow tests
│   └── test_checkpoint_live.py   # Checkpoint/resume tests
└── harness/
    ├── __init__.py
    ├── mocks.py                  # Mock implementations
    ├── helpers.py                # Test helpers
    └── fixtures.py               # Reusable fixtures
```

## Running Tests

**Live tests are excluded from normal test runs by default** via `--ignore=tests/live` in
pytest config. This ensures builds don't accidentally incur API costs.

```bash
# Normal test run (excludes live tests)
moon run api:test

# All agent tests (mocked)
SIBYL_MOCK_LLM=true moon run api:test -- -k agent

# Infrastructure only
SIBYL_MOCK_LLM=true uv run pytest apps/api/tests/test_agents_infra.py -v

# Live tests (requires API key) - must explicitly include live directory
uv run pytest apps/api/tests/live/ -v --live-models --no-cov

# Live tests with cost cap
uv run pytest apps/api/tests/live/ -v --live-models --cost-limit=0.50 --no-cov

# Specific scenario
uv run pytest apps/api/tests/live/test_agents_live.py::test_agent_basic_response -v --live-models --no-cov
```

**Important:** When running live tests, use `--no-cov` to avoid coverage overhead on API calls.
