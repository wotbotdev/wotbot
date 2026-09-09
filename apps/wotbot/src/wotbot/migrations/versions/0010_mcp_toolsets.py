"""Shared agent execution and isolated raw tool contexts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_mcp_toolsets"
down_revision = "0009_a2a_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "a2a_tasks", sa.Column("family", sa.Text(), nullable=False, server_default="assistant")
    )
    op.add_column(
        "a2a_tasks", sa.Column("operation", postgresql.JSONB(), nullable=False, server_default="{}")
    )
    op.add_column("a2a_tasks", sa.Column("origin", sa.Text(), nullable=False, server_default="a2a"))
    op.drop_constraint("ck_threads_kind", "threads", type_="check")
    op.create_check_constraint(
        "ck_threads_kind", "threads", "kind IN ('chat','job','a2a','mcp_raw')"
    )
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

    # Earlier structured outputs lived only in task payloads. Materialize their
    # manifests without changing the retained task, public IDs, or retry hashes.
    op.execute("""
        INSERT INTO a2a_artifacts (id, task_id, owner, name, media_type, metadata, created_at, expires_at)
        SELECT artifact->>'artifactId', task.id, task.owner,
               COALESCE(artifact->>'name', 'Result'), 'application/json',
               jsonb_build_object('kind', 'json', 'artifact', artifact, 'expiresAt', task.expires_at),
               task.updated_at, task.expires_at
        FROM a2a_tasks task,
             LATERAL jsonb_array_elements(COALESCE(task.payload->'artifacts', '[]'::jsonb)) artifact
        WHERE artifact ? 'artifactId' AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(COALESCE(artifact->'parts', '[]'::jsonb)) part
            WHERE part->'data'->>'kind' = 'wotbot.tool_result'
        )
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute("""
        UPDATE a2a_artifacts record
        SET metadata = record.metadata || jsonb_build_object('figure', part->'data'->'figure', 'resultKind', 'wotbot.plotly')
        FROM a2a_tasks task,
             LATERAL jsonb_array_elements(COALESCE(task.payload->'artifacts', '[]'::jsonb)) artifact,
             LATERAL jsonb_array_elements(COALESCE(artifact->'parts', '[]'::jsonb)) part
        WHERE record.id = artifact->>'artifactId' AND record.owner = task.owner
          AND part->'data'->>'kind' = 'wotbot.plotly'
    """)


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM threads WHERE kind='mcp_raw') OR EXISTS (SELECT 1 FROM a2a_tasks WHERE payload ? 'payloadVersion')"
        )
    ):
        raise RuntimeError("Cannot downgrade while shared agent execution records remain")
    op.drop_table("agent_subscriptions")
    op.drop_constraint("ck_threads_kind", "threads", type_="check")
    op.create_check_constraint("ck_threads_kind", "threads", "kind IN ('chat','job','a2a')")
    for field in ("family", "operation", "origin"):
        op.drop_column("a2a_tasks", field)
