"""Quality gate runner framework for TaskOrchestrator.

Executes lint, typecheck, test, and security scan commands in worktrees.
Detects project types and runs appropriate commands.
"""

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

from sibyl_core.models import QualityGateType

log = structlog.get_logger()


class ProjectType(str, Enum):
    """Detected project types for quality gate configuration."""

    PYTHON = "python"
    TYPESCRIPT = "typescript"
    RUST = "rust"
    GO = "go"
    UNKNOWN = "unknown"


@dataclass
class CommandResult:
    """Result of running a shell command."""

    command: str
    return_code: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        """Whether command succeeded (return code 0)."""
        return self.return_code == 0

    @property
    def output(self) -> str:
        """Combined stdout and stderr."""
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append(self.stderr.strip())
        return "\n".join(parts)


@dataclass
class GateResult:
    """Result from running a quality gate."""

    gate_type: QualityGateType
    passed: bool
    output: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0


@dataclass
class ProjectConfig:
    """Detected project configuration for quality gates."""

    project_type: ProjectType
    root_dir: Path
    has_pyproject: bool = False
    has_package_json: bool = False
    has_cargo_toml: bool = False
    has_go_mod: bool = False
    # Lint
    lint_command: str | None = None
    lint_fix_command: str | None = None
    # Typecheck
    typecheck_command: str | None = None
    # Test
    test_command: str | None = None
    # Security
    security_command: str | None = None


