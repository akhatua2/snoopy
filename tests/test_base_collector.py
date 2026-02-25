"""Tests for BaseCollector ABC — verifies thread lifecycle, error handling, watermarks."""

import time

import pytest

from snoopy.db import Database
from snoopy.buffer import EventBuffer, Event
from snoopy.collectors.base import BaseCollector


class DummyCollector(BaseCollector):
    """Concrete collector for testing the base class."""
    name = "dummy"
    interval = 0.1

    def setup(self) -> None:
        self.call_count = 0

    def collect(self) -> None:
        self.call_count += 1
        self.buffer.push(Event(
            table="daemon_health",
            columns=["timestamp", "event_type", "details"],
            values=(time.time(), "test", f"call_{self.call_count}"),
        ))


class FailingCollector(BaseCollector):
    """Collector that raises on every collect — thread should survive."""
    name = "failing"
    interval = 0.1

    def collect(self) -> None:
        raise RuntimeError("intentional test error")


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


class TestBaseCollector:
    def test_start_stop(self, buf, db):
        """Start a collector, let it run a few cycles, stop it.
        Should have collected at least twice."""
        c = DummyCollector(buf, db)
        c.start()
        assert c.running
        time.sleep(0.35)
        c.stop()
        assert not c.running
        assert c.call_count >= 2

    def test_error_does_not_crash_thread(self, buf, db):
        """A collector that throws every cycle should keep its thread alive."""
        c = FailingCollector(buf, db)
        c.start()
        time.sleep(0.25)
        assert c.running
        c.stop()

    def test_watermark_helpers(self, buf, db):
        """Set and get a watermark through the collector's helper methods."""
        c = DummyCollector(buf, db)
        assert c.get_watermark() is None
        c.set_watermark("abc123")
        assert c.get_watermark() == "abc123"

    def test_cannot_instantiate_abc(self, buf, db):
        """BaseCollector is abstract and cannot be instantiated directly."""
        with pytest.raises(TypeError):
            BaseCollector(buf, db)  # type: ignore[abstract]
