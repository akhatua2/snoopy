"""Tests for system collector — verifies lock/unlock and sleep/wake detection."""

import time

import pytest

from snoopy.db import Database
from snoopy.buffer import EventBuffer
from snoopy.collectors.system import SystemCollector, _is_screen_locked


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


class TestIsScreenLocked:
    def test_returns_bool(self):
        """CGSession API should return a boolean without crashing."""
        result = _is_screen_locked()
        assert isinstance(result, bool)


class TestSystemCollector:
    def test_lock_unlock_detection(self, buf, db, monkeypatch):
        """Simulate screen lock → still locked → unlock. Should produce 2 events."""
        states = iter([False, True, True, False])
        monkeypatch.setattr("snoopy.collectors.system._is_screen_locked", lambda: next(states))

        c = SystemCollector(buf, db)
        c.setup()
        c.collect()  # initial state: unlocked → no event
        c.collect()  # unlocked → locked (logs "lock")
        c.collect()  # locked → locked (skipped)
        c.collect()  # locked → unlocked (logs "unlock")
        buf.flush()

        assert db.count("system_events") == 2

        cur = db._ensure_conn().execute(
            "SELECT event_type FROM system_events ORDER BY id"
        )
        rows = [r[0] for r in cur.fetchall()]
        assert rows == ["lock", "unlock"]

    def test_sleep_wake_detection_via_time_gap(self, buf, db, monkeypatch):
        """If a large time gap is detected between polls, log sleep + wake."""
        monkeypatch.setattr("snoopy.collectors.system._is_screen_locked", lambda: False)

        c = SystemCollector(buf, db)
        c.setup()
        c.collect()  # normal first poll

        # Simulate a 10-minute sleep by backdating last poll
        c._last_poll_ts = time.time() - 600

        c.collect()  # should detect sleep/wake gap
        buf.flush()

        assert db.count("system_events") == 2
        cur = db._ensure_conn().execute(
            "SELECT event_type FROM system_events ORDER BY id"
        )
        rows = [r[0] for r in cur.fetchall()]
        assert rows == ["sleep", "wake"]

    def test_no_events_when_state_unchanged(self, buf, db, monkeypatch):
        """If nothing changes, no events should be logged."""
        monkeypatch.setattr("snoopy.collectors.system._is_screen_locked", lambda: False)

        c = SystemCollector(buf, db)
        c.setup()
        c.collect()  # initial
        c.collect()  # same state
        c.collect()  # same state
        buf.flush()

        assert db.count("system_events") == 0

    def test_log_event_writes_to_db(self, buf, db):
        """Direct _log_event call should write to system_events."""
        c = SystemCollector(buf, db)
        c._log_event("wake", "gap=5.2min")
        buf.flush()

        cur = db._ensure_conn().execute(
            "SELECT event_type, details FROM system_events"
        )
        row = cur.fetchone()
        assert row == ("wake", "gap=5.2min")
