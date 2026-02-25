"""Tests for media collector — verifies deduplication and event creation."""

import time

import pytest

from snoopy.db import Database
from snoopy.buffer import EventBuffer
from snoopy.collectors.media import MediaCollector


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


class TestMediaCollector:
    def test_deduplication_skips_same_track(self, buf, db, monkeypatch):
        """When the same song is playing twice in a row, only one event should be logged.
        This verifies the title+artist dedup key works correctly."""

        fake_track = {
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "album": "A Night at the Opera",
            "playing": True,
            "app": "Spotify",
        }
        monkeypatch.setattr(MediaCollector, "_get_now_playing", staticmethod(lambda: fake_track))

        c = MediaCollector(buf, db)
        c.setup()
        c.collect()
        c.collect()  # same track — should be skipped
        buf.flush()

        assert db.count("media_events") == 1

    def test_logs_new_track_on_change(self, buf, db, monkeypatch):
        """When the track changes, a new event should be logged for the new track."""

        tracks = [
            {"title": "Song A", "artist": "Artist 1", "album": "", "playing": True, "app": "Music"},
            {"title": "Song B", "artist": "Artist 2", "album": "", "playing": True, "app": "Music"},
        ]
        call_count = {"n": 0}

        def fake_get():
            result = tracks[call_count["n"]]
            call_count["n"] += 1
            return result

        monkeypatch.setattr(MediaCollector, "_get_now_playing", staticmethod(fake_get))

        c = MediaCollector(buf, db)
        c.setup()
        c.collect()
        c.collect()
        buf.flush()

        assert db.count("media_events") == 2
