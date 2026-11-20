"""Allowed SMs: known SM ports

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _timestamp():
    return (
        sa.DateTime(timezone=True)
        .with_variant(mysql.DATETIME(fsp=6), "mariadb")
        .with_variant(mysql.DATETIME(fsp=6), "mysql")
    )


def upgrade():
    # Checked first, as 0004 does: MariaDB commits each DDL statement, so an
    # upgrade that failed midway is run again from where it stopped.
    if not sa.inspect(op.get_bind()).has_table("known_sm_ports"):
        op.create_table(
            "known_sm_ports",
            sa.Column("guid", sa.String(length=16), nullable=False),
            sa.Column("own", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("recorded", _timestamp(), nullable=True),
            sa.PrimaryKeyConstraint("guid"),
        )


def downgrade():
    op.drop_table("known_sm_ports")
