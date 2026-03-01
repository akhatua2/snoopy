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


def _create_fake_bookmarks(path, chrome_time) -> None:
    """Create a minimal Chrome Bookmarks JSON file."""
    import json
    data = {
        "checksum": "abc123",
        "roots": {
            "bookmark_bar": {
                "children": [
                    {
                        "date_added": str(chrome_time),
                        "guid": "aaa",
                        "id": "1",
                        "name": "Example",
                        "type": "url",
                        "url": "https://example.com",
                    },
                ],
                "name": "Bookmarks bar",
                "type": "folder",
            }
        },
        "version": 1,
    }
    path.write_text(json.dumps(data))


def _add_downloads(path, chrome_time) -> None:
    """Add downloads table to an existing fake Chrome DB."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE downloads (id INTEGER PRIMARY KEY, guid TEXT, "
        "current_path TEXT, target_path TEXT, start_time INTEGER, "
        "received_bytes INTEGER, total_bytes INTEGER, state INTEGER, "
        "danger_type INTEGER, interrupt_reason INTEGER, hash BLOB, "
        "end_time INTEGER, opened INTEGER, last_access_time INTEGER, "
        "transient INTEGER, referrer TEXT, site_url TEXT, "
        "embedder_download_data TEXT, tab_url TEXT, tab_referrer_url TEXT, "
        "http_method TEXT, by_ext_id TEXT, by_ext_name TEXT, "
        "by_web_app_id TEXT, etag TEXT, last_modified TEXT, "
        "mime_type TEXT, original_mime_type TEXT)"
    )
    conn.execute(
        "INSERT INTO downloads VALUES (1, 'abc', '/tmp/f.pdf', '/tmp/f.pdf', "
        "?, 1000, 1000, 1, 0, 0, X'', ?, 0, 0, 0, '', '', '', "
        "'https://example.com/f.pdf', '', 'GET', '', '', '', '', '', "
        "'application/pdf', 'application/pdf')",
        (chrome_time, chrome_time + 1000000),
    )
    conn.commit()
    conn.close()


def _add_search_terms(path, chrome_time) -> None:
    """Add keyword_search_terms to an existing fake Chrome DB."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE keyword_search_terms "
        "(keyword_id INTEGER NOT NULL, url_id INTEGER NOT NULL, "
        "term LONGVARCHAR NOT NULL, normalized_term LONGVARCHAR NOT NULL)"
    )
    conn.execute(
        "INSERT INTO keyword_search_terms VALUES (51, 1, 'test query', 'test query')"
    )
    conn.execute(
        "INSERT INTO keyword_search_terms VALUES (51, 2, 'another search', 'another search')"
    )
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

    def test_search_terms_first_run_skips_then_collects_new(self, buf, db, tmp_path, monkeypatch):
        """Search terms: first run skips existing, second run collects new."""
        fake_chrome = tmp_path / "History"
        _create_fake_chrome_db(fake_chrome)
        now_chrome = int(time.time() * 1_000_000) + _CHROME_EPOCH_OFFSET
        _add_search_terms(fake_chrome, now_chrome)

        monkeypatch.setattr("snoopy.config.CHROME_HISTORY", fake_chrome)
        monkeypatch.setattr("snoopy.config.ARC_HISTORY", tmp_path / "no_arc")
        monkeypatch.setattr("snoopy.config.SAFARI_HISTORY", tmp_path / "no_safari")
        monkeypatch.setattr("snoopy.config.FIREFOX_PROFILES", tmp_path / "no_ff")

        c = BrowserCollector(buf, db)
        c.setup()

        # First run: skips existing search terms
        c.collect()
        buf.flush()
        assert db.count("search_events") == 0

        # Add a new search term (url_id=3)
        conn = sqlite3.connect(str(fake_chrome))
        conn.execute(
            "INSERT INTO urls VALUES (3, 'https://google.com/search?q=new+query', 'new query', 1, 0, ?)",
            (now_chrome,),
        )
        conn.execute("INSERT INTO visits VALUES (3, 3, ?, 1000000, 0)", (now_chrome,))
        conn.execute(
            "INSERT INTO keyword_search_terms VALUES (51, 3, 'new query', 'new query')"
        )
        conn.commit()
        conn.close()

        # Second run: only the new search term
        c.collect()
        buf.flush()
        assert db.count("search_events") == 1
        row = db._conn.execute(
            "SELECT term, browser, url FROM search_events"
        ).fetchone()
        assert row[0] == "new query"
        assert row[1] == "chrome"
        assert "google.com" in row[2]

    def test_downloads_first_run_skips_then_collects_new(self, buf, db, tmp_path, monkeypatch):
        """Downloads: first run skips existing, second run collects new."""
        fake_chrome = tmp_path / "History"
        _create_fake_chrome_db(fake_chrome)
        now_chrome = int(time.time() * 1_000_000) + _CHROME_EPOCH_OFFSET
        _add_downloads(fake_chrome, now_chrome)

        monkeypatch.setattr("snoopy.config.CHROME_HISTORY", fake_chrome)
        monkeypatch.setattr("snoopy.config.ARC_HISTORY", tmp_path / "no_arc")
        monkeypatch.setattr("snoopy.config.SAFARI_HISTORY", tmp_path / "no_safari")
        monkeypatch.setattr("snoopy.config.FIREFOX_PROFILES", tmp_path / "no_ff")

        c = BrowserCollector(buf, db)
        c.setup()

        # First run: skips existing downloads
        c.collect()
        buf.flush()
        assert db.count("download_events") == 0

        # Add a new download (id=2)
        conn = sqlite3.connect(str(fake_chrome))
        conn.execute(
            "INSERT INTO downloads VALUES (2, 'def', '/tmp/g.zip', '/tmp/g.zip', "
            "?, 5000, 5000, 1, 0, 0, X'', ?, 0, 0, 0, '', '', '', "
            "'https://example.com/g.zip', '', 'GET', '', '', '', '', '', "
            "'application/zip', 'application/zip')",
            (now_chrome, now_chrome + 1000000),
        )
        conn.commit()
        conn.close()

        # Second run: only the new download
        c.collect()
        buf.flush()
        assert db.count("download_events") == 1
        row = db._conn.execute(
            "SELECT file_path, source_url, mime_type, browser FROM download_events"
        ).fetchone()
        assert row[0] == "/tmp/g.zip"
        assert "example.com" in row[1]
        assert row[2] == "application/zip"
        assert row[3] == "chrome"

    def test_bookmarks_first_run_skips_then_collects_new(self, buf, db, tmp_path, monkeypatch):
        """Bookmarks: first run skips existing, second run collects new."""
        import json

        fake_chrome = tmp_path / "History"
        _create_fake_chrome_db(fake_chrome)
        now_chrome = int(time.time() * 1_000_000) + _CHROME_EPOCH_OFFSET
        fake_bookmarks = tmp_path / "Bookmarks"
        _create_fake_bookmarks(fake_bookmarks, now_chrome)

        monkeypatch.setattr("snoopy.config.CHROME_HISTORY", fake_chrome)
        monkeypatch.setattr("snoopy.config.CHROME_BOOKMARKS", fake_bookmarks)
        monkeypatch.setattr("snoopy.config.ARC_HISTORY", tmp_path / "no_arc")
        monkeypatch.setattr("snoopy.config.ARC_BOOKMARKS", tmp_path / "no_arc_bk")
        monkeypatch.setattr("snoopy.config.SAFARI_HISTORY", tmp_path / "no_safari")
        monkeypatch.setattr("snoopy.config.FIREFOX_PROFILES", tmp_path / "no_ff")

        c = BrowserCollector(buf, db)
        c.setup()

        # First run: skips existing bookmarks
        c.collect()
        buf.flush()
        assert db.count("bookmark_events") == 0

        # Add a new bookmark with a later date_added
        data = json.loads(fake_bookmarks.read_text())
        data["roots"]["bookmark_bar"]["children"].append({
            "date_added": str(now_chrome + 1_000_000),
            "guid": "bbb",
            "id": "2",
            "name": "New Site",
            "type": "url",
            "url": "https://new-site.com",
        })
        fake_bookmarks.write_text(json.dumps(data))

        # Second run: only the new bookmark
        c.collect()
        buf.flush()
        assert db.count("bookmark_events") == 1
        row = db._conn.execute(
            "SELECT url, title, folder, browser FROM bookmark_events"
        ).fetchone()
        assert row[0] == "https://new-site.com"
        assert row[1] == "New Site"
        assert row[2] == "Bookmarks bar"
        assert row[3] == "chrome"
