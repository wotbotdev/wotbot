# Frozen original 0008 schema for upgrade regression tests. Do not modernize.
"""Add inbound A2A tasks and panel resources without replacing existing state.

Revision ID: 0008_add_a2a
Revises: 0007_add_thing_origin
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_add_a2a"
down_revision = "0007_add_thing_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_threads_kind", "threads", type_="check")
    op.create_check_constraint("ck_threads_kind", "threads", "kind IN ('chat', 'job', 'a2a')")
    op.create_table(
        "a2a_contexts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=False),
        sa.Column("active_task_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id"),
    )
    op.create_index(op.f("ix_a2a_contexts_owner"), "a2a_contexts", ["owner"], unique=False)
    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("context_id", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pending", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["context_id"], ["a2a_contexts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_a2a_tasks_context_id"), "a2a_tasks", ["context_id"], unique=False)
    op.create_index(op.f("ix_a2a_tasks_expires_at"), "a2a_tasks", ["expires_at"], unique=False)
    op.create_index(
        "ix_a2a_tasks_owner_updated", "a2a_tasks", ["owner", "updated_at", "id"], unique=False
    )
    op.create_table(
        "a2a_messages",
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["a2a_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner", "message_id"),
    )
    op.create_table(
        "a2a_artifacts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("panel_version_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
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
        "mcp_panel_grants",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["a2a_artifacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_panel_grants_expires_at", "mcp_panel_grants", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_mcp_panel_grants_owner_artifact",
        "mcp_panel_grants",
        ["owner", "artifact_id"],
        unique=False,
    )
    op.create_index(
        "ix_mcp_panel_grants_token_hash", "mcp_panel_grants", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_table("mcp_panel_grants")
    op.drop_table("a2a_artifacts")
    op.drop_table("a2a_messages")
    op.drop_table("a2a_tasks")
    op.drop_table("a2a_contexts")
    # Keep hidden thread/checkpoint data even on downgrade; only relabel its kind.
    op.execute("UPDATE threads SET kind = 'chat' WHERE kind = 'a2a'")
    op.drop_constraint("ck_threads_kind", "threads", type_="check")
    op.create_check_constraint("ck_threads_kind", "threads", "kind IN ('chat', 'job')")
