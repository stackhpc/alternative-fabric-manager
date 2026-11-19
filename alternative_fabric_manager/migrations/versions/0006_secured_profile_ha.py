"""The secured profile in HA mode: node port GUIDs, the running allowed list

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    # Each step checks first: MariaDB commits each DDL statement, so an
    # upgrade that failed midway is run again from where it stopped. Both
    # are nullable, so that the previous version, which doesn't write them,
    # can share the database during a rolling upgrade.
    inspector = sa.inspect(op.get_bind())
    if "port_guids" not in {c["name"] for c in inspector.get_columns("nodes")}:
        op.add_column("nodes", sa.Column("port_guids", sa.Text(), nullable=True))
    if "allowed_sm" not in {c["name"] for c in inspector.get_columns("applied")}:
        op.add_column("applied", sa.Column("allowed_sm", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("applied", "allowed_sm")
    op.drop_column("nodes", "port_guids")
