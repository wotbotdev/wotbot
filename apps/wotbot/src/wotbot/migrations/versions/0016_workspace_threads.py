"""Remember which workspace each chat thread belongs to.

Existing threads belong to the ``default`` workspace. Only the chat history
list filters on this column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_workspace_threads"
down_revision = "0015_workspace_prompt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "threads",
        sa.Column("workspace_id", sa.String(), nullable=False, server_default="default"),
    )
    op.create_index("ix_threads_workspace_id", "threads", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_threads_workspace_id", table_name="threads")
    op.drop_column("threads", "workspace_id")
