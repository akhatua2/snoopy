"""Tests for snoopy.db — Database layer."""

import tempfile
import time
from pathlib import Path

import pytest

from snoopy.db import Database


@pytest.fixture
def db(tmp_path):
    """Yield an opened Database using a temp directory."""
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


class TestDatabaseLifecycle:
    def test_open_creates_file(self, tmp_path):
        path = tmp_path / "sub" / "test.db"
        d = Database(path=path)
        d.open()
        assert path.exists()
        d.close()

    def test_context_manager(self, tmp_path):
        path = tmp_path / "test.db"
        with Database(path=path) as d:
            assert d._conn is not None
        assert d._conn is None

    def test_double_close_safe(self, db):
        db.close()
        db.close()  # should not raise

    def test_ensure_conn_raises_when_closed(self, tmp_path):
        d = Database(path=tmp_path / "test.db")
        with pytest.raises(RuntimeError, match="not open"):
            d._ensure_conn()


class TestBatchInsert:
    def test_insert_and_count(self, db):
        db.batch_insert(
            "window_events",
            ["timestamp", "app_name", "window_title"],
            [(time.time(), "Safari", "Google"), (time.time(), "Code", "main.py")],
        )
        assert db.count("window_events") == 2

    def test_insert_empty_rows_noop(self, db):
        db.batch_insert("window_events", ["timestamp"], [])
        assert db.count("window_events") == 0

    def test_insert_one(self, db):
        db.insert_one(
            "idle_events",
            ["timestamp", "idle_seconds", "is_idle"],
            (time.time(), 5.0, 0),
        )
        assert db.count("idle_events") == 1

    def test_invalid_table_raises(self, db):
        with pytest.raises(ValueError, match="unknown table"):
            db.batch_insert("drop_table_users", ["x"], [(1,)])


class TestWatermarks:
    def test_get_nonexistent_returns_none(self, db):
        assert db.get_watermark("nonexistent") is None

    def test_set_and_get(self, db):
        db.set_watermark("browser", "12345", time.time())
        assert db.get_watermark("browser") == "12345"

    def test_upsert_overwrites(self, db):
        db.set_watermark("browser", "100", time.time())
        db.set_watermark("browser", "200", time.time())
        assert db.get_watermark("browser") == "200"


class TestHealth:
    def test_log_health(self, db):
        db.log_health(time.time(), "startup", "daemon started")
        assert db.count("daemon_health") == 1


class TestCount:
    def test_count_invalid_table(self, db):
        with pytest.raises(ValueError, match="unknown table"):
            db.count("evil_table")

    def test_count_empty_table(self, db):
        assert db.count("shell_events") == 0


class TestAllTablesExist:
    """Verify every expected table was created."""

    TABLES = [
        "window_events", "idle_events", "media_events", "browser_events",
        "shell_events", "wifi_events", "clipboard_events", "file_events",
        "claude_events", "network_events", "location_events",
        "notification_events", "audio_events", "message_events",
        "collector_state", "daemon_health",
    ]

    def test_all_tables_created(self, db):
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cur.fetchall()}
        for t in self.TABLES:
            assert t in tables, f"table {t!r} missing from schema"
