"""Tests for app lifecycle collector — verifies launch/quit detection."""

import subprocess

import pytest

from snoopy.db import Database
from snoopy.buffer import EventBuffer
from snoopy.collectors.applifecycle import AppLifecycleCollector, _get_running_apps


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


class TestGetRunningApps:
    def test_returns_set_of_app_names(self):
        """Should return a non-empty set of running app names."""
        apps = _get_running_apps()
        assert isinstance(apps, set)
        assert len(apps) > 0
        # Finder should always be running on macOS
        assert "Finder" in apps


class TestAppLifecycleCollector:
    def test_first_run_sets_baseline_no_events(self, buf, db, monkeypatch):
        """First poll should snapshot running apps without logging any events."""
        monkeypatch.setattr(
            "snoopy.collectors.applifecycle._get_running_apps",
            lambda: {"Safari", "Finder"},
        )

        c = AppLifecycleCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("app_events") == 0

    def test_detects_app_launch(self, buf, db, monkeypatch):
        """A new app appearing should be logged as a launch event."""
        snapshots = iter([
            {"Finder"},
            {"Finder", "Safari"},
        ])
        monkeypatch.setattr(
            "snoopy.collectors.applifecycle._get_running_apps",
            lambda: next(snapshots),
        )

        c = AppLifecycleCollector(buf, db)
        c.setup()
        c.collect()  # baseline
        c.collect()  # Safari appeared
        buf.flush()

        assert db.count("app_events") == 1
        cur = db._ensure_conn().execute(
            "SELECT event_type, app_name FROM app_events"
        )
        row = cur.fetchone()
        assert row == ("launch", "Safari")

    def test_detects_app_quit(self, buf, db, monkeypatch):
        """An app disappearing should be logged as a quit event."""
        snapshots = iter([
            {"Finder", "Safari"},
            {"Finder"},
        ])
        monkeypatch.setattr(
            "snoopy.collectors.applifecycle._get_running_apps",
            lambda: next(snapshots),
        )

        c = AppLifecycleCollector(buf, db)
        c.setup()
        c.collect()  # baseline
        c.collect()  # Safari gone
        buf.flush()

        assert db.count("app_events") == 1
        cur = db._ensure_conn().execute(
            "SELECT event_type, app_name FROM app_events"
        )
        row = cur.fetchone()
        assert row == ("quit", "Safari")

    def test_simultaneous_launch_and_quit(self, buf, db, monkeypatch):
        """Multiple apps changing at once should produce events for each."""
        snapshots = iter([
            {"Finder", "Safari"},
            {"Finder", "Chrome"},
        ])
        monkeypatch.setattr(
            "snoopy.collectors.applifecycle._get_running_apps",
            lambda: next(snapshots),
        )

        c = AppLifecycleCollector(buf, db)
        c.setup()
        c.collect()  # baseline
        c.collect()  # Safari quit, Chrome launched
        buf.flush()

        assert db.count("app_events") == 2

    def test_no_events_when_unchanged(self, buf, db, monkeypatch):
        """No events when running apps haven't changed."""
        monkeypatch.setattr(
            "snoopy.collectors.applifecycle._get_running_apps",
            lambda: {"Finder"},
        )

        c = AppLifecycleCollector(buf, db)
        c.setup()
        c.collect()  # baseline
        c.collect()  # same
        c.collect()  # same
        buf.flush()

        assert db.count("app_events") == 0
