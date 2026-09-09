"""Add inbound A2A tasks and panel resources without replacing existing state.

A2A contexts are ordinary hidden threads (``kind='a2a'``), the way jobs already
attach to threads, so there is no context table. Artifact bytes are not copied:
exported files stay in the code executor's store and panel markup stays in
``panel_versions``; ``a2a_artifacts`` records ownership and expiry only.

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

RESERVED_STATES = (
    "'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING', "
    "'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED'"
)


def upgrade() -> None:
    op.drop_constraint("ck_threads_kind", "threads", type_="check")
    op.create_check_constraint("ck_threads_kind", "threads", "kind IN ('chat', 'job', 'a2a')")
    op.add_column("threads", sa.Column("owner_api_key_id", sa.String(), nullable=True))
    op.create_index(
        op.f("ix_threads_owner_api_key_id"), "threads", ["owner_api_key_id"], unique=False
    )

    op.create_table(
        "a2a_tasks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("context_id", sa.String(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pending", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("applied_messages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        "a2a_artifacts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("executor_artifact_id", sa.Text(), nullable=True),
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


def downgrade() -> None:
    op.drop_table("a2a_artifacts")
    op.drop_table("a2a_tasks")
    op.drop_index(op.f("ix_threads_owner_api_key_id"), table_name="threads")
    op.drop_column("threads", "owner_api_key_id")
    # Keep hidden thread/checkpoint data even on downgrade; only relabel its kind.
    op.execute("UPDATE threads SET kind = 'chat' WHERE kind = 'a2a'")
    op.drop_constraint("ck_threads_kind", "threads", type_="check")
    op.create_check_constraint("ck_threads_kind", "threads", "kind IN ('chat', 'job')")
