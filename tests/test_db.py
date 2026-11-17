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


def test_an_old_state_table_gains_its_text_column(tmp_path):
    # The per-port M_Key base's fingerprint is text, which state.value, an
    # integer, can't hold. The counters and the new tables survive.
    path = tmp_path / "afm.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE state (
            name VARCHAR(64) NOT NULL PRIMARY KEY,
            value INTEGER NOT NULL
        );
        INSERT INTO state VALUES ('generation', 7);
        """
    )
    connection.commit()
    connection.close()

    sessionmaker = db.make_sessionmaker(str(path))
    with sessionmaker() as session:
        assert session.get(db.State, "generation").value == 7
        assert session.get(db.State, "generation").text is None
        session.add(db.State(name="fingerprint", value=0, text="ab" * 32))
        session.add(db.FabricPort(guid="0" * 16))
        session.commit()
    # Upgrading again changes nothing.
    sessionmaker = db.make_sessionmaker(str(path))
    with sessionmaker() as session:
        assert session.get(db.State, "fingerprint").text == "ab" * 32
        assert session.get(db.FabricPort, "0" * 16).holds == "unknown"


def test_an_old_expected_switches_table_gains_its_pending_column(tmp_path):
    # Switches confirmed before are confirmed, not pending; a row added
    # without the column, as by an older version, isn't pending either.
    path = tmp_path / "afm.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE expected_switches (guid VARCHAR(16) NOT NULL PRIMARY KEY);
        INSERT INTO expected_switches VALUES ('0000000000000051');
        """
    )
    connection.commit()
    connection.close()
    sessionmaker = db.make_sessionmaker(str(path))
    with sessionmaker() as session:
        assert session.get(db.ExpectedSwitch, "0000000000000051").pending is False
        session.execute(
            sa.text("INSERT INTO expected_switches (guid) VALUES ('0000000000000052')")
        )
        session.add(db.ExpectedSwitch(guid="0000000000000053", pending=True))
        session.commit()
    sessionmaker = db.make_sessionmaker(str(path))
    with sessionmaker() as session:
        assert session.get(db.ExpectedSwitch, "0000000000000052").pending is False
        assert session.get(db.ExpectedSwitch, "0000000000000053").pending is True


def test_fabric_ports_and_links_round_trip(config):
    import datetime

    when = datetime.datetime(2026, 11, 16, 12, 0, 0, 123456, datetime.timezone.utc)
    sessionmaker = db.make_sessionmaker(config.database_url)
    with sessionmaker() as session:
        session.add(
            db.FabricPort(
                guid="ffffffffffff0001",
                node_guid="ffffffffffff0000",
                switch=True,
                holds="derived",
                lapsed=True,
                reachable=True,
                released=True,
                last_seen=when,
            )
        )
        session.add(db.FabricPort(guid="0000000000000002"))
        # Both directions of a link, as neighbors has them
        session.add(
            db.FabricLink(
                guid="ffffffffffff0001",
                port=1,
                peer_guid="0000000000000002",
                peer_port=1,
                last_seen=when,
            )
        )
        session.add(
            db.FabricLink(
                guid="0000000000000002",
                port=1,
                peer_guid="ffffffffffff0001",
                peer_port=1,
            )
        )
        session.commit()
    with sessionmaker() as session:
        port = session.get(db.FabricPort, "ffffffffffff0001")
        assert (port.node_guid, port.switch, port.holds) == (
            "ffffffffffff0000",
            True,
            "derived",
        )
        assert (port.lapsed, port.reachable, port.released) == (True, True, True)
        assert port.last_seen.replace(tzinfo=datetime.timezone.utc) == when
        new = session.get(db.FabricPort, "0000000000000002")
        # Not known until its NodeInfo answers
        assert new.switch is None and new.node_guid is None
        assert (new.holds, new.lapsed, new.reachable, new.released) == (
            "unknown",
            False,
            False,
            False,
        )
        assert new.last_seen is None
        link = session.get(db.FabricLink, ("ffffffffffff0001", 1))
        assert (link.peer_guid, link.peer_port) == ("0000000000000002", 1)
        assert session.get(db.FabricLink, ("0000000000000002", 1)).last_seen is None


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


@pytest.mark.skipif(not conftest.SERVER_URL, reason="needs AFM_TEST_DATABASE_URL")
def test_a_server_database_at_0003_gains_the_port_key_tables():
    # A deployed database is at 0003. 0004 adds state.text and the per-port
    # M_Key tables, and runs again from the start if it stopped midway,
    # since MariaDB commits each DDL statement.
    from alembic import command

    url = sa.engine.make_url(conftest.SERVER_URL).set(database="afm_schema_test")
    root = sa.create_engine(url.set(database=""))
    with root.begin() as connection:
        connection.exec_driver_sql("DROP DATABASE IF EXISTS afm_schema_test")
        connection.exec_driver_sql("CREATE DATABASE afm_schema_test")
    rendered = url.render_as_string(hide_password=False)
    alembic = db._alembic_config(rendered)
    command.upgrade(alembic, "0003")
    engine = sa.create_engine(rendered)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO state (name, value) VALUES ('generation', 7)"
        )
    db.upgrade(rendered)
    # As if it had stopped after its first step
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE fabric_links")
    command.stamp(alembic, "0003")
    db.upgrade(rendered)
    sessionmaker = db.make_sessionmaker(rendered)
    with sessionmaker() as session:
        assert session.get(db.State, "generation").value == 7
        session.add(db.State(name="fingerprint", value=0, text="ab" * 32))
        session.add(db.FabricPort(guid="0" * 16))
        session.add(
            db.FabricLink(guid="0" * 16, port=1, peer_guid="1" * 16, peer_port=2)
        )
        session.commit()
    # A row an older version adds, without the pending column, isn't pending.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO expected_switches (guid) VALUES ('0000000000000051')"
        )
    with sessionmaker() as session:
        assert session.get(db.ExpectedSwitch, "0000000000000051").pending is False
    engine.dispose()
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
