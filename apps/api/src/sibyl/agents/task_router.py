"""Smart task routing to distributed runners.

Routes tasks to optimal runners based on:
- Project affinity (warm worktrees = faster startup)
- Capability match (required tools/environments)
- Current load (available capacity)
- Health (recent heartbeat)

Scoring formula:
    score = affinity_score + capability_score + load_score + health_penalty

Where:
    - affinity_score: 50 points for warm worktree, 0 otherwise
    - capability_score: 30 points if all required capabilities present
    - load_score: 0-20 points based on available capacity
    - health_penalty: -100 if runner missed recent heartbeat
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from sibyl.db import Runner, RunnerProject, RunnerStatus

log = structlog.get_logger()

# Scoring weights
AFFINITY_SCORE = 50  # Warm worktree bonus
CAPABILITY_SCORE = 30  # Full capability match bonus
MAX_LOAD_SCORE = 20  # Maximum points for available capacity
HEALTH_PENALTY = -100  # Penalty for stale heartbeat
HEARTBEAT_STALE_SECONDS = 60  # Consider stale if no heartbeat in this time


@dataclass
class RunnerScore:
    """Score breakdown for a runner candidate."""

    runner_id: UUID
    runner_name: str
    total_score: float
    affinity_score: float = 0.0
    capability_score: float = 0.0
    load_score: float = 0.0
    health_penalty: float = 0.0
    available_slots: int = 0
    has_warm_worktree: bool = False
    missing_capabilities: list[str] | None = None

    def __lt__(self, other: "RunnerScore") -> bool:
        """Higher scores should sort first."""
        return self.total_score > other.total_score


@dataclass
class RoutingResult:
    """Result of task routing decision."""

    success: bool
    runner_id: UUID | None = None
    runner_name: str | None = None
    score: RunnerScore | None = None
    all_scores: list[RunnerScore] | None = None
    reason: str | None = None


class TaskRouter:
    """Routes tasks to optimal runners based on scoring.

    Usage:
        router = TaskRouter(session, org_id)

        # Route a task
        result = await router.route_task(
            project_id=project_id,
            required_capabilities=["docker", "gpu"],
        )

        if result.success:
            # Assign to result.runner_id
            pass
        else:
            # Handle unavailability
            print(result.reason)
    """

    def __init__(self, session: AsyncSession, org_id: UUID) -> None:
        """Initialize the task router.

        Args:
            session: SQLAlchemy async session
            org_id: Organization context for multi-tenancy
        """
        self.session = session
        self.org_id = org_id

    async def route_task(
        self,
        *,
        project_id: UUID | None = None,
        required_capabilities: list[str] | None = None,
        preferred_runner_id: UUID | None = None,
        exclude_runners: list[UUID] | None = None,
        task_context: dict[str, Any] | None = None,
    ) -> RoutingResult:
        """Route a task to the optimal runner.

        Args:
            project_id: Project ID for affinity scoring
            required_capabilities: Capabilities the runner must have
            preferred_runner_id: Prefer this runner if available
            exclude_runners: Runner IDs to exclude from consideration
            task_context: Additional context for routing decisions

        Returns:
            RoutingResult with selected runner or failure reason
        """
        required_capabilities = required_capabilities or []
        exclude_runners = exclude_runners or []

        # Get all online/available runners for this org
        runners = await self._get_available_runners(exclude_runners)

        if not runners:
            return RoutingResult(
                success=False,
                reason="No runners available. All runners are offline or excluded.",
            )

        # Get warm worktrees for project affinity
        warm_worktrees: dict[UUID, RunnerProject] = {}
        if project_id:
            warm_worktrees = await self._get_warm_worktrees(project_id)

        # Score all runners
        scores: list[RunnerScore] = []
        for runner in runners:
            score = self._score_runner(
                runner=runner,
                project_id=project_id,
                required_capabilities=required_capabilities,
                warm_worktrees=warm_worktrees,
                preferred_runner_id=preferred_runner_id,
            )
            scores.append(score)

        # Sort by score (highest first)
        scores.sort()

        # Filter out runners with negative scores (health issues, missing capabilities)
        viable_scores = [s for s in scores if s.total_score >= 0]

        if not viable_scores:
            # Log why runners were rejected
            rejection_reasons = []
            for s in scores:
                if s.missing_capabilities:
                    rejection_reasons.append(
                        f"{s.runner_name}: missing {s.missing_capabilities}"
                    )
                elif s.health_penalty < 0:
                    rejection_reasons.append(f"{s.runner_name}: unhealthy (stale heartbeat)")
                elif s.available_slots == 0:
                    rejection_reasons.append(f"{s.runner_name}: at capacity")

            return RoutingResult(
                success=False,
                all_scores=scores,
                reason=f"No suitable runners: {'; '.join(rejection_reasons)}",
            )

        # Select the best runner
        best = viable_scores[0]

        log.info(
            "task_routed",
            runner_id=str(best.runner_id),
            runner_name=best.runner_name,
            score=best.total_score,
            has_warm_worktree=best.has_warm_worktree,
            available_slots=best.available_slots,
            org_id=str(self.org_id),
        )

        return RoutingResult(
            success=True,
            runner_id=best.runner_id,
            runner_name=best.runner_name,
            score=best,
            all_scores=scores,
        )

    async def get_runner_scores(
        self,
        project_id: UUID | None = None,
        required_capabilities: list[str] | None = None,
    ) -> list[RunnerScore]:
        """Get scores for all runners without selecting one.

        Useful for debugging and UI display of runner availability.
        """
        required_capabilities = required_capabilities or []

        runners = await self._get_available_runners([])
        warm_worktrees: dict[UUID, RunnerProject] = {}
        if project_id:
            warm_worktrees = await self._get_warm_worktrees(project_id)

        scores = []
        for runner in runners:
            score = self._score_runner(
                runner=runner,
                project_id=project_id,
                required_capabilities=required_capabilities,
                warm_worktrees=warm_worktrees,
            )
            scores.append(score)

        scores.sort()
        return scores

    async def _get_available_runners(
        self, exclude_runners: list[UUID]
    ) -> list[Runner]:
        """Get runners that could potentially accept tasks."""
        query = (
            select(Runner)
            .where(col(Runner.organization_id) == self.org_id)
            .where(col(Runner.status).in_([RunnerStatus.ONLINE, RunnerStatus.BUSY]))
        )

        if exclude_runners:
            query = query.where(col(Runner.id).not_in(exclude_runners))

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def _get_warm_worktrees(
        self, project_id: UUID
    ) -> dict[UUID, RunnerProject]:
        """Get runners with warm worktrees for a project."""
        result = await self.session.execute(
            select(RunnerProject).where(col(RunnerProject.project_id) == project_id)
        )
        return {rp.runner_id: rp for rp in result.scalars().all()}

    def _score_runner(
        self,
        runner: Runner,
        project_id: UUID | None,
        required_capabilities: list[str],
        warm_worktrees: dict[UUID, RunnerProject],
        preferred_runner_id: UUID | None = None,
    ) -> RunnerScore:
        """Calculate score for a single runner."""
        score = RunnerScore(
            runner_id=runner.id,
            runner_name=runner.name,
            total_score=0,
        )

        # 1. Project affinity (warm worktree)
        if project_id and runner.id in warm_worktrees:
            score.affinity_score = AFFINITY_SCORE
            score.has_warm_worktree = True

        # 2. Capability match
        runner_caps = set(runner.capabilities or [])
        required_caps = set(required_capabilities)

        if required_caps:
            missing = required_caps - runner_caps
            if missing:
                score.missing_capabilities = list(missing)
                # Negative score means this runner is ineligible
                score.capability_score = -100
            else:
                score.capability_score = CAPABILITY_SCORE

        # 3. Load score (more available capacity = higher score)
        available = runner.max_concurrent_agents - runner.current_agent_count
        score.available_slots = available

        if available <= 0:
            # No capacity - runner is at max
            score.load_score = -50  # Strongly discourage
        else:
            # Scale load score: more capacity = higher score
            capacity_ratio = available / runner.max_concurrent_agents
            score.load_score = MAX_LOAD_SCORE * capacity_ratio

        # 4. Health check (recent heartbeat)
        if runner.last_heartbeat:
            age = (datetime.now(UTC) - runner.last_heartbeat.replace(tzinfo=UTC)).total_seconds()
            if age > HEARTBEAT_STALE_SECONDS:
                score.health_penalty = HEALTH_PENALTY

        # 5. Preference bonus
        if preferred_runner_id and runner.id == preferred_runner_id:
            score.affinity_score += 25  # Bonus for preferred runner

        # Calculate total
        score.total_score = (
            score.affinity_score
            + score.capability_score
            + score.load_score
            + score.health_penalty
        )

        return score


# =============================================================================
# Convenience functions
# =============================================================================


async def route_task_to_runner(
    session: AsyncSession,
    org_id: UUID,
    project_id: UUID | None = None,
    required_capabilities: list[str] | None = None,
) -> RoutingResult:
    """Convenience function to route a task.

    Args:
        session: Database session
        org_id: Organization ID
        project_id: Optional project for affinity scoring
        required_capabilities: Required runner capabilities

    Returns:
        RoutingResult with selected runner or failure reason
    """
    router = TaskRouter(session, org_id)
    return await router.route_task(
        project_id=project_id,
        required_capabilities=required_capabilities,
    )
