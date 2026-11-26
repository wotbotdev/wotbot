"""Store use-case guidance for the agent on each workspace."""

import sqlalchemy as sa
from alembic import op

revision = "0015_workspace_prompt"
down_revision = "0014_workspace_search_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("prompt_extra", sa.Text(), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "prompt_extra")
