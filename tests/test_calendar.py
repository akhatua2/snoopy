"""Tests for calendar collector — verifies living calendar with change tracking."""

import json

import pytest

from snoopy.db import Database
from snoopy.buffer import EventBuffer
from snoopy.collectors.calendar import CalendarCollector, _fetch_events


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


_SAMPLE_EVENTS = [
    {
        "uid": "ABC-123",
        "title": "CS224N Staff Mtg",
        "start": "2026-02-25T21:00:00Z",
        "end": "2026-02-25T22:00:00Z",
        "calendar": "Calendar",
        "location": "Gates 200",
        "all_day": False,
        "recurring": True,
        "attendees": ["Sarah Chen", "Diyi Yang"],
    },
    {
        "uid": "DEF-456",
        "title": "NLP Seminar",
        "start": "2026-02-26T20:00:00Z",
        "end": "2026-02-26T21:30:00Z",
        "calendar": "Meeting",
        "location": "Gates 2nd floor",
        "all_day": False,
        "recurring": True,
        "attendees": [],
    },
]


def _make_collector(buf, db, monkeypatch, events_fn):
    monkeypatch.setattr(
        "snoopy.collectors.calendar._fetch_events", events_fn,
    )
    c = CalendarCollector(buf, db)
    c.setup()
    return c


