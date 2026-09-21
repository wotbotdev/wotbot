"""Attach immutable data snapshots to generated panels."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_panel_data"
down_revision = "0008_agent_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "panel_data",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        if_not_exists=True,
    )
    for table in ("panels", "panel_versions"):
        # Idempotent when a retained database is restamped to the previous head.
        if "data" not in {
            column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)
        }:
            op.add_column(
                table,
                sa.Column("data", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            )


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("panels", "panel_versions"):
        if connection.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE data <> '{{}}'::jsonb LIMIT 1")
        ).first():
            raise RuntimeError("Cannot downgrade while panel data attachments remain")
    for table in ("panels", "panel_versions"):
        op.drop_column(table, "data")
    op.drop_table("panel_data")
