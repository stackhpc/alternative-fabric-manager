"""Revisions and tombstones, to refuse late requests; 64-bit counters

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable, so that the previous version, which doesn't write them, can
    # share the database during a rolling upgrade
    op.add_column("networks", sa.Column("revision", sa.BigInteger(), nullable=True))
    op.add_column("ports", sa.Column("revision", sa.BigInteger(), nullable=True))
    op.create_table(
        "tombstones",
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("object_id", sa.String(length=64), nullable=False),
        sa.Column("set_revision", sa.BigInteger(), nullable=True),
        sa.Column("deleted_revision", sa.BigInteger(), nullable=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True)
            .with_variant(mysql.DATETIME(fsp=6), "mariadb")
            .with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("kind", "object_id"),
    )
    op.create_index(
        op.f("ix_tombstones_deleted_at"), "tombstones", ["deleted_at"], unique=False
    )
    # Counters that every request raises, 64-bit, so that none overflows in
    # a cluster's life. deferred.job_id references jobs.id, and MariaDB won't
    # change either while the key exists, even with its checks off, so the
    # key is dropped, both are widened, and the key is made again.
    inspector = sa.inspect(op.get_bind())
    keys = [
        key
        for key in inspector.get_foreign_keys("deferred")
        if key["referred_table"] == "jobs"
    ]
    for key in keys:
        op.drop_constraint(key["name"], "deferred", type_="foreignkey")
    op.alter_column(
        "jobs",
        "id",
        type_=sa.BigInteger(),
        existing_type=sa.Integer(),
        existing_nullable=False,
        autoincrement=True,
    )
    op.alter_column(
        "deferred",
        "job_id",
        type_=sa.BigInteger(),
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    for key in keys:
        op.create_foreign_key(
            key["name"],
            "deferred",
            "jobs",
            key["constrained_columns"],
            key["referred_columns"],
        )
    for table, column in (
        ("jobs", "generation"),
        ("applied", "generation"),
        ("state", "value"),
    ):
        op.alter_column(
            table,
            column,
            type_=sa.BigInteger(),
            existing_type=sa.Integer(),
            existing_nullable=False,
        )


def downgrade():
    # The counters stay 64-bit: narrowing them could truncate.
    op.drop_index(op.f("ix_tombstones_deleted_at"), table_name="tombstones")
    op.drop_table("tombstones")
    op.drop_column("ports", "revision")
    op.drop_column("networks", "revision")
