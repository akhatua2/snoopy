"""Tests for reminders collector — verifies change tracking logic."""

import json
import time

import pytest

from snoopy.buffer import EventBuffer
from snoopy.collectors.reminders import RemindersCollector
from snoopy.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


_SAMPLE_REMINDERS = [
    {
        "uid": "R1",
        "title": "Buy groceries",
        "list": "Personal",
        "completed": False,
        "due_date": "2026-03-01T10:00:00Z",
        "modification_date": "2026-02-28T08:00:00Z",
    },
    {
        "uid": "R2",
        "title": "Call dentist",
        "list": "Health",
        "completed": False,
        "due_date": "",
        "modification_date": "2026-02-27T12:00:00Z",
    },
]


class TestRemindersCollector:
    def test_first_run_indexes_then_tracks_changes(self, buf, db, tmp_path, monkeypatch):
        """First run indexes state, subsequent runs detect changes."""
        fetch_data = list(_SAMPLE_REMINDERS)
        monkeypatch.setattr(
            "snoopy.collectors.reminders._fetch_reminders",
            lambda _: fetch_data,
        )

        c = RemindersCollector(buf, db)
        c.setup()

        # First run: index only, no events
        c.collect()
        buf.flush()
        assert db.count("reminder_events") == 0

        # Complete a reminder
        fetch_data[0] = {**fetch_data[0], "completed": True}
        c.collect()
        buf.flush()
        assert db.count("reminder_events") == 1
        row = db._conn.execute(
            "SELECT title, event_type FROM reminder_events"
        ).fetchone()
        assert row[0] == "Buy groceries"
        assert row[1] == "completed"

    def test_new_reminder_detected(self, buf, db, tmp_path, monkeypatch):
        """Adding a new reminder after first run emits 'added' event."""
        fetch_data = list(_SAMPLE_REMINDERS)
        monkeypatch.setattr(
            "snoopy.collectors.reminders._fetch_reminders",
            lambda _: fetch_data,
        )

        c = RemindersCollector(buf, db)
        c.setup()
        c.collect()  # first run
        buf.flush()

        # Add a new reminder
        fetch_data.append({
            "uid": "R3",
            "title": "New task",
            "list": "Work",
            "completed": False,
            "due_date": "",
            "modification_date": "2026-03-01T00:00:00Z",
        })
        c.collect()
        buf.flush()
        assert db.count("reminder_events") == 1
        row = db._conn.execute(
            "SELECT title, event_type, list_name FROM reminder_events"
        ).fetchone()
        assert row[0] == "New task"
        assert row[1] == "added"
        assert row[2] == "Work"

    def test_removed_reminder_detected(self, buf, db, tmp_path, monkeypatch):
        """Removing a reminder emits 'removed' event."""
        fetch_data = list(_SAMPLE_REMINDERS)
        monkeypatch.setattr(
            "snoopy.collectors.reminders._fetch_reminders",
            lambda _: fetch_data,
        )

        c = RemindersCollector(buf, db)
        c.setup()
        c.collect()  # first run
        buf.flush()

        # Remove second reminder
        fetch_data.pop(1)
        c.collect()
        buf.flush()
        assert db.count("reminder_events") == 1
        row = db._conn.execute(
            "SELECT title, event_type FROM reminder_events"
        ).fetchone()
        assert row[0] == "Call dentist"
        assert row[1] == "removed"
