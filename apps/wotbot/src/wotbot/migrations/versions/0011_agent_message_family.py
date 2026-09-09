"""Scope retry identity to the task family that recorded it.

``/mcp/raw`` contexts are documented as separate from assistant conversations,
so the same ``messageId`` may be in flight on both without one being reported
as "already used with different content".
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_agent_message_family"
down_revision = "0010_mcp_toolsets"
branch_labels = None
depends_on = None


def _primary_key() -> str:
    return sa.inspect(op.get_bind()).get_pk_constraint("a2a_messages")["name"]


def upgrade() -> None:
    existing = _primary_key()
    op.add_column(
        "a2a_messages", sa.Column("family", sa.Text(), nullable=False, server_default="assistant")
    )
    # Raw contexts arrived with 0010, so recorded identities are already the
    # family of the task they belong to. Take it from there rather than assume.
    op.execute("""
        UPDATE a2a_messages AS message SET family = task.family
          FROM a2a_tasks AS task WHERE message.task_id = task.id
    """)
    op.drop_constraint(existing, "a2a_messages", type_="primary")
    op.create_primary_key("a2a_messages_pkey", "a2a_messages", ["owner", "family", "message_id"])


def downgrade() -> None:
    existing = _primary_key()
    # One owner-wide identity can only keep one row per message. Retain the
    # assistant one, which is the only family the narrower key ever held.
    op.execute("""
        DELETE FROM a2a_messages AS message
              USING a2a_messages AS other
              WHERE message.owner = other.owner
                AND message.message_id = other.message_id
                AND message.family > other.family
    """)
    op.drop_constraint(existing, "a2a_messages", type_="primary")
    op.create_primary_key("a2a_messages_pkey", "a2a_messages", ["owner", "message_id"])
    op.drop_column("a2a_messages", "family")
