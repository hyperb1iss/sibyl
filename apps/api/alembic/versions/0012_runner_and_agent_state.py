"""Add runners, agent_states, and orchestrator_states tables.

Supports distributed runner architecture with operational state separation:
- Runner identity in graph, state in Postgres
- Agent identity in graph, state in Postgres
- Orchestrator identity in graph, state in Postgres

Revision ID: 0012_runner_and_agent_state
Revises: 0011_backup_management
Create Date: 2026-01-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012_runner_and_agent_state"
down_revision: str | None = "0011_backup_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ==========================================================================
    # Runners - Distributed agent execution hosts
    # ==========================================================================
    op.create_table(
        "runners",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organization_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Identity (links to graph entity)
        sa.Column("graph_runner_id", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hostname", sa.String(255), nullable=False),
        # Capabilities
        sa.Column("capabilities", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("max_concurrent_agents", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("current_agent_count", sa.Integer(), nullable=False, server_default="0"),
        # Status (using VARCHAR to avoid async enum issues)
        sa.Column("status", sa.String(32), nullable=False, server_default="offline"),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=True),
        # Connection info
        sa.Column("websocket_session_id", sa.String(64), nullable=True),
        sa.Column("client_version", sa.String(32), nullable=True),
        # Timestamps
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_runners_organization_id", "runners", ["organization_id"])
    op.create_index("ix_runners_user_id", "runners", ["user_id"])
    op.create_index("ix_runners_graph_runner_id", "runners", ["graph_runner_id"])
    op.create_index("ix_runners_status", "runners", ["status"])
    op.create_index("ix_runners_last_heartbeat", "runners", ["last_heartbeat"])

    # ==========================================================================
    # Runner Projects - Warm worktree tracking for project affinity
    # ==========================================================================
    op.create_table(
        "runner_projects",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "runner_id",
            sa.UUID(),
            sa.ForeignKey("runners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Worktree info
        sa.Column("worktree_path", sa.String(1024), nullable=False),
        sa.Column("worktree_branch", sa.String(255), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        # Timestamps
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_runner_projects_runner_id", "runner_projects", ["runner_id"])
    op.create_index("ix_runner_projects_project_id", "runner_projects", ["project_id"])
    op.create_index(
        "ix_runner_projects_runner_project_unique",
        "runner_projects",
        ["runner_id", "project_id"],
        unique=True,
    )

    # ==========================================================================
    # Agent States - Operational state for agents (identity in graph)
    # ==========================================================================
    op.create_table(
        "agent_states",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organization_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Link to graph entity
        sa.Column("graph_agent_id", sa.String(64), nullable=False, unique=True),
        # Execution context
        sa.Column(
            "runner_id",
            sa.UUID(),
            sa.ForeignKey("runners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("orchestrator_id", sa.String(64), nullable=True),
        # Status
        sa.Column("status", sa.String(32), nullable=False, server_default="initializing"),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_activity", sa.String(512), nullable=True),
        # Metrics
        sa.Column("tokens_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        # Error tracking
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        # Timestamps
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_agent_states_organization_id", "agent_states", ["organization_id"])
    op.create_index("ix_agent_states_graph_agent_id", "agent_states", ["graph_agent_id"])
    op.create_index("ix_agent_states_runner_id", "agent_states", ["runner_id"])
    op.create_index("ix_agent_states_task_id", "agent_states", ["task_id"])
    op.create_index("ix_agent_states_orchestrator_id", "agent_states", ["orchestrator_id"])
    op.create_index("ix_agent_states_status", "agent_states", ["status"])
    op.create_index("ix_agent_states_last_heartbeat", "agent_states", ["last_heartbeat"])

    # ==========================================================================
    # Orchestrator States - Operational state for task orchestrators
    # ==========================================================================
    op.create_table(
        "orchestrator_states",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organization_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Link to graph entity
        sa.Column("graph_orchestrator_id", sa.String(64), nullable=False, unique=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Build loop state
        sa.Column("current_phase", sa.String(32), nullable=False, server_default="implement"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        # Quality gates
        sa.Column("gate_config", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        # Iteration tracking
        sa.Column("rework_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_rework_attempts", sa.Integer(), nullable=False, server_default="3"),
        # Current worker
        sa.Column("current_worker_id", sa.String(64), nullable=True),
        # Gate results
        sa.Column(
            "gate_results",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Review state
        sa.Column("review_feedback", sa.Text(), nullable=True),
        sa.Column(
            "human_reviewer_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Metrics
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        # Error tracking
        sa.Column("error_message", sa.Text(), nullable=True),
        # Timestamps
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_orchestrator_states_organization_id", "orchestrator_states", ["organization_id"])
    op.create_index("ix_orchestrator_states_graph_orchestrator_id", "orchestrator_states", ["graph_orchestrator_id"])
    op.create_index("ix_orchestrator_states_task_id", "orchestrator_states", ["task_id"])
    op.create_index("ix_orchestrator_states_project_id", "orchestrator_states", ["project_id"])
    op.create_index("ix_orchestrator_states_status", "orchestrator_states", ["status"])
    op.create_index("ix_orchestrator_states_current_worker_id", "orchestrator_states", ["current_worker_id"])


def downgrade() -> None:
    # Drop orchestrator_states
    op.drop_index("ix_orchestrator_states_current_worker_id")
    op.drop_index("ix_orchestrator_states_status")
    op.drop_index("ix_orchestrator_states_project_id")
    op.drop_index("ix_orchestrator_states_task_id")
    op.drop_index("ix_orchestrator_states_graph_orchestrator_id")
    op.drop_index("ix_orchestrator_states_organization_id")
    op.drop_table("orchestrator_states")

    # Drop agent_states
    op.drop_index("ix_agent_states_last_heartbeat")
    op.drop_index("ix_agent_states_status")
    op.drop_index("ix_agent_states_orchestrator_id")
    op.drop_index("ix_agent_states_task_id")
    op.drop_index("ix_agent_states_runner_id")
    op.drop_index("ix_agent_states_graph_agent_id")
    op.drop_index("ix_agent_states_organization_id")
    op.drop_table("agent_states")

    # Drop runner_projects
    op.drop_index("ix_runner_projects_runner_project_unique")
    op.drop_index("ix_runner_projects_project_id")
    op.drop_index("ix_runner_projects_runner_id")
    op.drop_table("runner_projects")

    # Drop runners
    op.drop_index("ix_runners_last_heartbeat")
    op.drop_index("ix_runners_status")
    op.drop_index("ix_runners_graph_runner_id")
    op.drop_index("ix_runners_user_id")
    op.drop_index("ix_runners_organization_id")
    op.drop_table("runners")
