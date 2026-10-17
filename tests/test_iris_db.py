"""WP10: the shared IRIS connection, on a fake driver (no IRIS needed)."""

import pytest

import hlidac.iris.db as db_module
from hlidac.iris.db import MAX_IDLE_SECONDS, Database


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.rowcount, self.closed = conn, 1, False

    def execute(self, sql, params):
        if self.conn.fail_with:
            raise self.conn.fail_with
        self.conn.statements.append(sql)

    def fetchall(self):
        return [("row",)]

    def close(self):
        self.closed = True
        self.conn.cursors_closed += 1


class FakeConnection:
    def __init__(self):
        self.statements, self.cursors_closed, self.closed, self.fail_with = [], 0, False, None

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    connections, now = [], [1000.0]
    monkeypatch.setattr(db_module.time, "monotonic", lambda: now[0])

    def connect():
        connections.append(FakeConnection())
        return connections[-1]

    return Database(connect), connections, now


def test_connects_lazily_once_and_closes_every_cursor(fake):
    db, connections, _ = fake
    assert connections == []
    assert db.query("SELECT 1") == [("row",)]
    assert db.execute("UPDATE x") == 1
    assert len(connections) == 1
    assert connections[0].cursors_closed == 2


def test_an_idle_connection_is_replaced_before_use(fake):
    db, connections, now = fake
    db.query("SELECT 1")
    now[0] += MAX_IDLE_SECONDS + 1  # Azure may have dropped it silently
    db.query("SELECT 2")
    assert len(connections) == 2
    assert connections[0].closed
    assert connections[1].statements == ["SELECT 2"]


def test_sql_errors_keep_the_connection_other_errors_replace_it(fake):
    db, connections, _ = fake
    db.query("SELECT 1")
    connections[0].fail_with = RuntimeError("[SQLCODE: <-99>:<Privilege violation>]")
    with pytest.raises(RuntimeError):
        db.execute("DELETE FROM x")
    assert not connections[0].closed
    connections[0].fail_with = OSError("Communication timed out")
    with pytest.raises(OSError):
        db.query("SELECT 1")
    assert connections[0].closed
    db.query("SELECT 1")
    assert len(connections) == 2
