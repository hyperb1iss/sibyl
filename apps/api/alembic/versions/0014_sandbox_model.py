"""Add sandbox models and runner sandbox linkage.

Revision ID: 0014_sandbox_model
Revises: 0013_inter_agent_messages
Create Date: 2026-02-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014_sandbox_model"
down_revision: str | None = "0013_inter_agent_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create sandbox tables and extend runners for sandbox execution."""
    op.create_table(
        "sandboxes",
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
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False, server_default="sandbox"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "image",
            sa.String(512),
            nullable=False,
            server_default="ghcr.io/hyperb1iss/sibyl-sandbox:latest",
        ),
        sa.Column("namespace", sa.String(255), nullable=True),
        sa.Column("pod_name", sa.String(255), nullable=True),
        sa.Column("cpu_request", sa.String(32), nullable=False, server_default="250m"),
        sa.Column("cpu_limit", sa.String(32), nullable=False, server_default="1000m"),
        sa.Column("memory_request", sa.String(32), nullable=False, server_default="512Mi"),
        sa.Column("memory_limit", sa.String(32), nullable=False, server_default="2Gi"),
        sa.Column(
            "ephemeral_storage_request",
            sa.String(32),
            nullable=False,
            server_default="1Gi",
        ),
        sa.Column(
            "ephemeral_storage_limit",
            sa.String(32),
            nullable=False,
            server_default="4Gi",
        ),
        sa.Column("idle_ttl_seconds", sa.Integer(), nullable=False, server_default="1800"),
        sa.Column("max_lifetime_seconds", sa.Integer(), nullable=False, server_default="14400"),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "runner_id",
            sa.UUID(),
            sa.ForeignKey("runners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "context",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_sandboxes_organization_id", "sandboxes", ["organization_id"])
    op.create_index("ix_sandboxes_user_id", "sandboxes", ["user_id"])
    op.create_index("ix_sandboxes_name", "sandboxes", ["name"])
    op.create_index("ix_sandboxes_status", "sandboxes", ["status"])
    op.create_index("ix_sandboxes_namespace", "sandboxes", ["namespace"])
    op.create_index("ix_sandboxes_pod_name", "sandboxes", ["pod_name"])
    op.create_index("ix_sandboxes_last_heartbeat", "sandboxes", ["last_heartbeat"])
    op.create_index("ix_sandboxes_org_status", "sandboxes", ["organization_id", "status"])
    op.create_index("ix_sandboxes_runner_id", "sandboxes", ["runner_id"])
    op.create_index(
        "ix_sandboxes_org_user_unique",
        "sandboxes",
        ["organization_id", "user_id"],
        unique=True,
    )

    op.create_table(
        "sandbox_tasks",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "sandbox_id",
            sa.UUID(),
            sa.ForeignKey("sandboxes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runner_id",
            sa.UUID(),
            sa.ForeignKey("runners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("task_type", sa.String(64), nullable=False, server_default="agent_execution"),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("working_directory", sa.String(1024), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout_preview", sa.Text(), nullable=True),
        sa.Column("stderr_preview", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_dispatch_at", sa.DateTime(), nullable=True),
        sa.Column("acked_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "context",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_sandbox_tasks_sandbox_id", "sandbox_tasks", ["sandbox_id"])
    op.create_index("ix_sandbox_tasks_organization_id", "sandbox_tasks", ["organization_id"])
    op.create_index("ix_sandbox_tasks_runner_id", "sandbox_tasks", ["runner_id"])
    op.create_index("ix_sandbox_tasks_task_id", "sandbox_tasks", ["task_id"])
    op.create_index("ix_sandbox_tasks_task_type", "sandbox_tasks", ["task_type"])
    op.create_index("ix_sandbox_tasks_idempotency_key", "sandbox_tasks", ["idempotency_key"])
    op.create_index("ix_sandbox_tasks_status", "sandbox_tasks", ["status"])
    op.create_index(
        "ix_sandbox_tasks_sandbox_created",
        "sandbox_tasks",
        ["sandbox_id", "created_at"],
    )

    op.create_table(
        "user_ssh_keys",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("key_type", sa.String(32), nullable=False, server_default="ssh-ed25519"),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("comment", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "extra",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_user_ssh_keys_user_id", "user_ssh_keys", ["user_id"])
    op.create_index("ix_user_ssh_keys_fingerprint", "user_ssh_keys", ["fingerprint"])
    op.create_index(
        "ix_user_ssh_keys_user_fingerprint_unique",
        "user_ssh_keys",
        ["user_id", "fingerprint"],
        unique=True,
    )

    op.add_column("runners", sa.Column("sandbox_id", sa.UUID(), nullable=True))
    op.add_column(
        "runners",
        sa.Column("is_sandbox_runner", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_foreign_key(
        "fk_runners_sandbox_id_sandboxes",
        "runners",
        "sandboxes",
        ["sandbox_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_runners_sandbox_id", "runners", ["sandbox_id"])
    op.create_index("ix_runners_is_sandbox_runner", "runners", ["is_sandbox_runner"])


def downgrade() -> None:
    """Remove sandbox tables and runner sandbox fields."""
    op.drop_index("ix_runners_is_sandbox_runner", table_name="runners")
    op.drop_index("ix_runners_sandbox_id", table_name="runners")
    op.drop_constraint("fk_runners_sandbox_id_sandboxes", "runners", type_="foreignkey")
    op.drop_column("runners", "is_sandbox_runner")
    op.drop_column("runners", "sandbox_id")

    op.drop_index("ix_user_ssh_keys_user_fingerprint_unique", table_name="user_ssh_keys")
    op.drop_index("ix_user_ssh_keys_fingerprint", table_name="user_ssh_keys")
    op.drop_index("ix_user_ssh_keys_user_id", table_name="user_ssh_keys")
    op.drop_table("user_ssh_keys")

    op.drop_index("ix_sandbox_tasks_sandbox_created", table_name="sandbox_tasks")
    op.drop_index("ix_sandbox_tasks_status", table_name="sandbox_tasks")
    op.drop_index("ix_sandbox_tasks_idempotency_key", table_name="sandbox_tasks")
    op.drop_index("ix_sandbox_tasks_task_type", table_name="sandbox_tasks")
    op.drop_index("ix_sandbox_tasks_task_id", table_name="sandbox_tasks")
    op.drop_index("ix_sandbox_tasks_runner_id", table_name="sandbox_tasks")
    op.drop_index("ix_sandbox_tasks_organization_id", table_name="sandbox_tasks")
    op.drop_index("ix_sandbox_tasks_sandbox_id", table_name="sandbox_tasks")
    op.drop_table("sandbox_tasks")

    op.drop_index("ix_sandboxes_org_user_unique", table_name="sandboxes")
    op.drop_index("ix_sandboxes_runner_id", table_name="sandboxes")
    op.drop_index("ix_sandboxes_org_status", table_name="sandboxes")
    op.drop_index("ix_sandboxes_last_heartbeat", table_name="sandboxes")
    op.drop_index("ix_sandboxes_pod_name", table_name="sandboxes")
    op.drop_index("ix_sandboxes_namespace", table_name="sandboxes")
    op.drop_index("ix_sandboxes_status", table_name="sandboxes")
    op.drop_index("ix_sandboxes_name", table_name="sandboxes")
    op.drop_index("ix_sandboxes_user_id", table_name="sandboxes")
    op.drop_index("ix_sandboxes_organization_id", table_name="sandboxes")
    op.drop_table("sandboxes")
