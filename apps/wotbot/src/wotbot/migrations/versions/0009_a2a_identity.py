"""Preserve both deployed 0008 layouts and enforce owner-wide retry identity.

0008 existed with separate contexts/copied files and, later, with contexts on
threads and executor-backed files. Inspect that boundary once; both layouts
converge on the same schema without renaming graph threads or public IDs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_a2a_identity"
down_revision = "0008_add_a2a"
branch_labels = None
depends_on = None

RESERVED_STATES = (
    "'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING', "
    "'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED'"
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    thread_columns = {c["name"] for c in inspector.get_columns("threads")}
    task_columns = {c["name"] for c in inspector.get_columns("a2a_tasks")}
    artifact_columns = {c["name"] for c in inspector.get_columns("a2a_artifacts")}
    if "owner_api_key_id" not in thread_columns:
        op.add_column("threads", sa.Column("owner_api_key_id", sa.String(), nullable=True))
        op.create_index("ix_threads_owner_api_key_id", "threads", ["owner_api_key_id"])
    op.add_column("threads", sa.Column("legacy_a2a_context_id", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_threads_legacy_a2a_context_id", "threads", ["legacy_a2a_context_id"]
    )

    if "a2a_contexts" in tables:
        op.execute("""
            UPDATE threads AS t
               SET owner_api_key_id = c.owner, legacy_a2a_context_id = c.id
              FROM a2a_contexts AS c WHERE t.id = c.thread_id
        """)
        for foreign_key in inspector.get_foreign_keys("a2a_tasks"):
            if foreign_key["constrained_columns"] == ["context_id"]:
                op.drop_constraint(foreign_key["name"], "a2a_tasks", type_="foreignkey")
        op.execute("""
            UPDATE a2a_tasks AS t SET context_id = c.thread_id
              FROM a2a_contexts AS c WHERE t.context_id = c.id
        """)
        op.alter_column("a2a_tasks", "context_id", existing_type=sa.Text(), type_=sa.String())
        op.create_foreign_key(
            "a2a_tasks_context_id_fkey",
            "a2a_tasks",
            "threads",
            ["context_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.drop_table("a2a_contexts")
    if "uq_a2a_tasks_active_context" not in {
        index["name"] for index in inspector.get_indexes("a2a_tasks")
    }:
        op.create_index(
            "uq_a2a_tasks_active_context",
            "a2a_tasks",
            ["context_id"],
            unique=True,
            postgresql_where=sa.text(f"state IN ({RESERVED_STATES})"),
        )

    if "a2a_messages" not in tables:
        op.create_table(
            "a2a_messages",
            sa.Column("owner", sa.Text(), nullable=False),
            sa.Column("message_id", sa.Text(), nullable=False),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("task_id", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["a2a_tasks.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("owner", "message_id"),
        )
    if "applied_messages" in task_columns:
        # A buggy client may already have reused an ID across tasks. Keep every
        # task, but block replay of that ambiguous ID for the longest retention
        # window. Never choose an arbitrary request that might repeat an action.
        op.execute("""
            WITH messages AS (
                SELECT t.owner, m.key AS message_id, m.value AS request_hash, t.id AS task_id,
                       count(*) OVER (PARTITION BY t.owner, m.key) AS uses,
                       row_number() OVER (
                           PARTITION BY t.owner, m.key ORDER BY t.expires_at DESC, t.id
                       ) AS position
                  FROM a2a_tasks AS t
                  CROSS JOIN LATERAL jsonb_each_text(t.applied_messages) AS m
            )
            INSERT INTO a2a_messages (owner, message_id, request_hash, task_id)
            SELECT owner, message_id,
                   CASE WHEN uses > 1 THEN 'conflict' ELSE request_hash END, task_id
              FROM messages WHERE position = 1
            ON CONFLICT (owner, message_id) DO NOTHING
        """)
        op.drop_column("a2a_tasks", "applied_messages")

    if "executor_artifact_id" not in artifact_columns:
        op.add_column("a2a_artifacts", sa.Column("executor_artifact_id", sa.Text(), nullable=True))
    if "content" in artifact_columns:
        op.alter_column(
            "a2a_artifacts",
            "content",
            new_column_name="legacy_content",
            existing_type=sa.LargeBinary(),
            nullable=True,
        )
        # Panel sources already live in immutable panel_versions. Only copied
        # exports need the compatibility bytes until their original expiry.
        op.execute(
            "UPDATE a2a_artifacts SET legacy_content = NULL WHERE panel_version_id IS NOT NULL"
        )
    else:
        op.add_column("a2a_artifacts", sa.Column("legacy_content", sa.LargeBinary(), nullable=True))
    op.execute("""
        UPDATE a2a_artifacts AS a
           SET metadata = jsonb_set(a.metadata, '{subscriptions}', (
               SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(item) = 'string'
                                             THEN item ELSE item->'id' END), '[]'::jsonb)
                 FROM jsonb_array_elements(a.metadata->'subscriptions') AS item
                WHERE jsonb_typeof(item) = 'string' OR jsonb_typeof(item->'id') = 'string'
           ))
         WHERE jsonb_typeof(a.metadata->'subscriptions') = 'array'
    """)
    if "mcp_panel_grants" in tables:
        # Launch grants are disposable capabilities, now held in Redis. An
        # already-open legacy app must reopen its tool once after upgrading.
        op.drop_table("mcp_panel_grants")


def downgrade() -> None:
    # Returning to the cleanup's 0008 would make migrated public IDs and copied
    # exports inaccessible. Refuse that downgrade while compatibility data exists.
    connection = op.get_bind()
    if connection.scalar(
        sa.text("""
        SELECT EXISTS (SELECT 1 FROM threads WHERE legacy_a2a_context_id IS NOT NULL)
            OR EXISTS (SELECT 1 FROM a2a_artifacts WHERE legacy_content IS NOT NULL)
    """)
    ):
        raise RuntimeError(
            "Cannot downgrade while migrated A2A contexts or copied artifacts remain"
        )
    op.add_column(
        "a2a_tasks",
        sa.Column(
            "applied_messages",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute("""
        UPDATE a2a_tasks AS t SET applied_messages = m.messages
          FROM (SELECT task_id, jsonb_object_agg(message_id, request_hash) AS messages
                  FROM a2a_messages GROUP BY task_id) AS m
         WHERE t.id = m.task_id
    """)
    op.alter_column("a2a_tasks", "applied_messages", server_default=None)
    op.drop_table("a2a_messages")
    op.drop_column("a2a_artifacts", "legacy_content")
    op.drop_constraint("uq_threads_legacy_a2a_context_id", "threads", type_="unique")
    op.drop_column("threads", "legacy_a2a_context_id")
