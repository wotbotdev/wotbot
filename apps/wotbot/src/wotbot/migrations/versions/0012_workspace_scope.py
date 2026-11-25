"""Scope the Thing catalog and discovery sources to workspaces.

Existing rows move to the ``default`` workspace, so a deployment without
``WORKSPACES_ENABLED`` keeps seeing exactly the data it had before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_workspace_scope"
down_revision = "0011_panel_narrow_screenshot"
branch_labels = None
depends_on = None

_DEFAULT_WORKSPACE_ID = "default"
_TABLES = ("things", "discovery_sources", "discovery_source_credentials")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "workspace_id",
                sa.Text(),
                nullable=False,
                server_default=_DEFAULT_WORKSPACE_ID,
            ),
        )
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])

    op.drop_constraint("uq_discovery_source_identity", "discovery_sources", type_="unique")
    op.create_unique_constraint(
        "uq_discovery_source_identity",
        "discovery_sources",
        ["workspace_id", "provider", "external_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in _TABLES:
        scoped_rows = connection.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE workspace_id <> :default"),
            {"default": _DEFAULT_WORKSPACE_ID},
        ).scalar_one()
        if scoped_rows:
            raise RuntimeError(
                f"Refusing to downgrade: {table} has {scoped_rows} row(s) outside the "
                f"'{_DEFAULT_WORKSPACE_ID}' workspace. Delete or move them first."
            )

    op.drop_constraint("uq_discovery_source_identity", "discovery_sources", type_="unique")
    op.create_unique_constraint(
        "uq_discovery_source_identity",
        "discovery_sources",
        ["provider", "external_id"],
    )
    for table in _TABLES:
        op.drop_index(f"ix_{table}_workspace_id", table_name=table)
        op.drop_column(table, "workspace_id")
