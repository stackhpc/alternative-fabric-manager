"""While a switch is missing: each hypervisor's learnt physical ports, and
the adapters' links the last passing scan found

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    # Each step checks first: MariaDB commits each DDL statement, so an
    # upgrade that failed midway is run again from where it stopped. A new
    # table, which the previous version never reads.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("host_ports"):
        op.create_table(
            "host_ports",
            sa.Column("guid", sa.String(length=16), nullable=False),
            sa.Column("host", sa.String(length=255), nullable=False),
            sa.Column(
                "learnt",
                sa.DateTime(timezone=True)
                .with_variant(mysql.DATETIME(fsp=6), "mariadb")
                .with_variant(mysql.DATETIME(fsp=6), "mysql"),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("guid"),
        )
    # Nullable, so that the previous version, which doesn't write it, can
    # share the database during a rolling upgrade
    if "adapter_links" not in {c["name"] for c in inspector.get_columns("applied")}:
        op.add_column("applied", sa.Column("adapter_links", sa.Text(), nullable=True))
    inspector = sa.inspect(bind)
    if "ix_host_ports_host" not in {
        index["name"] for index in inspector.get_indexes("host_ports")
    }:
        op.create_index(
            op.f("ix_host_ports_host"), "host_ports", ["host"], unique=False
        )


def downgrade():
    op.drop_column("applied", "adapter_links")
    op.drop_index(op.f("ix_host_ports_host"), table_name="host_ports")
    op.drop_table("host_ports")
