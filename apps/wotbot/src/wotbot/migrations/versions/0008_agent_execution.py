"""Add shared A2A/MCP execution without replacing existing application state.

Assistant and raw contexts are hidden threads. Task retry identities are scoped
by API key and family, artifacts reference executor files or saved panel
versions, and raw subscriptions have renewable leases.

Revision ID: 0008_agent_execution
Revises: 0007_add_thing_origin
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_agent_execution"
down_revision = "0007_add_thing_origin"
branch_labels = None
depends_on = None

RESERVED_STATES = (
    "'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING', "
    "'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED'"
)


def upgrade() -> None:
    op.drop_constraint("ck_threads_kind", "threads", type_="check")
    op.create_check_constraint(
        "ck_threads_kind", "threads", "kind IN ('chat', 'job', 'a2a', 'mcp_raw')"
    )
    op.add_column("threads", sa.Column("owner_api_key_id", sa.String(), nullable=True))
    op.create_index(
        op.f("ix_threads_owner_api_key_id"), "threads", ["owner_api_key_id"], unique=False
    )
    # Keep the final branch schema, including compatibility fields still read
    # by the application, so existing development databases can be re-stamped.
    op.add_column("threads", sa.Column("legacy_a2a_context_id", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_threads_legacy_a2a_context_id", "threads", ["legacy_a2a_context_id"]
    )

    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("context_id", sa.String(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("family", sa.Text(), nullable=False, server_default="assistant"),
        sa.Column("operation", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("origin", sa.Text(), nullable=False, server_default="a2a"),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pending", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["context_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_a2a_tasks_context_id"), "a2a_tasks", ["context_id"], unique=False)
    op.create_index(op.f("ix_a2a_tasks_expires_at"), "a2a_tasks", ["expires_at"], unique=False)
    op.create_index(
        "ix_a2a_tasks_owner_updated", "a2a_tasks", ["owner", "updated_at", "id"], unique=False
    )
    # Enforces "one executing or paused task per context" in the database.
    op.create_index(
        "uq_a2a_tasks_active_context",
        "a2a_tasks",
        ["context_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({RESERVED_STATES})"),
    )

    op.create_table(
        "a2a_messages",
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("family", sa.Text(), nullable=False, server_default="assistant"),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["a2a_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner", "family", "message_id"),
    )

    op.create_table(
        "a2a_artifacts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("executor_artifact_id", sa.Text(), nullable=True),
        sa.Column("legacy_content", sa.LargeBinary(), nullable=True),
        sa.Column("panel_version_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["panel_version_id"], ["panel_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_a2a_artifacts_owner_media_created_id",
        "a2a_artifacts",
        ["owner", "media_type", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_a2a_artifacts_expires_at"), "a2a_artifacts", ["expires_at"], unique=False
    )
    op.create_index(op.f("ix_a2a_artifacts_owner"), "a2a_artifacts", ["owner"], unique=False)
    op.create_index(
        op.f("ix_a2a_artifacts_panel_version_id"),
        "a2a_artifacts",
        ["panel_version_id"],
        unique=False,
    )
    op.create_index(op.f("ix_a2a_artifacts_task_id"), "a2a_artifacts", ["task_id"], unique=False)

    op.create_table(
        "agent_subscriptions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column(
            "context_id",
            sa.String(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("runtime_id", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    for field in ("owner", "context_id", "expires_at"):
        op.create_index(f"ix_agent_subscriptions_{field}", "agent_subscriptions", [field])


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("""
            SELECT EXISTS (SELECT 1 FROM threads WHERE kind IN ('a2a', 'mcp_raw')
                           OR owner_api_key_id IS NOT NULL OR legacy_a2a_context_id IS NOT NULL)
                OR EXISTS (SELECT 1 FROM a2a_tasks)
                OR EXISTS (SELECT 1 FROM a2a_artifacts)
                OR EXISTS (SELECT 1 FROM agent_subscriptions)
        """)
    ):
        raise RuntimeError("Cannot downgrade while agent execution records remain")
    op.drop_table("agent_subscriptions")
    op.drop_table("a2a_artifacts")
    op.drop_table("a2a_messages")
    op.drop_table("a2a_tasks")
    op.drop_constraint("uq_threads_legacy_a2a_context_id", "threads", type_="unique")
    op.drop_column("threads", "legacy_a2a_context_id")
    op.drop_index(op.f("ix_threads_owner_api_key_id"), table_name="threads")
    op.drop_column("threads", "owner_api_key_id")
    op.drop_constraint("ck_threads_kind", "threads", type_="check")
    op.create_check_constraint("ck_threads_kind", "threads", "kind IN ('chat', 'job')")
