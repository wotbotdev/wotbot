"""Scope the semantic search index to workspaces.

Existing chunks take the workspace of the Thing they index, so Things created
in other workspaces since 0012 stay searchable only there. Chunks without a
matching Thing fall back to ``default``.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_workspace_search_index"
down_revision = "0013_workspace_registry"
branch_labels = None
depends_on = None

_DEFAULT_WORKSPACE_ID = "default"


def upgrade() -> None:
    op.add_column(
        "search_index_chunks",
        sa.Column(
            "workspace_id",
            sa.Text(),
            nullable=False,
            server_default=_DEFAULT_WORKSPACE_ID,
        ),
    )
    op.execute(
        "UPDATE search_index_chunks AS chunk SET workspace_id = thing.workspace_id "
        "FROM things AS thing WHERE thing.id = chunk.thing_id"
    )
    op.create_index("ix_search_index_chunks_workspace_id", "search_index_chunks", ["workspace_id"])


def downgrade() -> None:
    scoped_rows = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM search_index_chunks WHERE workspace_id <> :default"),
            {"default": _DEFAULT_WORKSPACE_ID},
        )
        .scalar_one()
    )
    if scoped_rows:
        raise RuntimeError(
            f"Refusing to downgrade: search_index_chunks has {scoped_rows} row(s) outside "
            f"the '{_DEFAULT_WORKSPACE_ID}' workspace. Delete or move them first."
        )
    op.drop_index("ix_search_index_chunks_workspace_id", table_name="search_index_chunks")
    op.drop_column("search_index_chunks", "workspace_id")
