import sqlite3

import pytest
import sqlalchemy as sa

from alternative_fabric_manager import db
from tests import conftest


def test_the_package_ships_its_migrations():
    # Only server databases run them, so without this the SQLite suite
    # passes against an installed package that has lost them.
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(db._alembic_config("sqlite://"))
    assert script.get_current_head() is not None


def test_an_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "afm.sqlite"
    # The first release's jobs table, with an open job, and a key to keep
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            status VARCHAR(9) NOT NULL,
            operation VARCHAR(64) NOT NULL,
            object_id VARCHAR(36) NOT NULL,
            error TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            finished_at DATETIME
        );
        INSERT INTO jobs VALUES
            (1, 'PENDING', 'put_network', 'n', '', '2026-11-01 00:00:00', NULL),
            (2, 'COMPLETED', 'put_port', 'p', '', '2026-11-01 00:00:00', NULL);
        CREATE TABLE secrets (name VARCHAR(16) PRIMARY KEY, value VARCHAR(32));
        INSERT INTO secrets VALUES ('m_key', '0x1234');
        """
    )
    connection.commit()
    connection.close()

    sessionmaker = db.make_sessionmaker(str(path))
    with sessionmaker() as session:
        assert session.get(db.Job, 1).status == db.JobStatus.FAILED
        assert session.get(db.Job, 2).status == db.JobStatus.COMPLETED
        assert session.get(db.Job, 2).effect == "{}"
        assert session.get(db.Secret, "m_key").value == "0x1234"
        assert session.get(db.Applied, 1) is None
    # Upgrading again changes nothing.
    db.make_sessionmaker(str(path))
    connection = sqlite3.connect(path)
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(jobs)")}
    assert "ix_jobs_generation" in indexes


@pytest.mark.skipif(not conftest.SERVER_URL, reason="needs AFM_TEST_DATABASE_URL")
def test_a_server_database_needs_its_schema_upgraded():
    import sqlalchemy as sa

    url = sa.engine.make_url(conftest.SERVER_URL).set(database="afm_schema_test")
    root = sa.create_engine(url.set(database=""))
    with root.begin() as connection:
        connection.exec_driver_sql("DROP DATABASE IF EXISTS afm_schema_test")
        connection.exec_driver_sql("CREATE DATABASE afm_schema_test")
    rendered = url.render_as_string(hide_password=False)
    with pytest.raises(db.SchemaError, match="--db-upgrade"):
        db.make_sessionmaker(rendered)
    db.upgrade(rendered)
    sessionmaker = db.make_sessionmaker(rendered)
    with sessionmaker() as session:
        session.add(db.Secret(name="m_key", value="0x1"))
        session.commit()
    with root.begin() as connection:
        connection.exec_driver_sql("DROP DATABASE afm_schema_test")


class _GaleraSession:
    """Just enough of a session on a Galera node for check_serving"""

    def __init__(self, status, wsrep_on=1):
        self.status = status
        self.wsrep_on = wsrep_on

    def get_bind(self):
        class Bind:
            class dialect:
                name = "mysql"

        return Bind()

    def scalar(self, _statement):
        return self.wsrep_on

    def execute(self, _statement):
        class Result:
            def __init__(self, rows):
                self.rows = rows

            def all(self):
                return self.rows

        return Result(list(self.status.items()))


def _galera(state, comment, cluster="Primary", ready="ON"):
    return _GaleraSession(
        {
            "wsrep_cluster_status": cluster,
            "wsrep_ready": ready,
            "wsrep_local_state": state,
            "wsrep_local_state_comment": comment,
        }
    )


@pytest.mark.parametrize(
    "session",
    [
        _galera("4", "Synced"),
        _galera("2", "Donor/Desynced"),
        # Whether it has caught up is the causal wait's job, not this check's
        _galera("3", "Joined"),
        _GaleraSession({}, 0),
    ],
)
def test_a_primary_ready_node_serves(session):
    db.check_serving(session)


@pytest.mark.parametrize(
    "session",
    [
        _galera("4", "Synced", cluster="non-Primary"),
        _galera("4", "Synced", ready="OFF"),
    ],
)
def test_a_node_not_primary_or_not_ready_doesnt_serve(session):
    with pytest.raises(db.NotServing):
        db.check_serving(session)


def test_a_failed_status_query_doesnt_serve():
    class Failing(_GaleraSession):
        def execute(self, _statement):
            raise sa.exc.OperationalError("SHOW STATUS", {}, Exception("lost"))

        def rollback(self):
            pass

    with pytest.raises(db.NotServing):
        db.check_serving(Failing({}))


def test_every_statement_waits_causally(config):
    # A request's transaction begins with an UPDATE, the generation bump, so
    # reads alone (1) wouldn't make it wait (see specs/ha.md, Galera
    # specifics).
    if not config.connection:
        pytest.skip("needs a server database: AFM_TEST_DATABASE_URL")
    session = db.make_sessionmaker(config.database_url)()
    try:
        value = session.scalar(sa.text("SELECT @@session.wsrep_sync_wait"))
    except sa.exc.DBAPIError:
        pytest.skip("a server without wsrep")
    assert value == db.SYNC_WAIT == 7
