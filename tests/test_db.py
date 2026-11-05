import sqlite3

from alternative_fabric_manager import db


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
