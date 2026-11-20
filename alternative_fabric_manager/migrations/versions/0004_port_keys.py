"""Per-port M_Key state: fabric ports and links, state's text, and
pending expected switches

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _timestamp():
    return (
        sa.DateTime(timezone=True)
        .with_variant(mysql.DATETIME(fsp=6), "mariadb")
        .with_variant(mysql.DATETIME(fsp=6), "mysql")
    )


def upgrade():
    # Each step checks first: MariaDB commits each DDL statement, so an
    # upgrade that failed midway is run again from where it stopped.
    inspector = sa.inspect(op.get_bind())
    # Nullable, so that the previous version, which doesn't write it, can
    # share the database during a rolling upgrade
    if "text" not in {column["name"] for column in inspector.get_columns("state")}:
        op.add_column("state", sa.Column("text", sa.String(length=255), nullable=True))
    # With a server default, for the same reason
    expected = {c["name"] for c in inspector.get_columns("expected_switches")}
    if "pending" not in expected:
        op.add_column(
            "expected_switches",
            sa.Column(
                "pending", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )
    if not inspector.has_table("fabric_ports"):
        op.create_table(
            "fabric_ports",
            sa.Column("guid", sa.String(length=16), nullable=False),
            sa.Column("node_guid", sa.String(length=16), nullable=True),
            sa.Column("switch", sa.Boolean(), nullable=True),
            sa.Column("holds", sa.String(length=16), nullable=False),
            sa.Column("lapsed", sa.Boolean(), nullable=False),
            sa.Column("reachable", sa.Boolean(), nullable=False),
            sa.Column("released", sa.Boolean(), nullable=False),
            sa.Column("last_seen", _timestamp(), nullable=True),
            sa.PrimaryKeyConstraint("guid"),
        )
    if not inspector.has_table("fabric_links"):
        op.create_table(
            "fabric_links",
            sa.Column("guid", sa.String(length=16), nullable=False),
            sa.Column("port", sa.Integer(), autoincrement=False, nullable=False),
            sa.Column("peer_guid", sa.String(length=16), nullable=False),
            sa.Column("peer_port", sa.Integer(), nullable=False),
            sa.Column("last_seen", _timestamp(), nullable=True),
            sa.PrimaryKeyConstraint("guid", "port"),
        )


def downgrade():
    op.drop_table("fabric_links")
    op.drop_table("fabric_ports")
    op.drop_column("expected_switches", "pending")
    op.drop_column("state", "text")
