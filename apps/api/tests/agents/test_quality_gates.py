"""Tests for quality gate runner framework."""

import pytest

from sibyl.agents.quality_gates import (
    CommandResult,
    GateResult,
    ProjectConfig,
    ProjectType,
    QualityGateRunner,
    create_gate_runner,
)
from sibyl_core.models import QualityGateType


@pytest.fixture
def temp_worktree(tmp_path):
    """Create a temporary worktree directory."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return worktree


@pytest.fixture
def python_project(temp_worktree):
    """Create a Python project structure."""
    (temp_worktree / "pyproject.toml").write_text(
        """
[project]
name = "test-project"
version = "0.1.0"
"""
    )
    (temp_worktree / "src").mkdir()
    (temp_worktree / "src" / "main.py").write_text("def hello(): pass")
    return temp_worktree


@pytest.fixture
def typescript_project(temp_worktree):
    """Create a TypeScript project structure."""
    (temp_worktree / "package.json").write_text(
        """
{
    "name": "test-project",
    "scripts": {
        "lint": "eslint .",
        "test": "jest"
    }
}
"""
    )
    (temp_worktree / "src").mkdir()
    (temp_worktree / "src" / "index.ts").write_text("export const hello = () => {};")
    return temp_worktree


class TestCommandResult:
    """Tests for CommandResult dataclass."""

    def test_success_true_on_zero_return(self):
        """Success is True when return code is 0."""
        result = CommandResult(command="test", return_code=0, stdout="ok", stderr="")
        assert result.success is True

    def test_success_false_on_nonzero_return(self):
        """Success is False when return code is non-zero."""
        result = CommandResult(command="test", return_code=1, stdout="", stderr="error")
        assert result.success is False

    def test_output_combines_stdout_stderr(self):
        """Output combines stdout and stderr."""
        result = CommandResult(
            command="test", return_code=0, stdout="stdout msg", stderr="stderr msg"
        )
        assert "stdout msg" in result.output
        assert "stderr msg" in result.output


class TestGateResult:
    """Tests for GateResult dataclass."""

    def test_defaults(self):
        """GateResult has sensible defaults."""
        result = GateResult(
            gate_type=QualityGateType.LINT,
            passed=True,
            output="All good",
        )
        assert result.errors == []
        assert result.warnings == []
        assert result.metrics == {}
        assert result.duration_ms == 0.0


class TestQualityGateRunnerDetection:
    """Tests for project type detection."""

    @pytest.mark.asyncio
    async def test_detect_python_project(self, python_project):
        """Detects Python project from pyproject.toml."""
        runner = QualityGateRunner(python_project)
        config = await runner.detect_project()

        assert config.project_type == ProjectType.PYTHON
        assert config.has_pyproject is True

    @pytest.mark.asyncio
    async def test_detect_typescript_project(self, typescript_project):
        """Detects TypeScript project from package.json."""
        runner = QualityGateRunner(typescript_project)
        config = await runner.detect_project()

        assert config.project_type == ProjectType.TYPESCRIPT
        assert config.has_package_json is True

    @pytest.mark.asyncio
    async def test_detect_unknown_project(self, temp_worktree):
        """Returns UNKNOWN for empty directory."""
        runner = QualityGateRunner(temp_worktree)
        config = await runner.detect_project()

        assert config.project_type == ProjectType.UNKNOWN

    @pytest.mark.asyncio
    async def test_caches_config(self, python_project):
        """Config is cached after first detection."""
        runner = QualityGateRunner(python_project)
        config1 = await runner.detect_project()
        config2 = await runner.detect_project()

        assert config1 is config2


class TestQualityGateRunnerCommands:
    """Tests for command execution."""

    @pytest.mark.asyncio
    async def test_run_command_success(self, temp_worktree):
        """Run command returns success result."""
        runner = QualityGateRunner(temp_worktree)
        result = await runner.run_command("echo hello")

        assert result.success is True
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_run_command_failure(self, temp_worktree):
        """Run command returns failure for bad command."""
        runner = QualityGateRunner(temp_worktree)
        result = await runner.run_command("exit 1")

        assert result.success is False
        assert result.return_code == 1

    @pytest.mark.asyncio
    async def test_run_command_in_worktree(self, temp_worktree):
        """Command runs in worktree directory."""
        runner = QualityGateRunner(temp_worktree)
        result = await runner.run_command("pwd")

        assert str(temp_worktree) in result.stdout

    @pytest.mark.asyncio
    async def test_truncate_output(self, temp_worktree):
        """Long output is truncated."""
        runner = QualityGateRunner(temp_worktree)
        runner.MAX_OUTPUT_LINES = 5

        # Generate output with more than 5 lines
        result = await runner.run_command("seq 1 100")

        assert "truncated" in result.stdout.lower()


class TestQualityGateRunnerLint:
    """Tests for lint gate."""

    @pytest.mark.asyncio
    async def test_lint_with_no_command(self, temp_worktree):
        """Lint passes when no command configured."""
        runner = QualityGateRunner(temp_worktree)
        result = await runner.run_lint()

        assert result.passed is True
        assert "no lint command" in result.output.lower()

    @pytest.mark.asyncio
    async def test_lint_runs_configured_command(self, python_project):
        """Lint runs the configured command."""
        runner = QualityGateRunner(python_project)
        runner._config = ProjectConfig(
            project_type=ProjectType.PYTHON,
            root_dir=python_project,
            lint_command="echo lint_output",
        )

        result = await runner.run_lint()

        assert "lint_output" in result.output

    @pytest.mark.asyncio
    async def test_lint_parses_python_errors(self, python_project):
        """Lint parses Python error format."""
        runner = QualityGateRunner(python_project)

        errors = runner._parse_lint_errors(
            "src/main.py:10:5: E501 line too long\nsrc/main.py:20:1: F401 unused import",
            ProjectType.PYTHON,
        )

        assert len(errors) == 2
        assert "E501" in errors[0]


class TestQualityGateRunnerTypecheck:
    """Tests for typecheck gate."""

    @pytest.mark.asyncio
    async def test_typecheck_with_no_command(self, temp_worktree):
        """Typecheck passes when no command configured."""
        runner = QualityGateRunner(temp_worktree)
        result = await runner.run_typecheck()

        assert result.passed is True
        assert "no typecheck command" in result.output.lower()

    @pytest.mark.asyncio
    async def test_typecheck_parses_mypy_errors(self, python_project):
        """Typecheck parses mypy error format."""
        runner = QualityGateRunner(python_project)

        errors = runner._parse_typecheck_errors(
            "src/main.py:10: error: Incompatible types",
            ProjectType.PYTHON,
        )

        assert len(errors) == 1
        assert "Incompatible types" in errors[0]


class TestQualityGateRunnerTest:
    """Tests for test gate."""

    @pytest.mark.asyncio
    async def test_test_with_no_command(self, temp_worktree):
        """Test passes when no command configured."""
        runner = QualityGateRunner(temp_worktree)
        result = await runner.run_test()

        assert result.passed is True
        assert "no test command" in result.output.lower()

    @pytest.mark.asyncio
    async def test_test_parses_pytest_metrics(self, python_project):
        """Test parses pytest output metrics."""
        runner = QualityGateRunner(python_project)

        metrics = runner._parse_test_metrics(
            "====== 10 passed, 2 failed in 1.23s ======",
            ProjectType.PYTHON,
        )

        assert metrics.get("passed") == 10
        assert metrics.get("failed") == 2
        assert metrics.get("duration_s") == 1.23


class TestQualityGateRunnerSecurity:
    """Tests for security gate."""

    @pytest.mark.asyncio
    async def test_security_with_no_command(self, temp_worktree):
        """Security passes when no command configured."""
        runner = QualityGateRunner(temp_worktree)
        result = await runner.run_security()

        assert result.passed is True
        assert "no security scanner" in result.output.lower()

    @pytest.mark.asyncio
    async def test_security_parses_bandit_findings(self, python_project):
        """Security parses bandit findings."""
        runner = QualityGateRunner(python_project)

        errors, warnings = runner._parse_security_findings(
            """
            Issue: [B105] hardcoded_password
            Severity: High
            Issue: [B110] try_except_pass
            Severity: Low
            """,
            ProjectType.PYTHON,
        )

        assert len(errors) == 1
        assert len(warnings) == 1


class TestQualityGateRunnerRunAll:
    """Tests for run_all_gates method."""

    @pytest.mark.asyncio
    async def test_run_all_gates(self, temp_worktree):
        """Run all gates executes multiple gates."""
        runner = QualityGateRunner(temp_worktree)

        results = await runner.run_all_gates(
            [
                QualityGateType.LINT,
                QualityGateType.TYPECHECK,
                QualityGateType.TEST,
            ]
        )

        assert len(results) == 3
        assert results[0].gate_type == QualityGateType.LINT
        assert results[1].gate_type == QualityGateType.TYPECHECK
        assert results[2].gate_type == QualityGateType.TEST

    @pytest.mark.asyncio
    async def test_run_all_skips_human_review(self, temp_worktree):
        """Human review is skipped in automated gates."""
        runner = QualityGateRunner(temp_worktree)

        results = await runner.run_all_gates(
            [
                QualityGateType.LINT,
                QualityGateType.HUMAN_REVIEW,
            ]
        )

        # Only lint should have a result
        assert len(results) == 1
        assert results[0].gate_type == QualityGateType.LINT


class TestCreateGateRunner:
    """Tests for factory function."""

    @pytest.mark.asyncio
    async def test_create_returns_configured_runner(self, python_project):
        """Factory creates and configures runner."""
        runner = await create_gate_runner(python_project)

        assert isinstance(runner, QualityGateRunner)
        assert runner._config is not None
        assert runner._config.project_type == ProjectType.PYTHON
