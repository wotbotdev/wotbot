"""Add the workspace registry and bind every API key to one workspace.

Existing keys, including the init admin key, belong to the ``default``
workspace, so behavior is unchanged until other workspaces are created.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_workspace_registry"
down_revision = "0012_workspace_scope"
branch_labels = None
depends_on = None

_DEFAULT_WORKSPACE_ID = "default"


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        sa.text("INSERT INTO workspaces (id, title) VALUES (:id, 'Default')").bindparams(
            id=_DEFAULT_WORKSPACE_ID
        )
    )

    op.add_column(
        "api_keys",
        sa.Column(
            "workspace_id",
            sa.Text(),
            nullable=False,
            server_default=_DEFAULT_WORKSPACE_ID,
        ),
    )
    op.create_index("ix_api_keys_workspace_id", "api_keys", ["workspace_id"])
    op.create_foreign_key(
        "fk_api_keys_workspace_id",
        "api_keys",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    connection = op.get_bind()
    other_workspaces = connection.execute(
        sa.text("SELECT count(*) FROM workspaces WHERE id <> :default"),
        {"default": _DEFAULT_WORKSPACE_ID},
    ).scalar_one()
    if other_workspaces:
        raise RuntimeError(
            f"Refusing to downgrade: {other_workspaces} workspace(s) besides "
            f"'{_DEFAULT_WORKSPACE_ID}' exist. Delete them first."
        )

    op.drop_constraint("fk_api_keys_workspace_id", "api_keys", type_="foreignkey")
    op.drop_index("ix_api_keys_workspace_id", table_name="api_keys")
    op.drop_column("api_keys", "workspace_id")
    op.drop_table("workspaces")
