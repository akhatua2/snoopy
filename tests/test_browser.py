"""Tests for browser collector — verifies Chromium parsing + watermark tracking."""

import sqlite3
import time

import pytest

from snoopy.buffer import EventBuffer
from snoopy.collectors.browser import _CHROME_EPOCH_OFFSET, BrowserCollector
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


def _create_fake_chrome_db(path) -> None:
    """Minimal Chromium history DB with 2 visits."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT,"
        " visit_count INTEGER, typed_count INTEGER, last_visit_time INTEGER)"
    )
    conn.execute(
        "CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER,"
        " visit_time INTEGER, visit_duration INTEGER, transition INTEGER)"
    )
    now_chrome = int(time.time() * 1_000_000) + _CHROME_EPOCH_OFFSET
    conn.execute(
        "INSERT INTO urls VALUES (1, 'https://example.com', 'Example', 1, 0, ?)",
        (now_chrome,),
    )
    conn.execute(
        "INSERT INTO urls VALUES (2, 'https://test.dev', 'Test', 1, 0, ?)",
        (now_chrome,),
    )
    conn.execute("INSERT INTO visits VALUES (1, 1, ?, 5000000, 0)", (now_chrome,))
    conn.execute("INSERT INTO visits VALUES (2, 2, ?, 3000000, 0)", (now_chrome - 60_000_000,))
    conn.commit()
    conn.close()


class TestBrowserCollector:
    def test_first_run_skips_history_then_collects_new(self, buf, db, tmp_path, monkeypatch):
        """First run should skip existing history (set watermark to max visit id).
        Adding a new visit after that should be collected on the next run."""
        fake_chrome = tmp_path / "History"
        _create_fake_chrome_db(fake_chrome)

        monkeypatch.setattr("snoopy.config.CHROME_HISTORY", fake_chrome)
        monkeypatch.setattr("snoopy.config.ARC_HISTORY", tmp_path / "no_arc")
        monkeypatch.setattr("snoopy.config.SAFARI_HISTORY", tmp_path / "no_safari")
        monkeypatch.setattr("snoopy.config.FIREFOX_PROFILES", tmp_path / "no_ff")

        c = BrowserCollector(buf, db)
        c.setup()

        # First run: skips existing 2 visits, just sets watermark
        c.collect()
        buf.flush()
        assert db.count("browser_events") == 0

        # Add a new visit (id=3) to the fake Chrome DB
        now_chrome = int(time.time() * 1_000_000) + _CHROME_EPOCH_OFFSET
        conn = sqlite3.connect(str(fake_chrome))
        conn.execute(
            "INSERT INTO urls VALUES (3, 'https://new.com', 'New', 1, 0, ?)",
            (now_chrome,),
        )
        conn.execute("INSERT INTO visits VALUES (3, 3, ?, 1000000, 0)", (now_chrome,))
        conn.commit()
        conn.close()

        # Second run: only the new visit should be collected
        c.collect()
        buf.flush()
        assert db.count("browser_events") == 1
