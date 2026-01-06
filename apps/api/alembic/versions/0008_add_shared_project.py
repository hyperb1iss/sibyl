"""Add shared project support.

Revision ID: 0008_add_shared_project
Revises: 0007_system_settings
Create Date: 2026-01-05

Adds `is_shared` column to projects table and creates a shared project
for each existing organization. The shared project holds org-wide knowledge
like conventions, crawled docs, and learnings not tied to a specific project.

Graph backfill (reassigning orphan entities) is handled separately via:
    sibyld db backfill-shared-projects
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_add_shared_project"
down_revision: str | None = "0007_system_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Shared project constants (must match sibyl_core.models.projects)
SHARED_PROJECT_SLUG = "_shared"
SHARED_PROJECT_NAME = "Shared"
SHARED_PROJECT_DESCRIPTION = "Org-wide knowledge: conventions, crawled docs, and shared learnings"


def upgrade() -> None:
    # =========================================================================
    # 1. Add is_shared column
    # =========================================================================
    op.add_column(
        "projects",
        sa.Column(
            "is_shared",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # =========================================================================
    # 2. Create shared project for each existing organization
    # =========================================================================
    connection = op.get_bind()

    # Get all organizations
    orgs = connection.execute(sa.text("SELECT id FROM organizations")).fetchall()

    for (org_id,) in orgs:
        # Check if shared project already exists for this org
        existing = connection.execute(
            sa.text("""
                SELECT id FROM projects
                WHERE organization_id = :org_id AND slug = :slug
            """),
            {"org_id": org_id, "slug": SHARED_PROJECT_SLUG},
        ).fetchone()

        if existing:
            # Mark existing as shared
            connection.execute(
                sa.text("UPDATE projects SET is_shared = true WHERE id = :id"),
                {"id": existing[0]},
            )
            continue

        # Get org owner to use as project owner
        owner = connection.execute(
            sa.text("""
                SELECT u.id FROM users u
                JOIN organization_members om ON om.user_id = u.id
                WHERE om.organization_id = :org_id AND om.role = 'owner'
                LIMIT 1
            """),
            {"org_id": org_id},
        ).fetchone()

        if not owner:
            # Fallback: get any admin
            owner = connection.execute(
                sa.text("""
                    SELECT u.id FROM users u
                    JOIN organization_members om ON om.user_id = u.id
                    WHERE om.organization_id = :org_id AND om.role = 'admin'
                    LIMIT 1
                """),
                {"org_id": org_id},
            ).fetchone()

        if not owner:
            # Last resort: any member
            owner = connection.execute(
                sa.text("""
                    SELECT u.id FROM users u
                    JOIN organization_members om ON om.user_id = u.id
                    WHERE om.organization_id = :org_id
                    LIMIT 1
                """),
                {"org_id": org_id},
            ).fetchone()

        if not owner:
            # Skip orgs with no members
            continue

        owner_id = owner[0]
        project_id = uuid4()
        # Graph ID will be set by the backfill command after creating graph entity
        graph_project_id = f"proj_{project_id.hex[:16]}"

        connection.execute(
            sa.text("""
                INSERT INTO projects (
                    id, organization_id, owner_user_id, name, slug, description,
                    graph_project_id, visibility, is_shared
                ) VALUES (
                    :id, :org_id, :owner_id, :name, :slug, :description,
                    :graph_id, 'org', true
                )
            """),
            {
                "id": project_id,
                "org_id": org_id,
                "owner_id": owner_id,
                "name": SHARED_PROJECT_NAME,
                "slug": SHARED_PROJECT_SLUG,
                "description": SHARED_PROJECT_DESCRIPTION,
                "graph_id": graph_project_id,
            },
        )

    # =========================================================================
    # 3. Add partial unique index: one shared project per org
    # =========================================================================
    op.create_index(
        "ix_projects_org_shared_unique",
        "projects",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_shared = true"),
    )


def downgrade() -> None:
    # Drop partial unique index
    op.drop_index("ix_projects_org_shared_unique", table_name="projects")

    # Delete shared projects
    connection = op.get_bind()
    connection.execute(sa.text("DELETE FROM projects WHERE is_shared = true"))

    # Drop column
    op.drop_column("projects", "is_shared")
