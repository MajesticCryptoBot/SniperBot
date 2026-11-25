"""Tests for Turso connection-loss recovery (Hrana stream drop → auto reconnect)."""

import sqlite3

import pytest

from sniper.db import Database, _is_connection_error

# The two real errors from the 2026-11-15 incident log.
HRANA_404 = 'Hrana: `api error: `status=404 Not Found, body={"error":"stream not found: 5a289435:83666c0"}``'
HRANA_502 = 'Hrana: `api error: `status=502 Bad Gateway, body={"error":"connect to upstream failed"}``'
HRANA_TIMEOUT = 'Hrana: `api error: `status=500, body={"error":"request timed out"}``'


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Scripted connection: script entries are None (succeed) or an exception
    to raise, consumed one per execute() call; empty script always succeeds."""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.closed = False

    def execute(self, sql, params=()):
        self.calls.append(sql)
        if self.script:
            item = self.script.pop(0)
            if item is not None:
                raise item
        return FakeCursor([{"n": 1}])

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_real_turso_error_messages_are_recognised():
    assert _is_connection_error(ValueError(HRANA_502))
    assert _is_connection_error(ValueError(HRANA_404))
    assert not _is_connection_error(ValueError('syntax error near "FROM"'))
    assert not _is_connection_error(sqlite3.OperationalError("no such table: trades"))


def test_execute_recovers_by_reconnecting():
    dead = FakeConn([ValueError(HRANA_404)] * 100)  # poisoned stream: never recovers
    fresh = FakeConn()
    db = Database(dead, "libsql", factory=lambda: fresh)
    assert db.fetchall_sync("SELECT 1") == [{"n": 1}]
    assert dead.closed  # dead connection was dropped
    assert db._conn is fresh  # and replaced
    db.close()


def test_safe_502_retries_through_a_short_outage(monkeypatch):
    """The 2026-11-16 log: reconnect succeeded, the very next call was still 502."""
    slept = []
    monkeypatch.setattr("sniper.db.time.sleep", lambda seconds: slept.append(seconds))
    # The replacement socket 502s once, then the gateway recovers.
    fresh = FakeConn([ValueError(HRANA_502)])
    db = Database(FakeConn([ValueError(HRANA_502)] * 5), "libsql", factory=lambda: fresh)
    assert db.fetchall_sync("SELECT 1") == [{"n": 1}]
    assert slept == [1.5]
    db.close()


def test_unsafe_connection_drop_is_not_retried_past_once(monkeypatch):
    """'connection closed before message completed' may have applied the write."""
    monkeypatch.setattr(
        "sniper.db.time.sleep",
        lambda _seconds: (_ for _ in ()).throw(AssertionError("must not back off")),
    )
    closed = 'Hrana: `http error: `connection closed before message completed``'
    fresh = FakeConn([ValueError(closed), ValueError(closed)])
    db = Database(FakeConn([ValueError(closed)]), "libsql", factory=lambda: fresh)
    with pytest.raises(ValueError):
        db.fetchall_sync("SELECT 1")
    # one retry on the fresh socket, then stop — no second backoff attempt
    assert len(fresh.calls) == 1
    db.close()


def test_execute_does_not_retry_plain_sql_errors():
    factory_calls = []

    def factory():
        factory_calls.append(1)
        return FakeConn()

    db = Database(FakeConn([ValueError('syntax error near "FROM"')]), "libsql", factory=factory)
    with pytest.raises(ValueError):
        db.fetchall_sync("SELECT bogus")
    assert not factory_calls
    db.close()


def test_reconnect_failure_raises_original_error():
    attempts = []

    def factory():
        attempts.append(1)
        raise OSError("network down")

    db = Database(FakeConn([ValueError(HRANA_404)] * 100), "libsql", factory=factory)
    with pytest.raises(ValueError):
        db.fetchall_sync("SELECT 1")
    assert attempts == [1]
    db.close()


def test_transaction_replays_whole_fn_after_stream_reset():
    # BEGIN succeeds, then the first statement inside the transaction hits the
    # 502 that killed the Turso session.
    dead = FakeConn([None, ValueError(HRANA_502)])
    fresh = FakeConn()
    db = Database(dead, "libsql", factory=lambda: fresh)
    ran = []

    def fn():
        ran.append(1)
        db.execute_sync("INSERT INTO x VALUES (1)")
        return "ok"

    assert db._transaction_sync(fn) == "ok"
    assert len(ran) == 2  # replayed as a whole, not retried as a lone statement
    assert fresh.calls[0] == "BEGIN"  # replay runs on a fresh transaction
    assert fresh.calls[1] == "INSERT INTO x VALUES (1)"
    db.close()


def test_transaction_does_not_replay_after_timeout():
    # A mid-response loss could have applied the statements — never replay.
    factory_calls = []

    def factory():
        factory_calls.append(1)
        return FakeConn()

    db = Database(FakeConn([ValueError(HRANA_TIMEOUT)]), "libsql", factory=factory)
    with pytest.raises(ValueError):
        db._transaction_sync(lambda: 1)
    assert not factory_calls
    db.close()


def test_sqlite_backend_never_reconnects():
    db = Database.connect_sqlite()
    with pytest.raises(sqlite3.OperationalError):
        db.fetchall_sync("SELECT nope FROM missing_table")
    db.close()
