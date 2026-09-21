"""Retain narrow viewport evidence alongside the original panel screenshot."""

import sqlalchemy as sa
from alembic import op

revision = "0011_panel_narrow_screenshot"
down_revision = "0010_panel_validation_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("panel_validation_reports", sa.Column("narrow_screenshot", sa.LargeBinary()))


def downgrade() -> None:
    op.drop_column("panel_validation_reports", "narrow_screenshot")