class TestCalendarCollector:
    def test_inserts_new_events(self, buf, db, monkeypatch):
        """New events should be inserted into the database."""
        c = _make_collector(buf, db, monkeypatch,
                            lambda helper: list(_SAMPLE_EVENTS))
        c.collect()
        assert db.count("calendar_events") == 2

    def test_logs_added_changes(self, buf, db, monkeypatch):
        """Each new event should produce an 'added' changelog entry."""
        c = _make_collector(buf, db, monkeypatch,
                            lambda helper: list(_SAMPLE_EVENTS))
        c.collect()

        conn = db._ensure_conn()
        rows = conn.execute(
            "SELECT event_uid, change_type FROM calendar_changes "
            "ORDER BY event_uid"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == ("ABC-123", "added")
        assert rows[1] == ("DEF-456", "added")

    def test_no_duplicates_on_second_collect(self, buf, db, monkeypatch):
        """Running collect twice with same events should not create duplicates."""
        c = _make_collector(buf, db, monkeypatch,
                            lambda helper: list(_SAMPLE_EVENTS))
        c.collect()
        c.collect()

        assert db.count("calendar_events") == 2
        # Only 2 'added' entries, no extra changes
        conn = db._ensure_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM calendar_changes"
        ).fetchone()[0]
        assert count == 2

    def test_updates_last_seen(self, buf, db, monkeypatch):
        """Second collect should update last_seen without creating changes."""
        c = _make_collector(buf, db, monkeypatch,
                            lambda helper: list(_SAMPLE_EVENTS))
        c.collect()

        conn = db._ensure_conn()
        first_seen_1 = conn.execute(
            "SELECT last_seen FROM calendar_events WHERE event_uid='ABC-123'"
        ).fetchone()[0]

        c.collect()
        last_seen_2 = conn.execute(
            "SELECT last_seen FROM calendar_events WHERE event_uid='ABC-123'"
        ).fetchone()[0]

        assert last_seen_2 >= first_seen_1

    def test_detects_title_modification(self, buf, db, monkeypatch):
        """Changing an event's title should update the row and log a change."""
        calls = [0]
        def fake_fetch(helper):
            calls[0] += 1
            if calls[0] == 1:
                return list(_SAMPLE_EVENTS)
            # Second call: title changed
            modified = [dict(e) for e in _SAMPLE_EVENTS]
            modified[0]["title"] = "CS224N Office Hours"
            return modified

        c = _make_collector(buf, db, monkeypatch, fake_fetch)
        c.collect()
        c.collect()

        conn = db._ensure_conn()
        # Title should be updated in calendar_events
        row = conn.execute(
            "SELECT title FROM calendar_events WHERE event_uid='ABC-123'"
        ).fetchone()
        assert row[0] == "CS224N Office Hours"

        # Should have a 'modified' change logged
        change = conn.execute(
            "SELECT field_name, old_value, new_value FROM calendar_changes "
            "WHERE change_type='modified' AND event_uid='ABC-123'"
        ).fetchone()
        assert change[0] == "title"
        assert change[1] == "CS224N Staff Mtg"
        assert change[2] == "CS224N Office Hours"

    def test_detects_location_modification(self, buf, db, monkeypatch):
        """Changing an event's location should be tracked."""
        calls = [0]
        def fake_fetch(helper):
            calls[0] += 1
            if calls[0] == 1:
                return [_SAMPLE_EVENTS[0]]
            modified = [dict(_SAMPLE_EVENTS[0])]
            modified[0]["location"] = "Zoom"
            return modified

        c = _make_collector(buf, db, monkeypatch, fake_fetch)
        c.collect()
        c.collect()

        conn = db._ensure_conn()
        change = conn.execute(
            "SELECT field_name, old_value, new_value FROM calendar_changes "
            "WHERE change_type='modified'"
        ).fetchone()
        assert change[0] == "location"
        assert change[1] == "Gates 200"
        assert change[2] == "Zoom"

    def test_detects_event_removal(self, buf, db, monkeypatch):
        """An event that disappears from the fetch should be marked removed."""
        calls = [0]
        def fake_fetch(helper):
            calls[0] += 1
            if calls[0] == 1:
                return list(_SAMPLE_EVENTS)
            # Second call: first event gone, only second remains
            return [_SAMPLE_EVENTS[1]]

        c = _make_collector(buf, db, monkeypatch, fake_fetch)
        c.collect()
        c.collect()

        conn = db._ensure_conn()
        # First event should be marked removed
        row = conn.execute(
            "SELECT status FROM calendar_events WHERE event_uid='ABC-123'"
        ).fetchone()
        assert row[0] == "removed"

        # Second event should still be active
        row = conn.execute(
            "SELECT status FROM calendar_events WHERE event_uid='DEF-456'"
        ).fetchone()
        assert row[0] == "active"

        # Should have a 'removed' change logged
        change = conn.execute(
            "SELECT event_uid, change_type FROM calendar_changes "
            "WHERE change_type='removed'"
        ).fetchone()
        assert change[0] == "ABC-123"

    def test_stores_attendees_as_comma_string(self, buf, db, monkeypatch):
        """Attendees list should be stored as a comma-separated string."""
        c = _make_collector(buf, db, monkeypatch,
                            lambda helper: [_SAMPLE_EVENTS[0]])
        c.collect()

        conn = db._ensure_conn()
        row = conn.execute(
            "SELECT title, attendees, is_recurring, status "
            "FROM calendar_events"
        ).fetchone()
        assert row[0] == "CS224N Staff Mtg"
        assert row[1] == "Sarah Chen, Diyi Yang"
        assert row[2] == 1
        assert row[3] == "active"

    def test_skips_events_without_uid(self, buf, db, monkeypatch):
        """Events missing uid or start should be skipped."""
        bad_events = [{"title": "No UID", "start": "2026-01-01T00:00:00Z"}]
        c = _make_collector(buf, db, monkeypatch, lambda helper: bad_events)
        c.collect()
        assert db.count("calendar_events") == 0

    def test_new_event_added_alongside_existing(self, buf, db, monkeypatch):
        """A new event should be added even when old events are already in the DB."""
        calls = [0]
        def fake_fetch(helper):
            calls[0] += 1
            if calls[0] == 1:
                return [_SAMPLE_EVENTS[0]]
            return list(_SAMPLE_EVENTS)  # adds second event

        c = _make_collector(buf, db, monkeypatch, fake_fetch)
        c.collect()
        assert db.count("calendar_events") == 1

        c.collect()
        assert db.count("calendar_events") == 2

    def test_has_first_seen_and_last_seen(self, buf, db, monkeypatch):
        """New events should have first_seen and last_seen populated."""
        c = _make_collector(buf, db, monkeypatch,
                            lambda helper: [_SAMPLE_EVENTS[0]])
        c.collect()

        conn = db._ensure_conn()
        row = conn.execute(
            "SELECT first_seen, last_seen FROM calendar_events"
        ).fetchone()
        assert row[0] is not None
        assert row[1] is not None
        assert row[0] == row[1]  # same on first insert
