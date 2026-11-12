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


def _columns(inspector, table):
    return {column["name"]: column for column in inspector.get_columns(table)}


def _is_bigint(column):
    return isinstance(column["type"], sa.BigInteger)


def upgrade():
    # Each step checks first: MariaDB commits each DDL statement, so an
    # upgrade that failed midway is run again from where it stopped.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Nullable, so that the previous version, which doesn't write them, can
    # share the database during a rolling upgrade
    for table in ("networks", "ports"):
        if "revision" not in _columns(inspector, table):
            op.add_column(table, sa.Column("revision", sa.BigInteger(), nullable=True))
    if not inspector.has_table("tombstones"):
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
    inspector = sa.inspect(bind)
    if "ix_tombstones_deleted_at" not in {
        index["name"] for index in inspector.get_indexes("tombstones")
    }:
        op.create_index(
            op.f("ix_tombstones_deleted_at"), "tombstones", ["deleted_at"], unique=False
        )
    # Counters that every request raises, 64-bit, so that none overflows in
    # a cluster's life. deferred.job_id references jobs.id, and MariaDB won't
    # change either while the key exists, even with its checks off, so the
    # key is dropped, both are widened, and the key is made again; a run
    # that failed in between makes it again from the default name.
    jobs = _columns(inspector, "jobs")
    deferred = _columns(inspector, "deferred")
    keys = [
        key
        for key in inspector.get_foreign_keys("deferred")
        if key["referred_table"] == "jobs"
    ]
    widen = not (
        _is_bigint(jobs["id"])
        and _is_bigint(jobs["generation"])
        and _is_bigint(deferred["job_id"])
    )
    if widen:
        for key in keys:
            op.drop_constraint(key["name"], "deferred", type_="foreignkey")
        # jobs in one statement: under Galera's total order isolation each
        # rebuild of a table blocks every commit in the cluster.
        op.execute(
            "ALTER TABLE jobs MODIFY id BIGINT NOT NULL AUTO_INCREMENT, "
            "MODIFY generation BIGINT NOT NULL"
        )
        op.alter_column(
            "deferred",
            "job_id",
            type_=sa.BigInteger(),
            existing_type=sa.Integer(),
            existing_nullable=False,
        )
    if widen or not keys:
        names = [key["name"] for key in keys] or ["deferred_ibfk_1"]
        for name in names:
            op.create_foreign_key(name, "deferred", "jobs", ["job_id"], ["id"])
    for table, column in (("applied", "generation"), ("state", "value")):
        if not _is_bigint(_columns(inspector, table)[column]):
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
