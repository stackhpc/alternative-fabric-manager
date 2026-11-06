# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""The fabric's desired state, and the jobs that apply it"""

import datetime
import enum
import os

import sqlalchemy as sa
from sqlalchemy import orm


class Base(orm.DeclarativeBase):
    pass


class Network(Base):
    """A Neutron network: a VLAN network has its own partition, a flat
    network (pkey None) shares the default partition"""

    __tablename__ = "networks"

    id: orm.Mapped[str] = orm.mapped_column(sa.String(36), primary_key=True)
    pkey: orm.Mapped[int | None] = orm.mapped_column(unique=True, nullable=True)
    name: orm.Mapped[str] = orm.mapped_column(sa.String(255), default="")


class Port(Base):
    """A Neutron port, whose GUID is a full member of its network's
    partition"""

    __tablename__ = "ports"

    id: orm.Mapped[str] = orm.mapped_column(sa.String(36), primary_key=True)
    network_id: orm.Mapped[str] = orm.mapped_column(
        sa.ForeignKey("networks.id", ondelete="CASCADE"), index=True
    )
    # As 16 hex digits: a GUID often has its top bit set, which no SQL
    # integer type holds unsigned.
    # Unique: a GUID in two networks' partitions would be on both.
    guid: orm.Mapped[str] = orm.mapped_column(sa.String(16), unique=True)
    host: orm.Mapped[str] = orm.mapped_column(sa.String(255), default="")


class JobStatus(str, enum.Enum):
    # The values networking-mlnx reads. Anything other than Pending,
    # Running and Completed fails the request.
    PENDING = "Pending"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


class Job(Base):
    """A change to the desired state, which completes once OpenSM has
    applied it"""

    __tablename__ = "jobs"

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    status: orm.Mapped[JobStatus] = orm.mapped_column(
        sa.Enum(JobStatus, native_enum=False, length=16),
        default=JobStatus.PENDING,
        index=True,
    )
    operation: orm.Mapped[str] = orm.mapped_column(sa.String(64))
    object_id: orm.Mapped[str] = orm.mapped_column(sa.String(36))
    # The first generation of the state that includes the change
    generation: orm.Mapped[int] = orm.mapped_column(default=0, index=True)
    # What must hold in a swept partitions file for the job to complete, as
    # JSON (see effects.py)
    effect: orm.Mapped[str] = orm.mapped_column(sa.Text, default="{}")
    deadline: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    error: orm.Mapped[str] = orm.mapped_column(sa.Text, default="")
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        sa.DateTime(timezone=True), default=_now
    )
    finished_at: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class Quarantine(Base):
    """A network or port left out of the partitions file, because it broke
    a render or OpenSM's parse of it, until its next change"""

    __tablename__ = "quarantine"

    kind: orm.Mapped[str] = orm.mapped_column(sa.String(16), primary_key=True)
    object_id: orm.Mapped[str] = orm.mapped_column(sa.String(64), primary_key=True)
    reason: orm.Mapped[str] = orm.mapped_column(sa.Text, default="")


class Applied(Base):
    """What OpenSM last swept: one row"""

    __tablename__ = "applied"

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True, default=1)
    generation: orm.Mapped[int] = orm.mapped_column(default=0)
    body: orm.Mapped[str] = orm.mapped_column(sa.Text, default="")
    # Which network each partition, and which port each membership, of the
    # body comes from, as JSON (see partitions.attribute())
    attribution: orm.Mapped[str] = orm.mapped_column(sa.Text, default="{}")
    # GUIDs whose pkey tables OpenSM couldn't set in that sweep, and switch
    # ports whose partition enforcement it cleared, as JSON lists
    pkey_errors: orm.Mapped[str] = orm.mapped_column(sa.Text, default="[]")
    enforcement_errors: orm.Mapped[str] = orm.mapped_column(sa.Text, default="[]")
    # The last fabric check
    check_ok: orm.Mapped[bool] = orm.mapped_column(default=False)
    check_time: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    check_detail: orm.Mapped[str] = orm.mapped_column(sa.Text, default="")


class ExpectedSwitch(Base):
    """A switch that a complete fabric has, as an operator confirmed"""

    __tablename__ = "expected_switches"

    guid: orm.Mapped[str] = orm.mapped_column(sa.String(16), primary_key=True)


class Secret(Base):
    """Keys that every OpenSM of the fabric must share, kept across restarts"""

    __tablename__ = "secrets"

    name: orm.Mapped[str] = orm.mapped_column(sa.String(64), primary_key=True)
    value: orm.Mapped[str] = orm.mapped_column(sa.String(255))


class State(Base):
    """Counters kept across restarts"""

    __tablename__ = "state"

    name: orm.Mapped[str] = orm.mapped_column(sa.String(64), primary_key=True)
    value: orm.Mapped[int] = orm.mapped_column(default=0)


# Columns added to existing tables since the first release, until the
# schema is managed by Alembic
_ADDED_COLUMNS = {
    "jobs": {
        "generation": "INTEGER NOT NULL DEFAULT 0",
        "effect": "TEXT NOT NULL DEFAULT '{}'",
        "deadline": "DATETIME",
    },
}


def _upgrade(engine):
    """Add the columns an older database lacks

    Jobs from before effects existed can't be judged, so the open ones fail,
    and networking-mlnx sends their requests again.
    """
    inspector = sa.inspect(engine)
    missing = {
        table: [
            name
            for name in columns
            if name not in {c["name"] for c in inspector.get_columns(table)}
        ]
        for table, columns in _ADDED_COLUMNS.items()
    }
    with engine.begin() as connection:
        for table, names in missing.items():
            for name in names:
                definition = _ADDED_COLUMNS[table][name]
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )
        # create_all() leaves existing tables' indexes alone.
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_generation ON jobs (generation)"
        )
        if "effect" in missing["jobs"]:
            connection.execute(
                sa.update(Job.__table__)
                .where(
                    Job.__table__.c.status.in_([JobStatus.PENDING, JobStatus.RUNNING])
                )
                .values(status=JobStatus.FAILED, error="Superseded by an upgrade")
            )


def make_sessionmaker(database):
    """Return a session factory for an SQLite file, creating the schema"""
    if database != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(database)), exist_ok=True)
    engine = sa.create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False},
        # One connection, so that an in-memory database is shared, and
        # writes are serialised.
        poolclass=sa.pool.StaticPool,
    )

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    _upgrade(engine)
    if database != ":memory:":
        # It holds the fabric's OpenSM keys.
        os.chmod(database, 0o600)
    return orm.sessionmaker(engine, expire_on_commit=False)
