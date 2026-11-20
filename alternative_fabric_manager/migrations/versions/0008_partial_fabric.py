"""While a switch is missing: each port's vnic_type, and what a partial
fabric check and the possibly held memberships keep in applied

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Nullable, or with a server default, so that the previous version, which
# doesn't write them, can share the database during a rolling upgrade
APPLIED = (
    ("check_partial", sa.Boolean(), sa.false()),
    ("check_placed", sa.Text(), None),
    ("missing_switches", sa.Text(), None),
    ("missing", sa.Boolean(), sa.false()),
    ("placements", sa.Text(), None),
    ("held", sa.Text(), None),
    ("held_term", sa.Integer(), "0"),
)


def upgrade():
    # Each step checks first: MariaDB commits each DDL statement, so an
    # upgrade that failed midway is run again from where it stopped.
    inspector = sa.inspect(op.get_bind())
    if "vnic_type" not in {c["name"] for c in inspector.get_columns("ports")}:
        op.add_column(
            "ports", sa.Column("vnic_type", sa.String(length=64), nullable=True)
        )
    columns = {c["name"] for c in inspector.get_columns("applied")}
    for name, type_, default in APPLIED:
        if name in columns:
            continue
        if default is None:
            column = sa.Column(name, type_, nullable=True)
        else:
            column = sa.Column(name, type_, nullable=False, server_default=default)
        op.add_column("applied", column)


def downgrade():
    for name, _type, _default in reversed(APPLIED):
        op.drop_column("applied", name)
    op.drop_column("ports", "vnic_type")
