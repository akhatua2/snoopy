"""Tests for snoopy.buffer — EventBuffer."""

import threading
import time

import pytest

from snoopy.buffer import Event, EventBuffer
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


class TestEventBuffer:
    def test_push_and_flush(self, buf, db):
        buf.push(Event(
            table="window_events",
            columns=["timestamp", "app_name"],
            values=(time.time(), "Safari"),
        ))
        buf.flush()
        assert db.count("window_events") == 1

    def test_flush_empty_is_noop(self, buf):
        buf.flush()  # should not raise

    def test_push_many(self, buf, db):
        events = [
            Event("idle_events", ["timestamp", "idle_seconds", "is_idle"], (time.time(), i, 0))
            for i in range(10)
        ]
        buf.push_many(events)
        buf.flush()
        assert db.count("idle_events") == 10

    def test_auto_flush_on_max_size(self, db):
        """Buffer auto-flushes when BUFFER_MAX_SIZE is exceeded."""
        # Temporarily set a small max
        import snoopy.config as cfg
        original = cfg.BUFFER_MAX_SIZE
        cfg.BUFFER_MAX_SIZE = 5

        buf = EventBuffer(db)
        for i in range(6):
            buf.push(Event(
                "shell_events",
                ["timestamp", "command"],
                (time.time(), f"cmd_{i}"),
            ))

        # Should have auto-flushed
        assert db.count("shell_events") >= 5
        cfg.BUFFER_MAX_SIZE = original

    def test_thread_safety(self, buf, db):
        """Multiple threads pushing concurrently should not corrupt data."""
        errors = []

        def push_events(n):
            try:
                for i in range(n):
                    buf.push(Event(
                        "wifi_events",
                        ["timestamp", "ssid"],
                        (time.time(), f"net_{threading.current_thread().name}_{i}"),
                    ))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=push_events, args=(20,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        buf.flush()
        assert not errors
        assert db.count("wifi_events") == 100
