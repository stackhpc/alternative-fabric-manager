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
    """A Neutron network, which has a partition"""

    __tablename__ = "networks"

    id: orm.Mapped[str] = orm.mapped_column(sa.String(36), primary_key=True)
    pkey: orm.Mapped[int] = orm.mapped_column(unique=True)
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
    guid: orm.Mapped[str] = orm.mapped_column(sa.String(16))
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
    error: orm.Mapped[str] = orm.mapped_column(sa.Text, default="")
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        sa.DateTime(timezone=True), default=_now
    )
    finished_at: orm.Mapped[datetime.datetime | None] = orm.mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class State(Base):
    """Counters kept across restarts"""

    __tablename__ = "state"

    name: orm.Mapped[str] = orm.mapped_column(sa.String(64), primary_key=True)
    value: orm.Mapped[int] = orm.mapped_column(default=0)


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
    return orm.sessionmaker(engine, expire_on_commit=False)