class QualityGateRunner:
    """Runs quality gates in a worktree directory.

    Detects project type and executes appropriate commands for each gate.
    """

    # Command execution limits
    MAX_OUTPUT_LINES = 100
    COMMAND_TIMEOUT = 300  # 5 minutes

    def __init__(self, worktree_path: str | Path):
        """Initialize runner for a specific worktree.

        Args:
            worktree_path: Path to the git worktree directory
        """
        self.worktree_path = Path(worktree_path)
        self._config: ProjectConfig | None = None

    async def detect_project(self) -> ProjectConfig:
        """Detect project type and available tools.

        Returns:
            ProjectConfig with detected settings
        """
        if self._config is not None:
            return self._config

        root = self.worktree_path

        config = ProjectConfig(
            project_type=ProjectType.UNKNOWN,
            root_dir=root,
        )

        # Check for project markers
        config.has_pyproject = (root / "pyproject.toml").exists()
        config.has_package_json = (root / "package.json").exists()
        config.has_cargo_toml = (root / "Cargo.toml").exists()
        config.has_go_mod = (root / "go.mod").exists()

        # Determine primary project type
        if config.has_pyproject:
            config.project_type = ProjectType.PYTHON
            await self._configure_python(config)
        elif config.has_package_json:
            config.project_type = ProjectType.TYPESCRIPT
            await self._configure_typescript(config)
        elif config.has_cargo_toml:
            config.project_type = ProjectType.RUST
            await self._configure_rust(config)
        elif config.has_go_mod:
            config.project_type = ProjectType.GO
            await self._configure_go(config)

        self._config = config
        log.info(
            "Detected project config",
            project_type=config.project_type.value,
            lint=config.lint_command,
            typecheck=config.typecheck_command,
            test=config.test_command,
        )

        return config

    async def _configure_python(self, config: ProjectConfig) -> None:
        """Configure commands for Python project."""
        # Check for ruff (modern) or flake8 (legacy)
        if await self._command_available("ruff"):
            config.lint_command = "ruff check ."
            config.lint_fix_command = "ruff check . --fix"
        elif await self._command_available("flake8"):
            config.lint_command = "flake8 ."

        # Check for mypy or pyright
        if await self._command_available("mypy"):
            config.typecheck_command = "mypy ."
        elif await self._command_available("pyright"):
            config.typecheck_command = "pyright ."

        # Check for pytest or unittest
        if await self._command_available("pytest"):
            config.test_command = "pytest -v"
        else:
            config.test_command = "python -m unittest discover -v"

        # Security scanning
        if await self._command_available("bandit"):
            config.security_command = "bandit -r . -f json"

    async def _configure_typescript(self, config: ProjectConfig) -> None:
        """Configure commands for TypeScript/JavaScript project."""
        # Check for package manager
        has_pnpm = (config.root_dir / "pnpm-lock.yaml").exists()
        has_yarn = (config.root_dir / "yarn.lock").exists()
        pkg_runner = "pnpm" if has_pnpm else "yarn" if has_yarn else "npm"

        # Read package.json for scripts
        pkg_json = config.root_dir / "package.json"
        if pkg_json.exists():
            import json

            pkg = json.loads(pkg_json.read_text())
            scripts = pkg.get("scripts", {})

            if "lint" in scripts:
                config.lint_command = f"{pkg_runner} run lint"
            elif await self._command_available("eslint"):
                config.lint_command = "eslint . --ext .ts,.tsx,.js,.jsx"
            elif await self._command_available("biome"):
                config.lint_command = "biome check ."
                config.lint_fix_command = "biome check . --write"

            if "typecheck" in scripts:
                config.typecheck_command = f"{pkg_runner} run typecheck"
            elif "type-check" in scripts:
                config.typecheck_command = f"{pkg_runner} run type-check"
            elif await self._command_available("tsc"):
                config.typecheck_command = "tsc --noEmit"

            if "test" in scripts:
                config.test_command = f"{pkg_runner} run test"
            elif await self._command_available("vitest"):
                config.test_command = "vitest run"
            elif await self._command_available("jest"):
                config.test_command = "jest"

    async def _configure_rust(self, config: ProjectConfig) -> None:
        """Configure commands for Rust project."""
        config.lint_command = "cargo clippy -- -D warnings"
        # Rust type checking is part of compilation
        config.typecheck_command = "cargo check"
        config.test_command = "cargo test"
        # Security
        if await self._command_available("cargo-audit"):
            config.security_command = "cargo audit"

    async def _configure_go(self, config: ProjectConfig) -> None:
        """Configure commands for Go project."""
        if await self._command_available("golangci-lint"):
            config.lint_command = "golangci-lint run"
        else:
            config.lint_command = "go vet ./..."
        # Go is statically typed, build checks types
        config.typecheck_command = "go build ./..."
        config.test_command = "go test -v ./..."
        # Security
        if await self._command_available("gosec"):
            config.security_command = "gosec ./..."

    async def _command_available(self, cmd: str) -> bool:
        """Check if a command is available in the environment."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "which",
                cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.worktree_path,
            )
            await proc.wait()
            return proc.returncode == 0
        except Exception:
            return False

    async def run_command(self, command: str) -> CommandResult:
        """Execute a shell command in the worktree.

        Args:
            command: Shell command to run

        Returns:
            CommandResult with output and status
        """
        log.debug("Running command", command=command, cwd=str(self.worktree_path))

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.worktree_path,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.COMMAND_TIMEOUT,
            )

            return CommandResult(
                command=command,
                return_code=proc.returncode or 0,
                stdout=self._truncate_output(stdout.decode("utf-8", errors="replace")),
                stderr=self._truncate_output(stderr.decode("utf-8", errors="replace")),
            )

        except TimeoutError:
            log.warning("Command timed out", command=command)
            return CommandResult(
                command=command,
                return_code=-1,
                stdout="",
                stderr=f"Command timed out after {self.COMMAND_TIMEOUT}s",
            )

        except Exception as e:
            log.exception("Command execution failed", command=command)
            return CommandResult(
                command=command,
                return_code=-1,
                stdout="",
                stderr=f"Execution error: {e}",
            )

    def _truncate_output(self, output: str) -> str:
        """Truncate output to reasonable length."""
        lines = output.split("\n")
        if len(lines) > self.MAX_OUTPUT_LINES:
            truncated = lines[: self.MAX_OUTPUT_LINES]
            truncated.append(f"... (truncated {len(lines) - self.MAX_OUTPUT_LINES} more lines)")
            return "\n".join(truncated)
        return output

    async def run_lint(self) -> GateResult:
        """Run linting gate.

        Returns:
            GateResult with lint status
        """
        import time

        start = time.perf_counter()
        config = await self.detect_project()

        if not config.lint_command:
            return GateResult(
                gate_type=QualityGateType.LINT,
                passed=True,
                output="No lint command configured for this project type",
            )

        result = await self.run_command(config.lint_command)
        duration = (time.perf_counter() - start) * 1000

        errors = self._parse_lint_errors(result.output, config.project_type)

        return GateResult(
            gate_type=QualityGateType.LINT,
            passed=result.success,
            output=result.output,
            errors=errors,
            duration_ms=duration,
        )

    def _parse_lint_errors(self, output: str, project_type: ProjectType) -> list[str]:
        """Parse lint output for specific errors."""
        lines = output.split("\n")

        if project_type == ProjectType.PYTHON:
            # Ruff format: path:line:col: CODE message
            errors = [line.strip() for line in lines if re.match(r"^.+:\d+:\d+:", line)]
        elif project_type == ProjectType.TYPESCRIPT:
            # ESLint format: path:line:col: message (rule)
            errors = [line.strip() for line in lines if re.match(r"^\s+\d+:\d+", line)]
        else:
            errors = []

        return errors[:50]  # Limit to 50 errors

    async def run_typecheck(self) -> GateResult:
        """Run type checking gate.

        Returns:
            GateResult with typecheck status
        """
        import time

        start = time.perf_counter()
        config = await self.detect_project()

        if not config.typecheck_command:
            return GateResult(
                gate_type=QualityGateType.TYPECHECK,
                passed=True,
                output="No typecheck command configured for this project type",
            )

        result = await self.run_command(config.typecheck_command)
        duration = (time.perf_counter() - start) * 1000

        errors = self._parse_typecheck_errors(result.output, config.project_type)

        return GateResult(
            gate_type=QualityGateType.TYPECHECK,
            passed=result.success,
            output=result.output,
            errors=errors,
            duration_ms=duration,
        )

    def _parse_typecheck_errors(self, output: str, project_type: ProjectType) -> list[str]:
        """Parse typecheck output for specific errors."""
        lines = output.split("\n")

        if project_type == ProjectType.PYTHON:
            # Mypy format: path:line: error: message
            errors = [line.strip() for line in lines if ": error:" in line]
        elif project_type == ProjectType.TYPESCRIPT:
            # tsc format: path(line,col): error TS1234: message
            errors = [line.strip() for line in lines if "error TS" in line]
        else:
            errors = []

        return errors[:50]

    async def run_test(self) -> GateResult:
        """Run test gate.

        Returns:
            GateResult with test status
        """
        import time

        start = time.perf_counter()
        config = await self.detect_project()

        if not config.test_command:
            return GateResult(
                gate_type=QualityGateType.TEST,
                passed=True,
                output="No test command configured for this project type",
            )

        result = await self.run_command(config.test_command)
        duration = (time.perf_counter() - start) * 1000

        # Parse test metrics
        metrics = self._parse_test_metrics(result.output, config.project_type)
        errors = self._parse_test_failures(result.output, config.project_type)

        return GateResult(
            gate_type=QualityGateType.TEST,
            passed=result.success,
            output=result.output,
            errors=errors,
            metrics=metrics,
            duration_ms=duration,
        )

    def _parse_test_metrics(self, output: str, project_type: ProjectType) -> dict[str, Any]:
        """Parse test output for metrics."""
        metrics: dict[str, Any] = {}

        if project_type == ProjectType.PYTHON:
            # Pytest format: X passed, Y failed, Z errors in N.NNs
            match = re.search(r"(\d+) passed", output)
            if match:
                metrics["passed"] = int(match.group(1))
            match = re.search(r"(\d+) failed", output)
            if match:
                metrics["failed"] = int(match.group(1))
            match = re.search(r"in ([\d.]+)s", output)
            if match:
                metrics["duration_s"] = float(match.group(1))

        return metrics

    def _parse_test_failures(self, output: str, project_type: ProjectType) -> list[str]:
        """Parse test output for failure details."""
        if project_type == ProjectType.PYTHON:
            # Pytest FAILED lines
            errors = [line.strip() for line in output.split("\n") if line.strip().startswith("FAILED")]
        else:
            errors = []

        return errors[:20]

    async def run_security(self) -> GateResult:
        """Run security scan gate.

        Returns:
            GateResult with security status
        """
        import time

        start = time.perf_counter()
        config = await self.detect_project()

        if not config.security_command:
            return GateResult(
                gate_type=QualityGateType.SECURITY_SCAN,
                passed=True,
                output="No security scanner configured for this project type",
            )

        result = await self.run_command(config.security_command)
        duration = (time.perf_counter() - start) * 1000

        # Security scanners often use non-zero exit for findings
        # Parse output to determine severity
        errors, warnings = self._parse_security_findings(result.output, config.project_type)

        # Only fail on high/critical severity
        passed = len(errors) == 0

        return GateResult(
            gate_type=QualityGateType.SECURITY_SCAN,
            passed=passed,
            output=result.output,
            errors=errors,
            warnings=warnings,
            duration_ms=duration,
        )

    def _parse_security_findings(
        self, output: str, project_type: ProjectType
    ) -> tuple[list[str], list[str]]:
        """Parse security scan output.

        Returns:
            Tuple of (high/critical errors, low/medium warnings)
        """
        errors: list[str] = []
        warnings: list[str] = []

        if project_type == ProjectType.PYTHON:
            # Bandit severity levels
            for line in output.split("\n"):
                if "Severity: High" in line or "Severity: Critical" in line:
                    errors.append(line.strip())
                elif "Severity: Medium" in line or "Severity: Low" in line:
                    warnings.append(line.strip())

        return errors[:20], warnings[:20]

    async def run_all_gates(
        self,
        gates: list[QualityGateType],
    ) -> list[GateResult]:
        """Run multiple quality gates.

        Args:
            gates: List of gates to run

        Returns:
            List of GateResult for each gate
        """
        results = []

        for gate in gates:
            if gate == QualityGateType.LINT:
                results.append(await self.run_lint())
            elif gate == QualityGateType.TYPECHECK:
                results.append(await self.run_typecheck())
            elif gate == QualityGateType.TEST:
                results.append(await self.run_test())
            elif gate == QualityGateType.SECURITY_SCAN:
                results.append(await self.run_security())
            elif gate == QualityGateType.HUMAN_REVIEW:
                # Human review is handled separately
                pass
            elif gate == QualityGateType.AI_REVIEW:
                # AI review needs separate implementation
                results.append(
                    GateResult(
                        gate_type=QualityGateType.AI_REVIEW,
                        passed=True,
                        output="AI review not implemented",
                    )
                )

        return results


async def create_gate_runner(worktree_path: str | Path) -> QualityGateRunner:
    """Factory function for creating QualityGateRunner instances.

    Args:
        worktree_path: Path to the worktree

    Returns:
        Configured QualityGateRunner
    """
    runner = QualityGateRunner(worktree_path)
    await runner.detect_project()
    return runner
