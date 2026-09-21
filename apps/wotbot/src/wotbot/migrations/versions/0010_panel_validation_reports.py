"""Retain panel validation reports and screenshots independently of artifacts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_panel_validation_reports"
down_revision = "0009_panel_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "panel_validation_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("thread_id", sa.Text(), sa.ForeignKey("threads.id", ondelete="CASCADE")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("document_sha256", sa.Text(), nullable=False),
        sa.Column("report", JSONB(), nullable=False),
        sa.Column("screenshot", sa.LargeBinary()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_panel_validation_reports_expires_at", "panel_validation_reports", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("panel_validation_reports")
