"""Zero-touch operation: the expected switches' node GUIDs

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    # Each step checks first: MariaDB commits each DDL statement, so an
    # upgrade that failed midway is run again from where it stopped.
    # Nullable, so that the previous version, which doesn't write it, can
    # share the database during a rolling upgrade.
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("expected_switches")}
    if "node_guid" not in columns:
        op.add_column(
            "expected_switches",
            sa.Column("node_guid", sa.String(length=16), nullable=True),
        )


def downgrade():
    op.drop_column("expected_switches", "node_guid")
